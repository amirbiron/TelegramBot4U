"""
ניהול הקשר Tenant — הבסיס לבידוד ה-multi-tenant.

הועתק מ-`ai-business-bot/tenancy.py` (ראה ROADMAP T0.2). ההבדל היחיד:
אין `tenant_faiss_dir` — בפרויקט הזה אין RAG ולכן אין אינדקס וקטורי
(‏PLAN §3.2). כל השאר זהה מילה במילה.

עקרונות:
- ה-context נקבע **רק בנקודות כניסה** (route של webhook, ‏before_request
  בפאנל, ‏job, ‏CLI) דרך tenant_context() / set_current_tenant. קוד עמוק
  לעולם לא מנחש tenant — הוא קורא get_current_tenant().
- contextvars זורמים אוטומטית לתוך asyncio tasks, אבל **לא** לתוך
  threading.Thread חדש ולא דרך asyncio.to_thread בכיוון ההפוך — בהעברת
  עבודה בין threads יש להעביר את ה-tenant כפרמטר ולקבוע אותו מחדש.
- מצב STRICT (משתנה סביבה TENANCY_STRICT=true): גישה בלי context קובע
  ⇒ חריגה. מודלק בפרודקשן; בטסטים משמש לאיתור נתיבים לא מכוסים.
"""

import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "default"

# slug חוקי: אותיות קטנות/ספרות/מקף, מתחיל באות/ספרה, עד 32 תווים.
# הולידציה היא גם קו ההגנה מפני path traversal (ה-slug משמש כשם תיקייה).
_TENANT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

_current_tenant: ContextVar[Optional[str]] = ContextVar("current_tenant", default=None)


class TenancyError(RuntimeError):
    """שגיאת בסיס של שכבת ה-tenancy."""


class MissingTenantContext(TenancyError):
    """גישה לנתונים בלי tenant context קובע (רלוונטי במצב STRICT)."""


class InvalidTenantSlug(TenancyError):
    """מזהה tenant שאינו עומד בכללי ה-slug."""


class TenantSuspendedError(TenancyError):
    """גישה לנתוני tenant מושעה / בתהליך הגירה — חסומה."""


class UnregisteredTenantError(TenancyError):
    """במצב STRICT: tenant שאינו רשום ב-control plane — חסום (הגנת typo)."""


def _strict_mode() -> bool:
    # נקרא דינמית (לא קבוע import-time) כדי שאפשר יהיה להדליק בטסטים
    # ובפרודקשן בלי לגעת בקוד.
    return os.getenv("TENANCY_STRICT", "").strip().lower() in ("1", "true", "yes")


def validate_tenant_id(tenant_id: str) -> str:
    """מוודא שה-tenant_id הוא slug חוקי ומחזיר אותו. זורק InvalidTenantSlug."""
    if not isinstance(tenant_id, str) or not _TENANT_SLUG_RE.match(tenant_id):
        raise InvalidTenantSlug(f"invalid tenant id: {tenant_id!r}")
    return tenant_id


def set_current_tenant(tenant_id: str):
    """קובע את ה-tenant הנוכחי ומחזיר token לשחזור (ראה reset_current_tenant)."""
    return _current_tenant.set(validate_tenant_id(tenant_id))


def reset_current_tenant(token) -> None:
    """משחזר את מצב ה-context שלפני set_current_tenant (לשימוש ב-teardown)."""
    _current_tenant.reset(token)


def get_current_tenant() -> str:
    """מחזיר את ה-tenant הנוכחי.

    כשה-context לא נקבע: במצב רגיל נופל ל-DEFAULT_TENANT (פיתוח וטסטים);
    במצב STRICT — זורק MissingTenantContext.
    """
    tenant = _current_tenant.get()
    if tenant is not None:
        return tenant
    if _strict_mode():
        raise MissingTenantContext(
            "tenant context was not set on this execution path "
            "(entry point must call set_current_tenant/tenant_context)"
        )
    return DEFAULT_TENANT


@contextmanager
def tenant_context(tenant_id: str):
    """קובע tenant לבלוק קוד ומשחזר את הקודם ביציאה (גם בחריגה)."""
    token = set_current_tenant(tenant_id)
    try:
        yield tenant_id
    finally:
        reset_current_tenant(token)


def _tenants_root() -> Path:
    import config as _config  # ייבוא עצל — נקרא דינמית כדי לכבד patches

    return Path(_config.DATA_DIR) / "tenants"


def _check_tenant_allowed(tenant: str) -> None:
    """אכיפת סטטוס מול ה-control plane.

    - tenant מושעה / בהגירה ⇒ חסימה (בכל מצב).
    - tenant לא-רשום ⇒ נחסם רק במצב STRICT (הגנת typo בפלטפורמה);
      במצב רגיל מותר — טסטים ופיתוח יוצרים tenants בלי registry.
    - ה-tenant של ברירת המחדל (legacy) לעולם לא נבדק — הוא לא רשום.
    """
    if tenant == DEFAULT_TENANT:
        return
    # ייבוא עצל — control_plane מייבא את tenancy ברמת המודול; הכיוון
    # ההפוך חייב להיות בתוך הפונקציה כדי לא ליצור מעגל ייבוא.
    from control_plane import get_tenant_status_cached

    status = get_tenant_status_cached(tenant)
    if status in ("suspended", "migrating"):
        raise TenantSuspendedError(
            f"tenant '{tenant}' במצב '{status}' — הגישה לנתוניו חסומה"
        )
    if status is None and _strict_mode():
        raise UnregisteredTenantError(
            f"tenant '{tenant}' אינו רשום ב-control plane (מצב STRICT)"
        )


def tenant_db_path(tenant_id: Optional[str] = None) -> Path:
    """נתיב קובץ ה-SQLite של ה-tenant.

    ה-tenant של ברירת המחדל ממופה ל-config.DB_PATH (פיתוח מקומי, טסטים,
    ו-tenant יחיד בשלב 1). כל tenant אחר — DATA_DIR/tenants/<slug>/chatbot.db.
    הערה: הפונקציה לא יוצרת תיקיות — יצירת ה-tenant (onboarding) אחראית לכך.
    """
    tenant = validate_tenant_id(tenant_id) if tenant_id else get_current_tenant()
    if tenant == DEFAULT_TENANT:
        import config as _config

        return Path(_config.DB_PATH)
    _check_tenant_allowed(tenant)
    path = (_tenants_root() / tenant / "chatbot.db").resolve()
    # belt-and-braces מעבר לולידציית ה-slug: הנתיב חייב להישאר תחת השורש
    if not path.is_relative_to(_tenants_root().resolve()):
        raise InvalidTenantSlug(f"tenant path escapes tenants root: {tenant!r}")
    return path


def tenant_data_dir(tenant_id: str) -> Path:
    """תיקיית ה-data plane של ה-tenant (מכילה chatbot.db).

    בניגוד ל-tenant_db_path — **לא** מפעילה בדיקת סטטוס
    (_check_tenant_allowed), כדי שאפשר יהיה למחוק את קבצי ה-tenant גם
    כשהוא מושעה/בתהליך הסרה (אז פתירת הנתיב הרגילה הייתה נחסמת).
    לא חלה על ה-tenant ה-legacy ('default') — קבציו יושבים בנתיבי config.
    """
    tenant = validate_tenant_id(tenant_id)
    if tenant == DEFAULT_TENANT:
        raise InvalidTenantSlug("ל-tenant ה-legacy ('default') אין תיקיית data plane ייעודית")
    path = (_tenants_root() / tenant).resolve()
    # belt-and-braces מעבר לולידציית ה-slug: הנתיב חייב להישאר תחת השורש
    if not path.is_relative_to(_tenants_root().resolve()):
        raise InvalidTenantSlug(f"tenant path escapes tenants root: {tenant!r}")
    return path


def remove_tenant_files(tenant_id: str) -> bool:
    """מחיקת כל קבצי ה-data plane של tenant (chatbot.db + WAL/SHM).

    נקרא בתהליך מחיקת tenant — אחרי שהרשומה כבר הוסרה מה-control plane.
    מחזיר True אם התיקייה נמחקה או שלא הייתה קיימת מלכתחילה. חריגה
    (למשל הרשאות דיסק) מופצת לקורא, שירשום אותה ל-log ויסמן כישלון חלקי.
    """
    import shutil

    target = tenant_data_dir(tenant_id)
    if not target.exists():
        return True
    shutil.rmtree(target)
    return True
