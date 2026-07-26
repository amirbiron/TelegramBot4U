"""
גיבוי לילי של קובצי ה-tenants ושל ה-control plane (‏ROADMAP T4.4).

הועתק מ-`ai-business-bot` בהתאמה לערוץ: ענפי ה-FAISS ירדו (אין RAG,
ואין `tenant_faiss_dir`). השאר חל כלשונו.

**למה זה קיים:** בארכיטקטורת קובץ-לכל-tenant, הדיסק של Render הוא
‏SPOF. בלי גיבוי, אובדן שלו הוא אובדן כל הלקוחות.

**עקביות:** ה-SQLite מגובה דרך ה-online backup API של `sqlite3` ולא
ב-`cp`. עותק של קובץ WAL פעיל באמצע כתיבה הוא DB פגום; ה-API הזה בטוח
לגיבוי חי ואינו תופס lock ארוך.

**יעד:** ‏`BACKUP_DIR` (ברירת מחדל `DATA_DIR/backups`). העלאה ל-object
storage היא **seam מפורש** — ‏`set_upload_hook` נקרא לכל ארכיון שנוצר.
בלי hook: גיבוי מקומי בלבד, עם rotation. זה מכוון: דיסק mounted הוא
הצעד הראשון, וענן הוא החלטה תפעולית נפרדת.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ‏seam ל-object storage: ‏(local_path, relative_key) -> None
_upload_hook: Optional[Callable[[Path, str], None]] = None


def set_upload_hook(hook: Optional[Callable[[Path, str], None]]) -> None:
    """רישום פונקציית העלאה ל-object storage (‏S3/GCS/וכו')."""
    global _upload_hook
    _upload_hook = hook


def _backup_dir() -> Path:
    import config as _cfg

    raw = os.getenv("BACKUP_DIR", "").strip()
    return Path(raw) if raw else Path(_cfg.DATA_DIR) / "backups"


def _retention_days() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "14")))
    except ValueError:
        logger.error("BACKUP_RETENTION_DAYS אינו מספר — נופלים ל-14")
        return 14


def _sqlite_backup(src: Path, dst: Path) -> None:
    """גיבוי עקבי של קובץ SQLite דרך ה-online backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src), timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _maybe_upload(local_path: Path, relative_key: str) -> None:
    if _upload_hook is None:
        return
    try:
        _upload_hook(local_path, relative_key)
    except Exception:
        # כשל העלאה לא מבטל את הגיבוי המקומי — הוא עדיין על הדיסק
        logger.error("העלאת הגיבוי %s נכשלה", relative_key, exc_info=True)


def backup_tenant(tenant_id: str, stamp: str) -> bool:
    """גיבוי ה-DB של tenant בודד. מחזיר True בהצלחה.

    ה-stamp מסופק ע"י הקורא ולא נגזר כאן: אותו stamp משמש את כל
    ה-tenants בריצה, כך שכולם נופלים לאותה תיקייה גם אם הריצה חוצה
    חצות.
    """
    from tenancy import tenant_context, tenant_db_path

    try:
        with tenant_context(tenant_id):
            db_src = tenant_db_path()
    except Exception:
        logger.error("backup_tenant: פתרון הנתיב נכשל (%s)", tenant_id, exc_info=True)
        return False

    if not Path(db_src).exists():
        # ‏tenant רשום בלי קובץ DB — לא אמור לקרות אחרי create_tenant
        logger.warning("backup_tenant: אין קובץ DB ל-%s", tenant_id)
        return False

    try:
        db_dst = _backup_dir() / stamp / tenant_id / "chatbot.db"
        _sqlite_backup(Path(db_src), db_dst)
        _maybe_upload(db_dst, f"{stamp}/{tenant_id}/chatbot.db")
        return True
    except Exception:
        logger.error("backup_tenant: הגיבוי נכשל (%s)", tenant_id, exc_info=True)
        return False


def backup_platform_db(stamp: str) -> bool:
    """גיבוי `platform.db` — בלעדיו אין למי לשחזר את קובצי ה-tenants."""
    from control_plane import platform_db_path

    src = platform_db_path()
    if not src.exists():
        return True  # אין רישום עדיין (מצב legacy) — אין מה לגבות
    try:
        dst = _backup_dir() / stamp / "_platform" / "platform.db"
        _sqlite_backup(src, dst)
        _maybe_upload(dst, f"{stamp}/_platform/platform.db")
        return True
    except Exception:
        logger.error("גיבוי platform.db נכשל", exc_info=True)
        return False


def _prune_old_backups(now_epoch: float) -> int:
    """מחיקת תיקיות גיבוי מעבר ל-retention. מחזיר כמה נמחקו.

    ה-prune מבוסס על **שם התיקייה** (התאריך המקודד בו) ולא על ה-mtime
    של הדיסק: ‏mtime משתנה בכל נגיעה בקובץ, ובקפיצת שעון הוא היה עלול
    למחוק גיבוי טרי. תיקייה ששמה אינו תאריך תקין — מדולגת בבטחה, כי
    אין לנו דרך לדעת מה היא.
    """
    from datetime import datetime, timezone

    root = _backup_dir()
    if not root.exists():
        return 0
    cutoff = datetime.fromtimestamp(
        now_epoch - _retention_days() * 86400, tz=timezone.utc,
    ).date()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_date < cutoff:
            try:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
            except OSError:
                logger.error("prune: מחיקת %s נכשלה", child, exc_info=True)
    if removed:
        logger.info("prune: נמחקו %d תיקיות גיבוי ישנות", removed)
    return removed


def run_backup(stamp: str, now_epoch: float) -> dict:
    """גיבוי מלא: כל ה-tenants + ‏platform.db + ‏prune.

    לולאת I/O על רשימת פריטים — ‏try/except **פר-tenant**: כשל אצל אחד
    לא עוצר את גיבוי השאר.
    """
    from control_plane import list_schedulable_tenant_ids

    summary = {"tenants_ok": 0, "tenants_failed": 0, "platform_ok": False, "pruned": 0}
    try:
        tenant_ids = list_schedulable_tenant_ids()
    except Exception:
        logger.error("run_backup: שליפת רשימת ה-tenants נכשלה", exc_info=True)
        tenant_ids = []

    for tenant_id in tenant_ids:
        try:
            ok = backup_tenant(tenant_id, stamp)
        except Exception:
            logger.error("run_backup: כשל לא צפוי ב-%s", tenant_id, exc_info=True)
            ok = False
        summary["tenants_ok" if ok else "tenants_failed"] += 1

    summary["platform_ok"] = backup_platform_db(stamp)
    summary["pruned"] = _prune_old_backups(now_epoch)
    logger.info("גיבוי לילי הושלם: %s", summary)
    return summary


def restore_tenant(tenant_id: str, stamp: str) -> bool:
    """שחזור DB של tenant מגיבוי. **דורס** את הקובץ הקיים.

    קיים כדי שנתיב השחזור יהיה מכוסה בטסט ולא רק בתיאוריה: גיבוי שלא
    ניסו לשחזר ממנו אינו גיבוי.
    """
    from tenancy import tenant_context, tenant_db_path

    src = _backup_dir() / stamp / tenant_id / "chatbot.db"
    if not src.exists():
        logger.error("restore_tenant: אין גיבוי ל-%s בחותמת %s", tenant_id, stamp)
        return False
    try:
        with tenant_context(tenant_id):
            dst = Path(tenant_db_path())
        dst.parent.mkdir(parents=True, exist_ok=True)
        # דרך ה-backup API גם בכיוון הזה: העתקת קובץ על DB פתוח משאירה
        # קובצי WAL ישנים לצדו, וה-DB המשוחזר עלול לקרוא מהם.
        _sqlite_backup(src, dst)
        for suffix in ("-wal", "-shm"):
            stale = Path(str(dst) + suffix)
            if stale.exists():
                stale.unlink()
        logger.info("restore_tenant: %s שוחזר מחותמת %s", tenant_id, stamp)
        return True
    except Exception:
        logger.error("restore_tenant: השחזור נכשל (%s)", tenant_id, exc_info=True)
        return False
