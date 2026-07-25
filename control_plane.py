"""
Control Plane — רישום ה-tenants של הפלטפורמה.

הועתק מ-`ai-business-bot/control_plane.py` וקוצץ לערוץ היחיד של הפרויקט
(‏ROADMAP T0.3). השכבה מחזיקה את המידע ה*תפעולי* על tenants — לא נתוני
ריצה של עסקים:

- `tenants` — רישום ה-tenants ומצבם (active/suspended/migrating).
- `tenant_routes` — מיפוי מפתחות ראוטינג נכנסים → tenant (מפתח ה-webhook
  של הבוט-הבן, ‏slug ציבורי).
- `tenant_secrets` — סודות פר-tenant (טוקן הבוט-הבן, סוד ה-webhook)
  מוצפנים Fernet **fail-closed**: בלי SECRETS_ENCRYPTION_KEY הכתיבה נחסמת.
- `managed_bots` / `business_connections` / `pairing_codes` — הסכימות
  החדשות של מודל B (‏PLAN §5.1).

ה-DB הוא קובץ SQLite נפרד (`DATA_DIR/platform.db`) שאינו שייך לאף
tenant — ולכן יש לו get_platform_connection משלו שאינו עובר דרך
tenancy. **אסור** לגשת אליו דרך database.get_connection.

נתוני העסק עצמם (שיחות, בסיס ידע...) נשארים בקובץ ה-SQLite של כל
tenant — זו הפרדת ה-data plane / control plane.
"""

import logging
import re
import secrets as _secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from tenancy import (
    DEFAULT_TENANT,
    InvalidTenantSlug,
    TenancyError,
    remove_tenant_files,
    tenant_context,
    tenant_db_path,
    validate_tenant_id,
)
from utils.crypto import decrypt_field, encrypt_field_strict

logger = logging.getLogger(__name__)

TENANT_STATUSES = ("active", "suspended", "migrating")

# סוגי ראוטים מוכרים — resolve של עדכון נכנס לפי (route_type, route_key).
# ה-CHECK בסכימה משקף את אותה רשימה; להוסיף סוג = מיגרציה + עדכון כאן.
# קוצץ לערוץ telegram_business בלבד (אין Twilio/Meta/widget).
ROUTE_TYPES = (
    "telegram_webhook_key",
    "public_slug",
)

# סטטוסים של בוט-בן מנוהל (PLAN §5.1):
#   created      — נוצר דרך Managed Bots, יש לנו טוקן
#   secretary_on — ‏Secretary Mode דלוק אצלו (כרגע ידנית — ראה V1)
#   connected    — הבעלים חיבר אותו לחשבון והתקבל business_connection
#   revoked      — ‏offboarding: הטוקן הוחלף וה-webhook בוטל
MANAGED_BOT_STATUSES = ("created", "secretary_on", "connected", "revoked")


class TenantExistsError(TenancyError):
    """ניסיון ליצור tenant עם slug תפוס."""


class UnknownTenantError(TenancyError):
    """פעולה על tenant שאינו רשום."""


def platform_db_path() -> Path:
    """נתיב קובץ ה-platform.db — נגזר דינמית מ-config (מכבד patches)."""
    import config as _config

    return Path(_config.DATA_DIR) / "platform.db"


@contextmanager
def get_platform_connection():
    """חיבור ל-platform.db — **לא** עובר דרך tenancy (הקובץ גלובלי).

    אותם pragmas כמו get_connection של ה-data plane (WAL, busy_timeout,
    foreign_keys) — הקובץ משותף ל-threads של Flask/schedulers.
    """
    path = platform_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_platform_db() -> None:
    """יצירת סכימת ה-control plane (idempotent — CREATE IF NOT EXISTS)."""
    statuses = ", ".join(f"'{s}'" for s in TENANT_STATUSES)
    route_types = ", ".join(f"'{t}'" for t in ROUTE_TYPES)
    bot_statuses = ", ".join(f"'{s}'" for s in MANAGED_BOT_STATUSES)
    with get_platform_connection() as conn:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id    TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active'
                             CHECK(status IN ({statuses})),
                plan         TEXT NOT NULL DEFAULT 'premium',
                notes        TEXT NOT NULL DEFAULT '',
                created_at   TEXT DEFAULT (datetime('now')),
                updated_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tenant_routes (
                route_type TEXT NOT NULL CHECK(route_type IN ({route_types})),
                route_key  TEXT NOT NULL,
                tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id)
                           ON DELETE CASCADE,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (route_type, route_key)
            );
            CREATE INDEX IF NOT EXISTS idx_tenant_routes_tenant
                ON tenant_routes(tenant_id);

            CREATE TABLE IF NOT EXISTS tenant_secrets (
                tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id)
                           ON DELETE CASCADE,
                name       TEXT NOT NULL,
                value_enc  TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (tenant_id, name)
            );

            CREATE TABLE IF NOT EXISTS admin_users (
                email         TEXT PRIMARY KEY,   -- מנורמל ל-lowercase
                password_hash TEXT NOT NULL,      -- werkzeug, לעולם לא מוחזר החוצה
                display_name  TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL CHECK(role IN ('owner','platform_admin')),
                tenant_id     TEXT REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                status        TEXT NOT NULL DEFAULT 'active'
                              CHECK(status IN ('active','disabled')),
                created_at    TEXT DEFAULT (datetime('now')),
                last_login_at TEXT,
                -- owner חייב עסק; platform_admin חוצה-עסקים (tenant_id ריק)
                CHECK((role = 'owner' AND tenant_id IS NOT NULL)
                      OR (role = 'platform_admin' AND tenant_id IS NULL))
            );
            CREATE INDEX IF NOT EXISTS idx_admin_users_tenant
                ON admin_users(tenant_id);

            CREATE TABLE IF NOT EXISTS platform_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- ── מודל B: בוט לכל לקוח (PLAN §5.1) ────────────────────────

            -- בוטים-בנים. הטוקן עצמו ב-tenant_secrets (מוצפן), לא כאן.
            CREATE TABLE IF NOT EXISTS managed_bots (
                bot_id          INTEGER PRIMARY KEY,           -- Telegram bot id של הבן
                tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                bot_username    TEXT NOT NULL,
                owner_user_id   INTEGER NOT NULL,              -- המשתמש היוצר = בעל העסק
                status          TEXT NOT NULL DEFAULT 'created'
                                CHECK(status IN ({bot_statuses})),
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_mbots_tenant ON managed_bots(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_mbots_owner  ON managed_bots(owner_user_id);

            -- מצב חיבור ה-Secretary של כל tenant (לרוב שורה אחת פר-tenant).
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id   TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                owner_user_id   INTEGER NOT NULL,
                user_chat_id    INTEGER,                       -- ערוץ הניהול (PLAN §4.5)
                is_enabled      INTEGER NOT NULL DEFAULT 1,
                can_reply       INTEGER NOT NULL DEFAULT 0,
                rights_json     TEXT NOT NULL DEFAULT '{{}}',
                connected_at    TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_bizconn_tenant ON business_connections(tenant_id);

            -- צימוד מוקדם: קושר owner_user_id ל-tenant לפני יצירת הבוט (PLAN §4.6).
            CREATE TABLE IF NOT EXISTS pairing_codes (
                code            TEXT PRIMARY KEY,              -- secrets.token_urlsafe
                tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
                created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at      TEXT NOT NULL,
                used_at         TEXT,
                used_by_user_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_pairing_tenant ON pairing_codes(tenant_id);
        """)


def get_platform_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    """קריאת ערך מטא תפעולי של הפלטפורמה (למשל last-run של job)."""
    if not platform_db_path().exists():
        return default
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT value FROM platform_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_platform_meta(key: str, value: str) -> None:
    """שמירת ערך מטא תפעולי (upsert)."""
    init_platform_db()
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT INTO platform_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = datetime('now')",
            (key, value),
        )


# ─── מחזור חיים של tenant ────────────────────────────────────────────────


def create_tenant(tenant_id: str, display_name: str, plan: str = "premium") -> None:
    """יצירת tenant חדש: רישום + תיקייה + סכימת DB.

    ה-slug 'default' שמור ל-tenant ה-legacy (הקבצים הקיימים) ואינו נרשם
    דרך הפונקציה הזו.
    """
    validate_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT:
        raise InvalidTenantSlug(
            f"'{DEFAULT_TENANT}' שמור ל-tenant ה-legacy ואינו נוצר דרך ה-control plane"
        )

    init_platform_db()
    with get_platform_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if existing:
            raise TenantExistsError(f"tenant כבר קיים: {tenant_id}")
        conn.execute(
            "INSERT INTO tenants (tenant_id, display_name, plan) VALUES (?, ?, ?)",
            (tenant_id, display_name, plan),
        )
    # ה-cache עלול להחזיק None מקריאה שקדמה ליצירה (TTL של 30ש')
    invalidate_status_cache(tenant_id)

    # יצירת ה-data plane של ה-tenant: תיקייה + סכימה מלאה (init_db רץ את
    # אותו executescript + migrations כמו בכל עליית תהליך — לכל קובץ בנפרד).
    # שם העסק אינו נזרע ל-tenant DB — get_business_config גוזר אותו ישירות
    # מ-display_name של ה-control plane (מקור אמת יחיד).
    db_file = tenant_db_path(tenant_id)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with tenant_context(tenant_id):
        import database as db

        db.init_db()

    logger.info("tenant created: %s (%s)", tenant_id, display_name)


def get_tenant(tenant_id: str) -> Optional[dict]:
    """שליפת רשומת tenant, או None אם אינו רשום (או שאין platform.db)."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return dict(row) if row else None


def list_tenants(status: Optional[str] = None) -> list[dict]:
    """כל ה-tenants הרשומים, אופציונלית מסונן לפי סטטוס."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tenants WHERE status = ? ORDER BY tenant_id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tenants ORDER BY tenant_id").fetchall()
        return [dict(r) for r in rows]


def migrate_all_tenants() -> dict:
    """עדכון סכימה (init_db) ל-DB של כל tenant פעיל — בכל עליית תהליך.

    שורש הבעיה שזה פותר: ה-data-plane DB של כל tenant עובר init_db
    (executescript + migrations) רק *פעם אחת*, ב-create_tenant. עמודה
    חדשה שנוספת ב-migration אחרי-כן (ADD COLUMN) חסרה מ-tenant DBs
    קיימים, וכל כתיבה שמפנה אליה זורקת "no such column".

    הפונקציה מריצה init_db על כל tenant *פעיל* (idempotent — על DB
    מעודכן זה no-op; על DB ישן זה משלים את העמודות החסרות). tenants
    מושעים/בהגירה מדולגים: גישתם ל-DB חסומה ממילא ואינם מוגשים לעדכונים.

    עמידה לכשל פר-tenant (CLAUDE.md — לולאות I/O): כשל ב-DB אחד לא עוצר
    את השאר ולא מפיל את עליית התהליך.
    """
    import database as db

    result = {"migrated": 0, "errors": 0}
    for t in list_tenants(status="active"):
        tenant_id = t["tenant_id"]
        try:
            with tenant_context(tenant_id):
                db.init_db()
            result["migrated"] += 1
        except Exception:
            logger.error(
                "migrate_all_tenants: כשל בעדכון סכימה ל-tenant=%s", tenant_id,
                exc_info=True,
            )
            result["errors"] += 1
    return result


def set_tenant_status(tenant_id: str, status: str) -> None:
    """עדכון סטטוס (active/suspended/migrating) + אינבלידציה של ה-cache."""
    if status not in TENANT_STATUSES:
        raise ValueError(f"סטטוס לא מוכר: {status!r} (מותר: {TENANT_STATUSES})")
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE tenants SET status = ?, updated_at = datetime('now') "
            "WHERE tenant_id = ?",
            (status, tenant_id),
        )
        if cur.rowcount == 0:
            raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    invalidate_status_cache(tenant_id)
    logger.info("tenant %s → status=%s", tenant_id, status)


def list_schedulable_tenant_ids() -> list[str]:
    """ה-tenants שה-jobs המתוזמנים רצים עליהם.

    כשאין רישום (אין platform.db או שאין בו tenants) — ה-tenant של ברירת
    המחדל בלבד (מצב שלב 1, בוט ידני). ברגע שנרשמו tenants, הרישום הוא
    מקור האמת ורק active נכללים.
    """
    registered = list_tenants()
    if not registered:
        return [DEFAULT_TENANT]
    return [t["tenant_id"] for t in registered if t["status"] == "active"]


def delete_tenant(tenant_id: str, *, backup: bool = True) -> dict:
    """מחיקה מלאה ובלתי-הפיכה של tenant מהפלטפורמה.

    מסיר את כל מה שמרכיב לקוח: שורת ה-tenant ב-control plane (ואיתה, דרך
    ON DELETE CASCADE, ה-routes, ה-secrets, ה-managed_bots,
    ה-business_connections, ה-pairing_codes ומשתמשי ה-owner), וכן קבצי
    ה-data plane על הדיסק.

    שכבתיות: ביטול ה-webhook מול טלגרם והחלפת הטוקן של הבוט-הבן (רשת,
    async) הם באחריות הקורא שרץ בתהליך השרת — יש לבצע אותם *לפני*
    הקריאה, בעוד הטוקן קיים (ראה offboarding, ‏PLAN §4.6). כאן נוגעים
    בנתונים בלבד.

    סדר קריטי:
      1. גיבוי — בעוד הסטטוס 'active' (backup פותר נתיבים דרך
         tenant_db_path שחוסם tenant מושעה, ולכן חייב לרוץ לפני ההשעיה).
      2. השעיה — חוסמת גישה חדשה לנתונים בכל המצבים בזמן הפירוק.
      3. מחיקת השורה — מפעילה את ה-cascade.
      4. אינבלידציית ה-caches — שלא ייקרא כ'פעיל' עד ל-TTL.
      5. מחיקת הקבצים מהדיסק (אחרת הם יתומים).
    """
    validate_tenant_id(tenant_id)
    if tenant_id == DEFAULT_TENANT:
        raise InvalidTenantSlug(
            f"'{DEFAULT_TENANT}' הוא ה-tenant ה-legacy ואינו נמחק דרך ה-control plane"
        )
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")

    summary: dict = {
        "tenant_id": tenant_id,
        "backup_ok": None,
        "backup_stamp": None,
        "cascade": {},
        "files_removed": False,
    }

    # 1. גיבוי אחרון — בעוד הסטטוס 'active'. best-effort: כשל נרשם ומדווח
    #    ב-summary אבל אינו עוצר את המחיקה.
    if backup:
        try:
            import backup_service

            stamp = "deleted-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            summary["backup_ok"] = bool(backup_service.backup_tenant(tenant_id, stamp))
            summary["backup_stamp"] = stamp
        except Exception:
            logger.error("delete_tenant(%s): גיבוי אחרון נכשל", tenant_id, exc_info=True)
            summary["backup_ok"] = False

    # 2. ספירת מה שיימחק ב-cascade (לדיווח/אודיט) — לפני המחיקה בפועל
    with get_platform_connection() as conn:
        for label, table in (
            ("routes", "tenant_routes"),
            ("secrets", "tenant_secrets"),
            ("admin_users", "admin_users"),
            ("managed_bots", "managed_bots"),
            ("business_connections", "business_connections"),
            ("pairing_codes", "pairing_codes"),
        ):
            row = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
            summary["cascade"][label] = row["c"]

    # 3. השעיה — חוסמת גישה חדשה לנתונים בזמן הפירוק ומוציאה מלולאות ה-schedulers
    set_tenant_status(tenant_id, "suspended")

    # 4. מחיקת שורת ה-tenant — מפעילה ON DELETE CASCADE
    with get_platform_connection() as conn:
        conn.execute("DELETE FROM tenants WHERE tenant_id = ?", (tenant_id,))

    # 5. אינבלידציית caches
    invalidate_status_cache(tenant_id)
    invalidate_connection_cache()

    # 6. מחיקת קבצי ה-data plane
    try:
        summary["files_removed"] = bool(remove_tenant_files(tenant_id))
    except Exception:
        logger.error(
            "delete_tenant(%s): מחיקת קבצי ה-data plane נכשלה", tenant_id, exc_info=True
        )
        summary["files_removed"] = False

    logger.info(
        "tenant deleted: %s (backup_ok=%s cascade=%s files_removed=%s)",
        tenant_id, summary["backup_ok"], summary["cascade"], summary["files_removed"],
    )
    return summary


# ─── cache סטטוסים (נצרך ע"י tenancy בכל פתיחת חיבור) ────────────────────

_STATUS_CACHE_TTL = 30.0
_status_cache: dict[str, tuple[float, Optional[str]]] = {}
_status_cache_lock = threading.Lock()


def get_tenant_status_cached(tenant_id: str) -> Optional[str]:
    """סטטוס ה-tenant עם cache קצר (30ש'). None = לא רשום/אין platform.db."""
    import time

    now = time.monotonic()
    with _status_cache_lock:
        hit = _status_cache.get(tenant_id)
        if hit and now - hit[0] < _STATUS_CACHE_TTL:
            return hit[1]
    row = get_tenant(tenant_id)
    status = row["status"] if row else None
    with _status_cache_lock:
        _status_cache[tenant_id] = (now, status)
    return status


def invalidate_status_cache(tenant_id: Optional[str] = None) -> None:
    """אינבלידציה — אחרי שינוי סטטוס (או הכל, בטסטים)."""
    with _status_cache_lock:
        if tenant_id is None:
            _status_cache.clear()
        else:
            _status_cache.pop(tenant_id, None)


# ─── ראוטים (מיפוי מפתח נכנס → tenant) ───────────────────────────────────


def set_route(route_type: str, route_key: str, tenant_id: str) -> None:
    """רישום/עדכון ראוט. INSERT OR REPLACE — המפתח הוא natural key וניתן
    להצביע מחדש (למשל אחרי רוטציית טוקן של בוט-בן)."""
    if route_type not in ROUTE_TYPES:
        raise ValueError(f"route_type לא מוכר: {route_type!r} (מותר: {ROUTE_TYPES})")
    route_key = (route_key or "").strip()
    if not route_key:
        raise ValueError("route_key ריק")
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tenant_routes (route_type, route_key, tenant_id) "
            "VALUES (?, ?, ?)",
            (route_type, route_key, tenant_id),
        )


def resolve_route(route_type: str, route_key: str) -> Optional[str]:
    """tenant_id של מפתח נכנס, או None אם אינו רשום."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM tenant_routes "
            "WHERE route_type = ? AND route_key = ?",
            (route_type, (route_key or "").strip()),
        ).fetchone()
        return row["tenant_id"] if row else None


def delete_route(route_type: str, route_key: str) -> bool:
    """הסרת ראוט. מחזיר True אם נמחק בפועל."""
    with get_platform_connection() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_routes WHERE route_type = ? AND route_key = ?",
            (route_type, route_key),
        )
        return cur.rowcount > 0


def list_routes(tenant_id: Optional[str] = None) -> list[dict]:
    """הראוטים הרשומים (אופציונלית של tenant מסוים)."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM tenant_routes WHERE tenant_id = ? "
                "ORDER BY route_type, route_key",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tenant_routes ORDER BY tenant_id, route_type"
            ).fetchall()
        return [dict(r) for r in rows]


def get_tenant_route_key(tenant_id: str, route_type: str) -> Optional[str]:
    """ה-route_key הרשום ל-tenant עבור סוג נתון (lookup הפוך ל-resolve).

    משמש לבניית URLs יוצאים (‏set_webhook של בוט-בן). אם רשומים כמה —
    מחזיר את הראשון.
    """
    if route_type not in ROUTE_TYPES:
        raise ValueError(f"route_type לא מוכר: {route_type!r}")
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT route_key FROM tenant_routes "
            "WHERE tenant_id = ? AND route_type = ? ORDER BY created_at LIMIT 1",
            (tenant_id, route_type),
        ).fetchone()
        return row["route_key"] if row else None


def generate_route_key() -> str:
    """מפתח ראוטינג אקראי בלתי-ניתן-לניחוש (ל-webhook של בוט-בן)."""
    return _secrets.token_urlsafe(24)


# ─── סודות פר-tenant (מוצפנים, fail-closed) ──────────────────────────────

# שמות הסודות המוכרים — לתיעוד ולולידציה רכה בלבד (אזהרה, לא חסימה,
# כדי לא לחסום סוד חדש שנוסף בקוד לפני שהרשימה עודכנה).
KNOWN_SECRET_NAMES = (
    # טוקן הבוט-הבן — מגיע מ-getManagedBotToken (שלב 2) או מהזנה ידנית (שלב 1)
    "telegram_bot_token",
    # הסוד לאימות הכותרת X-Telegram-Bot-Api-Secret-Token של ה-webhook
    "telegram_webhook_secret",
    # שם המשתמש של הבוט (t.me/<username>) — נלכד ב-getMe; לא סוד אמיתי
    # אבל נשמר באותו מנגנון יחד עם שאר נתוני הערוץ
    "telegram_bot_username",
    # מפתחות LLM פר-tenant (אופציונלי — ברירת המחדל היא ה-env של הפלטפורמה)
    "openai_api_key",
    "anthropic_api_key",
)

_SECRET_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def set_tenant_secret(tenant_id: str, name: str, value: str) -> None:
    """שמירת סוד מוצפן. fail-closed: בלי SECRETS_ENCRYPTION_KEY — חריגה.

    ערך ריק מוחק את הסוד (אין טעם לשמור שורות ריקות).
    """
    if not _SECRET_NAME_RE.match(name or ""):
        raise ValueError(f"שם סוד לא חוקי: {name!r} (a-z0-9_ עד 64 תווים)")
    if name not in KNOWN_SECRET_NAMES:
        logger.warning("set_tenant_secret: שם סוד לא ברשימה המוכרת: %s", name)
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")

    if not value:
        with get_platform_connection() as conn:
            conn.execute(
                "DELETE FROM tenant_secrets WHERE tenant_id = ? AND name = ?",
                (tenant_id, name),
            )
        return

    value_enc = encrypt_field_strict(value)
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT INTO tenant_secrets (tenant_id, name, value_enc) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(tenant_id, name) DO UPDATE SET "
            "value_enc = excluded.value_enc, updated_at = datetime('now')",
            (tenant_id, name, value_enc),
        )


def get_tenant_secret(tenant_id: str, name: str) -> Optional[str]:
    """שליפת סוד מפוענח, או None אם אינו קיים."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT value_enc FROM tenant_secrets WHERE tenant_id = ? AND name = ?",
            (tenant_id, name),
        ).fetchone()
    if not row:
        return None
    return decrypt_field(row["value_enc"])


def list_tenant_secret_names(tenant_id: str) -> list[str]:
    """שמות הסודות הקיימים ל-tenant — בלי הערכים (לתצוגת סטטוס בלבד)."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        rows = conn.execute(
            "SELECT name FROM tenant_secrets WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()
        return [r["name"] for r in rows]


# ─── בוטים-בנים מנוהלים (managed_bots) ───────────────────────────────────


def register_managed_bot(
    bot_id: int,
    tenant_id: str,
    bot_username: str,
    owner_user_id: int,
    status: str = "created",
) -> None:
    """רישום/עדכון בוט-בן. ‏INSERT OR REPLACE — יצירה חוזרת של אותו בוט
    (רוטציית טוקן, ‏managed_bot update חוזר) מעדכנת במקום להיכשל.

    created_at משתמר מהרשומה הקיימת (‏INSERT OR REPLACE מוחק ומכניס מחדש,
    ולכן קוראים את הערך הישן ומעבירים אותו במפורש).
    """
    if status not in MANAGED_BOT_STATUSES:
        raise ValueError(f"סטטוס בוט לא מוכר: {status!r} (מותר: {MANAGED_BOT_STATUSES})")
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    with get_platform_connection() as conn:
        prev = conn.execute(
            "SELECT created_at FROM managed_bots WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        created_at = prev["created_at"] if prev else datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn.execute(
            "INSERT OR REPLACE INTO managed_bots "
            "(bot_id, tenant_id, bot_username, owner_user_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (bot_id, tenant_id, bot_username, owner_user_id, status, created_at),
        )
    logger.info(
        "managed bot registered: bot_id=%s tenant=%s status=%s", bot_id, tenant_id, status
    )


def get_managed_bot(bot_id: int) -> Optional[dict]:
    """רשומת בוט-בן לפי מזהה הבוט בטלגרם."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT * FROM managed_bots WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        return dict(row) if row else None


def get_managed_bots_by_owner(owner_user_id: int) -> list[dict]:
    """כל הבוטים-הבנים שנוצרו ע"י משתמש מסוים (ההתאמה הראשית ב-onboarding)."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM managed_bots WHERE owner_user_id = ? ORDER BY created_at",
            (owner_user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_managed_bot_for_tenant(tenant_id: str) -> Optional[dict]:
    """הבוט-הבן הפעיל של ה-tenant (המאוחר ביותר שאינו revoked)."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT * FROM managed_bots WHERE tenant_id = ? AND status != 'revoked' "
            "ORDER BY created_at DESC, bot_id DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None


def list_managed_bots(tenant_id: Optional[str] = None) -> list[dict]:
    """כל הבוטים-הבנים (אופציונלית של tenant מסוים) — לתצוגת פלטפורמה."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT * FROM managed_bots WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM managed_bots ORDER BY tenant_id, created_at"
            ).fetchall()
        return [dict(r) for r in rows]


def set_managed_bot_status(bot_id: int, status: str) -> None:
    """עדכון סטטוס בוט-בן (created → secretary_on → connected → revoked)."""
    if status not in MANAGED_BOT_STATUSES:
        raise ValueError(f"סטטוס בוט לא מוכר: {status!r} (מותר: {MANAGED_BOT_STATUSES})")
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE managed_bots SET status = ?, updated_at = datetime('now') "
            "WHERE bot_id = ?",
            (status, bot_id),
        )
        if cur.rowcount == 0:
            raise UnknownTenantError(f"בוט-בן לא רשום: {bot_id}")


# ─── חיבורי Secretary (business_connections) ─────────────────────────────
#
# cache: ה-resolve של connection_id → רשומה קורה בכל הודעה נכנסת. ‏TTL קצר
# + אינבלידציה מפורשת בכל כתיבה — אותה תבנית כמו cache הסטטוסים למעלה.
#
# **למה המפתח אינו כולל tenant** (בניגוד לכלל הכללי ב-CLAUDE.md):
# ‏connection_id הוא מזהה גלובלי־ייחודי שטלגרם מנפיקה, וה-tenant הוא
# *ערך* בתוך הרשומה — לא חלק מהזהות שלה. מפתוח לפי (tenant, id) היה
# שובר את הפונקציה עבור קוראים חוצי-tenant (פאנל הפלטפורמה). אכיפת
# הבידוד יושבת בנקודה אחת מפורשת: `business_handlers._resolve_connection`
# משווה את `tenant_id` שברשומה ל-tenant הנוכחי ודוחה אי-התאמה
# (הגנת cross-wiring, ‏PLAN §4.2). יש טסט ייעודי לזה.

_CONNECTION_CACHE_TTL = 30.0
_connection_cache: dict[str, tuple[float, Optional[dict]]] = {}
_connection_cache_lock = threading.Lock()


def invalidate_connection_cache(connection_id: Optional[str] = None) -> None:
    """אינבלידציה של cache החיבורים — אחרי כל כתיבה (או הכל, בטסטים)."""
    with _connection_cache_lock:
        if connection_id is None:
            _connection_cache.clear()
        else:
            _connection_cache.pop(connection_id, None)


def upsert_business_connection(
    connection_id: str,
    tenant_id: str,
    owner_user_id: int,
    user_chat_id: Optional[int] = None,
    is_enabled: bool = True,
    can_reply: bool = False,
    rights_json: str = "{}",
) -> None:
    """שמירת מצב חיבור Secretary. ‏INSERT OR REPLACE — חיבור מחדש/עריכת
    הרשאות מעדכנים את אותה שורה (‏connection_id הוא ה-natural key).

    connected_at משתמר מהרשומה הקיימת — הוא מתעד את החיבור הראשון.
    """
    connection_id = (connection_id or "").strip()
    if not connection_id:
        raise ValueError("connection_id ריק")
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    with get_platform_connection() as conn:
        prev = conn.execute(
            "SELECT connected_at FROM business_connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        connected_at = prev["connected_at"] if prev else datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO business_connections "
            "(connection_id, tenant_id, owner_user_id, user_chat_id, is_enabled, "
            " can_reply, rights_json, connected_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                connection_id, tenant_id, owner_user_id, user_chat_id,
                1 if is_enabled else 0, 1 if can_reply else 0,
                rights_json or "{}", connected_at,
            ),
        )
    invalidate_connection_cache(connection_id)
    logger.info(
        "business connection upserted: tenant=%s enabled=%s can_reply=%s",
        tenant_id, is_enabled, can_reply,
    )


def get_business_connection(connection_id: str) -> Optional[dict]:
    """רשומת חיבור לפי מזהה — עם cache קצר (נקרא בכל הודעה נכנסת)."""
    import time

    connection_id = (connection_id or "").strip()
    if not connection_id:
        return None
    now = time.monotonic()
    with _connection_cache_lock:
        hit = _connection_cache.get(connection_id)
        if hit and now - hit[0] < _CONNECTION_CACHE_TTL:
            return hit[1]
    row_dict: Optional[dict] = None
    if platform_db_path().exists():
        with get_platform_connection() as conn:
            row = conn.execute(
                "SELECT * FROM business_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            row_dict = dict(row) if row else None
    with _connection_cache_lock:
        _connection_cache[connection_id] = (now, row_dict)
    return row_dict


def get_business_connection_for_tenant(tenant_id: str) -> Optional[dict]:
    """החיבור הפעיל של ה-tenant (לתצוגת "הבוט שלי" ולערוץ הבעלים).

    כשיש כמה — מחזיר את המחובר האחרון (‏is_enabled קודם).
    """
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT * FROM business_connections WHERE tenant_id = ? "
            "ORDER BY is_enabled DESC, updated_at DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None


def disable_business_connection(connection_id: str) -> bool:
    """סימון חיבור כמנותק (‏is_enabled=0). מחזיר True אם עודכן בפועל.

    השורה נשמרת בכוונה — היא ההיסטוריה של החיבור ומאפשרת לזהות חיבור
    מחדש של אותו מזהה.
    """
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE business_connections SET is_enabled = 0, can_reply = 0, "
            "updated_at = datetime('now') WHERE connection_id = ?",
            (connection_id,),
        )
        changed = cur.rowcount > 0
    invalidate_connection_cache(connection_id)
    return changed


# ─── קודי צימוד (pairing_codes) ──────────────────────────────────────────

PAIRING_CODE_TTL_MINUTES = 60


def create_pairing_code(tenant_id: str, ttl_minutes: int = PAIRING_CODE_TTL_MINUTES) -> str:
    """יצירת קוד צימוד חד-פעמי ל-tenant. מחזיר את הקוד.

    הקוד נשמר ל-DB **לפני** שהוא מוצג/נשלח למשתמש (דפוס קריטי #9 —
    credential נשמר לפני שנשלח, ‏fail closed).
    """
    if get_tenant(tenant_id) is None:
        raise UnknownTenantError(f"tenant לא רשום: {tenant_id}")
    code = _secrets.token_urlsafe(12)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with get_platform_connection() as conn:
        conn.execute(
            "INSERT INTO pairing_codes (code, tenant_id, expires_at) VALUES (?, ?, ?)",
            (code, tenant_id, expires_at),
        )
    logger.info("pairing code created for tenant=%s (ttl=%dm)", tenant_id, ttl_minutes)
    return code


def get_pairing_code(code: str) -> Optional[dict]:
    """רשומת קוד צימוד (לתצוגת סטטוס באשף) — בלי לצרוך אותו."""
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT * FROM pairing_codes WHERE code = ?", (code,)
        ).fetchone()
        return dict(row) if row else None


def consume_pairing_code(code: str, user_id: int) -> Optional[str]:
    """צריכת קוד צימוד: מסמן כמשומש ומחזיר את ה-tenant_id.

    מחזיר None אם הקוד לא קיים, פג תוקף, או כבר נוצל. הסימון והבדיקה
    נעשים ב-UPDATE אחד מותנה (‏CAS) כדי שקריאה מקבילית לא תצרוך פעמיים.
    """
    code = (code or "").strip()
    if not code:
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE pairing_codes SET used_at = ?, used_by_user_id = ? "
            "WHERE code = ? AND used_at IS NULL AND expires_at > ?",
            (now, user_id, code, now),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT tenant_id FROM pairing_codes WHERE code = ?", (code,)
        ).fetchone()
        return row["tenant_id"] if row else None


def get_tenant_by_paired_user(user_id: int) -> Optional[str]:
    """ה-tenant שהמשתמש צומד אליו — לפי הקוד האחרון שהוא ניצל.

    זו ההתאמה שמאפשרת לקשור `managed_bot` update ל-tenant: טלגרם אומרת
    מי המשתמש היוצר, אבל לא לאיזה עסק הוא שייך אצלנו. הצימוד קורה
    **לפני** יצירת הבוט, בדיוק בשביל זה (‏PLAN §4.6).

    אין טבלת מיפוי נפרדת: `pairing_codes.used_by_user_id` הוא כבר
    הרישום, וטבלה נוספת הייתה מקור אמת שני שיכול לסטות.
    """
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM pairing_codes WHERE used_by_user_id = ? "
            "ORDER BY used_at DESC, code LIMIT 1",
            (int(user_id),),
        ).fetchone()
        return row["tenant_id"] if row else None


def purge_expired_pairing_codes() -> int:
    """ניקוי קודים שפג תוקפם ולא נוצלו. מחזיר כמה נמחקו."""
    if not platform_db_path().exists():
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_platform_connection() as conn:
        cur = conn.execute(
            "DELETE FROM pairing_codes WHERE used_at IS NULL AND expires_at <= ?",
            (now,),
        )
        return cur.rowcount or 0


# ─── משתמשי אדמין (בעלי עסקים + platform admins) ─────────────────────────

# hash דמה — מורץ גם כשה-email לא קיים, כדי שזמן התגובה לא יסגיר האם
# החשבון קיים (timing oracle). נוצר פעם אחת בזמן import.
_DUMMY_PASSWORD_HASH: Optional[str] = None


def _dummy_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        from werkzeug.security import generate_password_hash

        _DUMMY_PASSWORD_HASH = generate_password_hash(_secrets.token_urlsafe(16))
    return _DUMMY_PASSWORD_HASH


def _normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 254:
        raise ValueError("כתובת אימייל לא תקינה")
    return email


def create_admin_user(
    email: str,
    password: str,
    role: str = "owner",
    tenant_id: Optional[str] = None,
    display_name: str = "",
) -> None:
    """יצירת משתמש אדמין. ‏owner חייב tenant רשום; ‏platform_admin — בלי.

    נוצר אך ורק ע"י מפעיל הפלטפורמה (CLI) — אין self-registration, ולכן
    אין כאן וקטור auto-admin לפי email לא מאומת (דפוס קריטי #3).
    """
    from werkzeug.security import generate_password_hash

    email = _normalize_email(email)
    if role not in ("owner", "platform_admin"):
        raise ValueError(f"role לא מוכר: {role!r}")
    if not password or len(password) < 8:
        raise ValueError("סיסמה קצרה מדי (מינימום 8 תווים)")
    if role == "owner":
        if not tenant_id or get_tenant(tenant_id) is None:
            raise UnknownTenantError(f"owner דורש tenant רשום (קיבלנו: {tenant_id!r})")
    else:
        tenant_id = None

    init_platform_db()
    with get_platform_connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM admin_users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            raise ValueError("משתמש עם האימייל הזה כבר קיים")
        conn.execute(
            "INSERT INTO admin_users (email, password_hash, display_name, role, tenant_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, generate_password_hash(password), display_name, role, tenant_id),
        )
    logger.info("admin user created: role=%s tenant=%s", role, tenant_id or "-")


def verify_admin_login(email: str, password: str) -> Optional[dict]:
    """אימות התחברות. מחזיר את רשומת המשתמש (בלי ה-hash) או None.

    תמיד מריץ בדיקת סיסמה (גם כשהמשתמש לא קיים / מושבת) — בלי timing
    oracle על קיום החשבון. None אחיד לכל סיבה — הקורא מציג הודעה גנרית
    (דפוס קריטי #10).
    """
    from werkzeug.security import check_password_hash

    try:
        email = _normalize_email(email)
    except ValueError:
        check_password_hash(_dummy_hash(), password or "")
        return None

    row = None
    if platform_db_path().exists():
        with get_platform_connection() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE email = ?", (email,)
            ).fetchone()

    if row is None:
        check_password_hash(_dummy_hash(), password or "")
        return None

    ok = check_password_hash(row["password_hash"], password or "")
    if not ok or row["status"] != "active":
        return None

    with get_platform_connection() as conn:
        conn.execute(
            "UPDATE admin_users SET last_login_at = datetime('now') WHERE email = ?",
            (email,),
        )
    user = dict(row)
    user.pop("password_hash", None)  # לעולם לא מחזירים hash החוצה (דפוס #6)
    return user


def list_admin_users(tenant_id: Optional[str] = None) -> list[dict]:
    """רשימת משתמשי האדמין — ללא ה-hash."""
    if not platform_db_path().exists():
        return []
    with get_platform_connection() as conn:
        if tenant_id:
            rows = conn.execute(
                "SELECT email, display_name, role, tenant_id, status, created_at, "
                "last_login_at FROM admin_users WHERE tenant_id = ? ORDER BY email",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT email, display_name, role, tenant_id, status, created_at, "
                "last_login_at FROM admin_users ORDER BY email"
            ).fetchall()
        return [dict(r) for r in rows]


def get_tenant_owner(tenant_id: str) -> Optional[dict]:
    """משתמש ה-owner של tenant — לתצוגה ולשינוי סיסמה.

    מחזיר dict בלי password_hash (דפוס #6), או None כשאין owner.
    אם רשומים כמה owners — מחזיר את הוותיק (הראשון שנוצר).
    """
    if not platform_db_path().exists():
        return None
    with get_platform_connection() as conn:
        row = conn.execute(
            "SELECT email, display_name, status FROM admin_users "
            "WHERE tenant_id = ? AND role = 'owner' "
            "ORDER BY created_at, email LIMIT 1",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else None


def set_admin_user_status(email: str, status: str) -> None:
    """הפעלה/השבתה של משתמש אדמין."""
    if status not in ("active", "disabled"):
        raise ValueError(f"סטטוס לא מוכר: {status!r}")
    email = _normalize_email(email)
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET status = ? WHERE email = ?", (status, email),
        )
        if cur.rowcount == 0:
            raise ValueError("משתמש לא קיים")


def set_admin_password(email: str, new_password: str) -> None:
    """שינוי סיסמת משתמש אדמין (בעל העסק משנה את הסיסמה שלו מהפאנל)."""
    from werkzeug.security import generate_password_hash

    if not new_password or len(new_password) < 8:
        raise ValueError("סיסמה קצרה מדי (מינימום 8 תווים)")
    email = _normalize_email(email)
    with get_platform_connection() as conn:
        cur = conn.execute(
            "UPDATE admin_users SET password_hash = ? WHERE email = ?",
            (generate_password_hash(new_password), email),
        )
        if cur.rowcount == 0:
            raise ValueError("משתמש לא קיים")
