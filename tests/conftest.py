"""fixtures משותפים — DB זמני פר-טסט ומשתני סביבה מבודדים.

בניגוד לריפו המקור, ‏python-telegram-bot היא תלות אמיתית כאן (הערוץ כולו
נשען עליה) ולכן היא **לא** ממוקקת גלובלית. מה שכן ממוקק בטסט בודד: קריאות
רשת (‏Bot.send_message וכו') — לעולם לא פונים ל-API אמיתי.
"""

import os
import sys
from pathlib import Path

import pytest

# שורש הריפו ל-sys.path — המודולים יושבים בשורש (אין חבילת aliases)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """סביבה מבודדת: כל טסט מקבל DATA_DIR משלו, כך שייבוא config לא נוגע
    בקבצים אמיתיים ו-platform.db של טסט אחד לא דולף לשני."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("ADMIN_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    # מפתח Fernet קבוע — בלעדיו כל כתיבת סוד פלטפורמה נכשלת (fail-closed)
    monkeypatch.setenv(
        "SECRETS_ENCRYPTION_KEY",
        "qV5Bw4Yw3VgX9V0E9-zZ_T1xX5sQqM4hCgL9pZsK5oI=",
    )
    monkeypatch.setenv("LEDGER_PEPPER_V1", "test-pepper-for-unit-tests")
    monkeypatch.setenv("TENANCY_STRICT", "false")

    # config נטען פעם אחת ב-import וקורא את ה-env אז; מסנכרנים את הערכים
    # התלויי-נתיב לכל טסט (הדפוס של הריפו המקור — patch על המודול, בלי reload
    # שישבור את ה-binding של cryptography).
    import config as _cfg

    monkeypatch.setattr(_cfg, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(_cfg, "DB_PATH", tmp_path / "test.db", raising=False)
    monkeypatch.setattr(_cfg, "ADMIN_SECRET_KEY", "test-secret", raising=False)
    monkeypatch.setattr(_cfg, "ADMIN_USERNAME", "admin", raising=False)
    monkeypatch.setattr(_cfg, "ADMIN_PASSWORD", "test-password", raising=False)
    monkeypatch.setattr(_cfg, "ADMIN_PASSWORD_HASH", "", raising=False)

    # ניקוי caches ברמת מודול שנשמרים בין טסטים
    import control_plane as _cp
    import kb_service as _kb
    import rate_limiter as _rl
    from services import owner_channel as _oc

    _cp.invalidate_status_cache()
    _cp.invalidate_connection_cache()
    _kb.invalidate_cache()
    _rl.reset_all()
    _oc.reset_dedup()
    yield


@pytest.fixture
def default_tenant_db(tmp_path):
    """‏DB של ה-tenant של ברירת המחדל, עם סכימה מלאה, תחת context."""
    import database as db
    from tenancy import DEFAULT_TENANT, tenant_context

    with tenant_context(DEFAULT_TENANT):
        db.init_db()
        yield db


@pytest.fixture
def platform_db():
    """‏control plane מאותחל (platform.db בתוך ה-DATA_DIR הזמני)."""
    import control_plane as cp

    cp.init_platform_db()
    return cp


@pytest.fixture
def tenant(platform_db):
    """‏tenant רשום עם data plane מאותחל. מחזיר את ה-slug."""
    platform_db.create_tenant("acme", "עסק לדוגמה")
    return "acme"

