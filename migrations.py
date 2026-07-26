"""
מיגרציות קלות ל-SQLite — נקראות מ-`init_db()` בכל עליית תהליך.

הועתק המנגנון מ-`ai-business-bot/migrations.py` (‏ROADMAP T0.5); רשימת
המיגרציות ההיסטוריות **לא** הועתקה — הריפו מתחיל מסכימה נקייה.

כללי הזהב (ראה CLAUDE.md → "DB"):
1. ‏SQLite תומך רק ב-ADD COLUMN. שינוי מורכב יותר (‏UNIQUE, שינוי טיפוס)
   דורש CREATE TABLE + INSERT + DROP — נכתב כאן, לא ב-executescript.
2. עמודה חדשה לטבלה **קיימת** נוספת אך ורק דרך `_ensure_column` כאן.
3. אינדקס או constraint שתלוי בעמודה כזו — **כאן בלבד**. אם ייכנס
   ל-executescript של `init_db`, ב-DB קיים הוא יקרוס: ה-executescript
   רץ *לפני* המיגרציות, ולכן העמודה עוד לא קיימת. הבאג עובר ב-CI על DB
   ריק ונתפס רק בפרודקשן.
4. ‏`control_plane.migrate_all_tenants()` מריץ את זה על כל tenant פעיל
   בכל עליית תהליך — בלעדיו סכימת ה-tenants הקיימים נרקבת בשקט.
"""

import logging

logger = logging.getLogger(__name__)


def _ensure_column(conn, table: str, column: str, ddl_suffix: str) -> None:
    """הוספת עמודה אם אינה קיימת (‏SQLite ADD COLUMN בלבד)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if any(r["name"] == column for r in cols):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_suffix}")
    logger.info("migration: added column %s.%s", table, column)


def _rebuild_owner_alert_targets(conn) -> None:
    """מעבר למפתח מורכב `(owner_chat_id, owner_message_id)`.

    הטבלה נוצרה בגרסה מוקדמת עם `owner_message_id` כמפתח יחיד. ‏message_id
    של טלגרם ייחודי **פר-צ'אט** ולא פר-בוט, ולכן ל-tenant עם שני חיבורים
    (חיבור מחדש מחשבון אחר) המפתח הישן מתנגש — ו-`/pause` בתגובה להתראה
    אחת היה משתיק את הלקוח של התראה אחרת.

    ‏SQLite אינו תומך בשינוי PRIMARY KEY, ולכן rebuild: טבלה חדשה,
    העתקה, החלפה. הכול בתוך הטרנזקציה של `get_connection` — או שהמעבר
    שלם, או שלא קרה.

    ה-DDL כאן **משוכפל** מ-`init_db` בכוונה: מיגרציה חייבת לתאר את
    הסכימה כפי שהייתה בזמן כתיבתה. אם `init_db` ישתנה בעתיד, המיגרציה
    הזו עדיין צריכה לייצר בדיוק את מה שהיא הבטיחה.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(owner_alert_targets)")}
    if not cols or "owner_chat_id" in cols:
        return  # הטבלה לא קיימת, או שהיא כבר בסכימה החדשה

    logger.info("migration: rebuilding owner_alert_targets with a composite key")
    conn.executescript("""
        CREATE TABLE owner_alert_targets_new (
            owner_chat_id    TEXT NOT NULL,
            owner_message_id INTEGER NOT NULL,
            user_id          TEXT NOT NULL,
            chat_id          TEXT NOT NULL,
            created_at       TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (owner_chat_id, owner_message_id)
        );
        -- ‏owner_chat_id ריק לרשומות הישנות: הצ'אט לא נשמר אז, ואין
        -- מאיפה להשלים אותו. תגובה להתראה ישנה תיפול ל-autopilot
        -- הגלובלי במקום להשתיק שיחה — התנהגות מוגדרת, לא שגויה.
        INSERT INTO owner_alert_targets_new
            (owner_chat_id, owner_message_id, user_id, chat_id, created_at)
        SELECT '', owner_message_id, user_id, chat_id, created_at
        FROM owner_alert_targets;
        DROP TABLE owner_alert_targets;
        ALTER TABLE owner_alert_targets_new RENAME TO owner_alert_targets;
        CREATE INDEX IF NOT EXISTS idx_alert_targets_user
            ON owner_alert_targets(user_id);
    """)


def run_migrations(conn) -> None:
    """הפעלת כל המיגרציות — נקראת מתוך `init_db()` עם חיבור פתוח."""
    _rebuild_owner_alert_targets(conn)

    # ── T4.1 — שיוך עובדת זיכרון להודעה שממנה נגזרה ──
    # עמודה לטבלה **קיימת**, ולכן כאן. בלעדיה `deleted_business_messages`
    # מוחק את ההודעה ומשאיר את מה שחולץ ממנה — כלומר חובת מחיקת
    # הנגזרות הופכת להצהרה.
    _ensure_column(
        conn, "customer_facts", "source_message_id",
        "INTEGER REFERENCES conversations(id) ON DELETE SET NULL",
    )
