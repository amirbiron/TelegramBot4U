"""
Database — אחסון SQLite פר-tenant: בסיס ידע, שיחות, משתמשים והשתקות.

תת-קבוצה של `ai-business-bot/database.py` (‏ROADMAP T0.5). מה שיצא:
‏kb_chunks (אין RAG), תורים, שידורים, הפניות, ‏Twilio/Meta, מצב חופשה.
מה שנוסף לערוץ החדש: ‏`conversations.authored_by` + מזהי ההודעה של
טלגרם (‏edited/deleted), ‏`users.last_inbound_at` (חלון 24ש') ו-
`live_chats.started_by`.

כלל מחייב: כל גישה ל-DB עוברת דרך `get_connection()`, שפותח את הקובץ לפי
ה-tenant הנוכחי (`tenancy.tenant_db_path()`). אסור `sqlite3.connect` ישיר.
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from tenancy import tenant_db_path

logger = logging.getLogger(__name__)

# ערוץ יחיד בפרויקט — נשמר בעמודת channel של conversations/users כדי
# שהנתונים יהיו מפורשים (ולא "telegram" גנרי שמתבלבל עם בוט רגיל).
CHANNEL = "telegram_business"


@contextmanager
def get_connection():
    """מחזיר חיבור SQLite ל-DB של ה-tenant הנוכחי, וסוגר אותו תמיד.

    נוצר חיבור חדש לכל פעולה, עם timeout נדיב ו-busy_timeout כדי לצמצם
    "database is locked" — הבוט (asyncio) והאדמין (Flask) רצים באותו
    תהליך. ‏check_same_thread=False נדרש כי Flask ו-asyncio משתמשים
    ב-threads שונים, אבל ה-connection עצמו *אינו* thread-safe: הבטיחות
    מובטחת ע"י context manager שפותח וסוגר בכל פעולה (בלי שיתוף).
    """
    conn = sqlite3.connect(str(tenant_db_path()), timeout=30, check_same_thread=False)
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


def init_db():
    """יצירת כל הטבלאות אם אינן קיימות, ואז הרצת המיגרציות.

    סדר ההרצה קריטי (ראה CLAUDE.md): ‏executescript תחילה, ואז
    `run_migrations`. ב-DB **קיים** ה-CREATE TABLE IF NOT EXISTS לא מוסיף
    עמודות — רק `_ensure_column` ב-migrations.py יוסיף אותן, ולכן כל
    אינדקס/constraint שתלוי בעמודה שנוספה ל-טבלה קיימת חייב לשבת שם.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.executescript("""
            -- ── בסיס ידע ────────────────────────────────────────────────
            -- אין kb_chunks: ה-KB נכנס לפרומפט בשלמותו (PLAN §3.2).
            CREATE TABLE IF NOT EXISTS kb_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT NOT NULL,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                metadata    TEXT DEFAULT '{}',
                is_active   INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            );

            -- מונה גרסה של בסיס הידע — מפתח ה-cache של kb_service.
            -- **למה לא MAX(updated_at):** ל-datetime('now') רזולוציה של
            -- שנייה. עריכה שמתרחשת באותה שנייה שבה נכתבה הרשומה (או שתי
            -- עריכות באותה שנייה) לא הייתה משנה את החתימה, וה-cache היה
            -- מגיש תוכן ישן. מונה מונוטוני חסין לזה לחלוטין.
            CREATE TABLE IF NOT EXISTS kb_meta (
                id       INTEGER PRIMARY KEY CHECK(id = 1),
                revision INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO kb_meta (id, revision) VALUES (1, 0);

            -- הבאמפ נעשה ב-triggers ולא בקוד הפייתון בכוונה: כך גם כתיבה
            -- ישירה (מיגרציה, seed, תיקון ידני, פיצ'ר עתידי) מעדכנת את
            -- הגרסה, ואי אפשר לשכוח.
            CREATE TRIGGER IF NOT EXISTS trg_kb_rev_insert
                AFTER INSERT ON kb_entries
            BEGIN
                UPDATE kb_meta SET revision = revision + 1 WHERE id = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_kb_rev_update
                AFTER UPDATE ON kb_entries
            BEGIN
                UPDATE kb_meta SET revision = revision + 1 WHERE id = 1;
            END;
            CREATE TRIGGER IF NOT EXISTS trg_kb_rev_delete
                AFTER DELETE ON kb_entries
            BEGIN
                UPDATE kb_meta SET revision = revision + 1 WHERE id = 1;
            END;

            -- ── היסטוריית שיחה ──────────────────────────────────────────
            -- authored_by: מי כתב את הודעת ה-assistant — הבוט או הבעלים
            --   (הבעלים עונה בעצמו ⇒ takeover, PLAN §4.3).
            -- tg_chat_id / tg_message_id: מזהי טלגרם של ההודעה המקורית —
            --   בלעדיהם אי אפשר לממש edited_business_message ו-
            --   deleted_business_messages (חובת פרטיות, PLAN §6).
            CREATE TABLE IF NOT EXISTS conversations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                username      TEXT DEFAULT '',
                role          TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                message       TEXT NOT NULL,
                sources       TEXT DEFAULT '',
                channel       TEXT DEFAULT 'telegram_business',
                authored_by   TEXT NOT NULL DEFAULT 'bot'
                              CHECK(authored_by IN ('bot', 'owner', 'customer')),
                tg_chat_id    INTEGER,
                tg_message_id INTEGER,
                created_at    TEXT DEFAULT (datetime('now'))
            );

            -- ── סיכומי שיחה (זיכרון ארוך-טווח) ──────────────────────────
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                     TEXT NOT NULL,
                summary_text                TEXT NOT NULL,
                message_count               INTEGER NOT NULL DEFAULT 0,
                last_summarized_message_id  INTEGER NOT NULL DEFAULT 0,
                created_at                  TEXT DEFAULT (datetime('now'))
            );

            -- ── משתמשי קצה (לקוחות העסק) ────────────────────────────────
            -- last_inbound_at: מעקב חלון 24 השעות (PLAN §1.4) — כל הודעה
            --   נכנסת מעדכנת אותו, והפאנל מציג לפיו אילו שיחות עוד ניתנות
            --   למענה.
            -- consecutive_fallbacks: ב-DB ולא ב-context.user_data, כי
            --   הערוץ הוא webhook חסר-מצב.
            -- send_failure_*: סיווג הכשל האחרון (חלון סגור / אין הרשאה /
            --   אחר) לתצוגה ולהתראה לבעלים.
            CREATE TABLE IF NOT EXISTS users (
                user_id               TEXT PRIMARY KEY,
                username              TEXT DEFAULT '',
                channel               TEXT DEFAULT 'telegram_business',
                chat_id               TEXT DEFAULT '',
                first_seen_at         TEXT DEFAULT (datetime('now')),
                last_active_at        TEXT DEFAULT (datetime('now')),
                last_inbound_at       TEXT,
                message_count         INTEGER DEFAULT 0,
                consecutive_fallbacks INTEGER DEFAULT 0,
                send_failure_reason   TEXT DEFAULT '',
                send_failure_at       TEXT
            );

            -- ── פערי ידע ────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS unanswered_questions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                username    TEXT DEFAULT '',
                question    TEXT NOT NULL,
                intent      TEXT DEFAULT '',
                channel     TEXT DEFAULT 'telegram_business',
                status      TEXT DEFAULT 'open'
                            CHECK(status IN ('open', 'resolved', 'not_relevant')),
                created_at  TEXT DEFAULT (datetime('now')),
                resolved_at TEXT
            );

            -- ── השתקה / takeover ────────────────────────────────────────
            -- המפתח הוא chat_id (PLAN §4.3): הבעלים עונה בצ'אט, לא
            -- "מצטרף למשתמש". started_by מתעד מה יצר את ההשתקה.
            CREATE TABLE IF NOT EXISTS live_chats (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL,
                user_id     TEXT DEFAULT '',
                username    TEXT DEFAULT '',
                is_active   INTEGER DEFAULT 1,
                started_by  TEXT NOT NULL DEFAULT 'owner_message'
                            CHECK(started_by IN ('owner_message', 'panel', 'handoff')),
                started_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now')),
                ended_at    TEXT
            );

            -- ── יעד ההתראה לבעלים ───────────────────────────────────────
            -- מיפוי: ההודעה ששלחנו לצ'אט הבעלים ⇐ הלקוח שהיא עוסקת בו.
            -- זה מה שמאפשר לבעלים לענות `/pause` **בתגובה** להתראה
            -- ולהשתיק את אותה שיחה בלבד. בלי המיפוי טלגרם נותנת לנו רק
            -- את message_id של ההודעה שהוא הגיב לה, ואין ממנו דרך חזרה
            -- ללקוח.
            --
            -- ‏natural key: ‏(owner_chat_id, owner_message_id). ‏message_id
            -- של טלגרם ייחודי **פר-צ'אט**, לא פר-בוט: ל-tenant יכולים
            -- להיות כמה חיבורים (חיבור מחדש מחשבון אחר), ואז שני צ'אטים
            -- של בעלים באותו DB — ומזהי ההודעות בהם מתנגשים. מפתח על
            -- ה-message_id בלבד היה גורם ל-`/pause` בתגובה להתראה אחת
            -- להשתיק את הלקוח של התראה אחרת לגמרי.
            CREATE TABLE IF NOT EXISTS owner_alert_targets (
                owner_chat_id    TEXT NOT NULL,
                owner_message_id INTEGER NOT NULL,
                user_id          TEXT NOT NULL,
                chat_id          TEXT NOT NULL,
                created_at       TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (owner_chat_id, owner_message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_alert_targets_user
                ON owner_alert_targets(user_id);

            -- ── משתמשים חסומים ──────────────────────────────────────────
            -- ה-row נשאר גם אחרי מחיקת נתונים (hold צר לאכיפה — אינטרס
            -- לגיטימי): רק block_category + blocked_at + appeal_contact.
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id                TEXT NOT NULL PRIMARY KEY,
                username               TEXT DEFAULT '',
                block_category         TEXT NOT NULL DEFAULT 'manual'
                                       CHECK(block_category IN
                                           ('abuse', 'spam', 'repeated_no_show', 'manual')),
                block_reason_internal  TEXT NOT NULL DEFAULT '',
                appeal_contact_method  TEXT NOT NULL DEFAULT '',
                blocked_at             TEXT DEFAULT (datetime('now'))
            );

            -- ── הגדרות ה-tenant (שורה בודדת — תמיד id=1) ───────────────
            -- שם העסק אינו כאן: מקורו display_name ב-control plane
            -- (מקור אמת יחיד, ראה config.get_business_config).
            CREATE TABLE IF NOT EXISTS bot_settings (
                id                    INTEGER PRIMARY KEY CHECK(id = 1),
                tone                  TEXT NOT NULL DEFAULT 'friendly'
                                      CHECK(tone IN ('none','friendly','formal','sales','luxury')),
                custom_phrases        TEXT DEFAULT '',
                custom_prompt         TEXT DEFAULT '',
                full_system_prompt    TEXT DEFAULT '',
                business_phone        TEXT DEFAULT '',
                business_address      TEXT DEFAULT '',
                business_website      TEXT DEFAULT '',
                -- הגדרות הערוץ: משפט הגישור ב-handoff, מענה למדיה,
                -- ומתג autopilot גלובלי (פקודות הבעלים /pause ו-/resume).
                handoff_bridge_message TEXT NOT NULL DEFAULT 'בודק ואחזור אליך בהקדם',
                media_bridge_message   TEXT NOT NULL DEFAULT
                                       'קיבלתי, אעבור על זה ואחזור אליך',
                autopilot_enabled      INTEGER NOT NULL DEFAULT 1,
                updated_at             TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO bot_settings (id) VALUES (1);

            -- ── זיכרון לקוחות ───────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS customer_facts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             TEXT NOT NULL,
                business_id         TEXT NOT NULL DEFAULT 'default',
                fact_type           TEXT NOT NULL
                                    CHECK(fact_type IN ('preference','personal_info',
                                                        'relationship','vocabulary','open_issue')),
                content             TEXT NOT NULL,
                confidence          REAL NOT NULL,
                source              TEXT NOT NULL DEFAULT 'inferred'
                                    CHECK(source IN ('inferred','business_owner')),
                requires_consent    INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL
                                    CHECK(status IN ('active','pending_approval','rejected',
                                                     'superseded','resolved')),
                evidence            TEXT DEFAULT '',
                superseded_by_id    INTEGER REFERENCES customer_facts(id) ON DELETE SET NULL,
                created_at          TEXT DEFAULT (datetime('now')),
                last_confirmed_at   TEXT DEFAULT (datetime('now')),
                access_count        INTEGER DEFAULT 0,
                resolved_at         TEXT,
                resolution_evidence TEXT,
                -- ההודעה שממנה נגזרה העובדה (‏conversations.id).
                -- **זה מה שהופך את חובת מחיקת הנגזרות לאכיפה ולא להצהרה:**
                -- בלי הקישור, `deleted_business_messages` מוחק את ההודעה
                -- ומשאיר את מה שחולץ ממנה. ‏NULL = עובדה שבעל העסק הזין
                -- ידנית, ואין לה מקור בהתכתבות.
                -- העמודה מוגדרת גם ב-migrations.py, ל-DB קיים.
                source_message_id   INTEGER REFERENCES conversations(id) ON DELETE SET NULL
            );

            -- ── פנקס הסכמות פסאודונימי (תיקון 13) ───────────────────────
            -- subject_hash הוא HMAC עם pepper נפרד מה-DB (utils/consent_ledger.py).
            CREATE TABLE IF NOT EXISTS consent_ledger (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_hash    TEXT NOT NULL,
                pepper_version  TEXT NOT NULL DEFAULT 'v1',
                channel         TEXT NOT NULL,
                category        TEXT NOT NULL CHECK(category IN ('consent', 'audit')),
                event_type      TEXT NOT NULL,
                consent_version INTEGER,
                event_at        TEXT NOT NULL DEFAULT (datetime('now')),
                metadata_json   TEXT NOT NULL DEFAULT '{}',
                compromised     INTEGER NOT NULL DEFAULT 0
            );

            -- תור retry לכתיבות ledger שנכשלו (DB נעול, pepper חסר).
            -- טבלה שאמורה להיות ריקה — רשומות בה = בעיה לפתור.
            CREATE TABLE IF NOT EXISTS ledger_write_retry (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json    TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT DEFAULT '',
                last_attempt_at TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- ── אינדקסים ────────────────────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_kb_entries_category ON kb_entries(category);
            CREATE INDEX IF NOT EXISTS idx_kb_entries_updated ON kb_entries(updated_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_conversations_user_created
                ON conversations(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_tg_msg
                ON conversations(tg_chat_id, tg_message_id);
            CREATE INDEX IF NOT EXISTS idx_conversation_summaries_user
                ON conversation_summaries(user_id);
            CREATE INDEX IF NOT EXISTS idx_live_chats_chat_active
                ON live_chats(chat_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_unanswered_questions_status
                ON unanswered_questions(status);
            CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at);
            CREATE INDEX IF NOT EXISTS idx_users_last_inbound ON users(last_inbound_at);
            CREATE INDEX IF NOT EXISTS idx_customer_facts_user_business
                ON customer_facts(user_id, business_id, status);
            -- partial UNIQUE: dedup ברמת DB ל-facts פעילים (natural key)
            CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_facts_active_unique
                ON customer_facts(user_id, business_id, fact_type, content)
                WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_consent_ledger_subject
                ON consent_ledger(subject_hash, event_at);
            CREATE INDEX IF NOT EXISTS idx_consent_ledger_purge
                ON consent_ledger(category, event_at);
        """)

        # מיגרציות קלות — בקובץ נפרד לקריאות
        from migrations import run_migrations

        run_migrations(conn)


# ─── עזרי SQL ────────────────────────────────────────────────────────────


def escape_like(value: str) -> str:
    """‏escaping של תווי wildcard ב-LIKE (‏`_`, ‏`%`) ושל תו ה-escape עצמו.

    דפוס קריטי #8 — קלט משתמש ב-LIKE בלי escaping מאפשר wildcard
    injection. הקורא חייב להוסיף `ESCAPE '\\'` לשאילתה.
    """
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


# ─── בסיס ידע ────────────────────────────────────────────────────────────


def add_kb_entry(category: str, title: str, content: str, metadata: dict = None) -> int:
    """הוספת רשומה לבסיס הידע. מחזיר את המזהה."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO kb_entries (category, title, content, metadata) VALUES (?, ?, ?, ?)",
            (category, title, content, json.dumps(metadata or {})),
        )
        return cursor.lastrowid


def update_kb_entry(entry_id: int, category: str, title: str, content: str,
                    metadata: dict = None) -> None:
    """עדכון רשומה קיימת. ‏updated_at מתעדכן — עליו נשען מפתח ה-cache
    של kb_service, ולכן העריכה נכנסת לתוקף בהודעה הבאה."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE kb_entries SET category=?, title=?, content=?, metadata=?, "
            "updated_at=datetime('now') WHERE id=?",
            (category, title, content, json.dumps(metadata or {}), entry_id),
        )


def delete_kb_entry(entry_id: int) -> None:
    """מחיקת רשומה מבסיס הידע."""
    with get_connection() as conn:
        conn.execute("DELETE FROM kb_entries WHERE id=?", (entry_id,))


def get_kb_entry(entry_id: int) -> Optional[dict]:
    """רשומה בודדת לפי מזהה."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM kb_entries WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None


def _kb_filter_sql(category: Optional[str], active_only: bool) -> tuple[str, list]:
    """‏WHERE משותף ל-get_all_kb_entries ול-count_kb_entries.

    ‏helper משותף בכוונה (CLAUDE.md): שכפול WHERE בין get ל-count מזמין
    סטייה שקטה כשמעדכנים רק אחד מהם.
    """
    query = " WHERE 1=1"
    params: list = []
    if active_only:
        query += " AND is_active=1"
    if category:
        query += " AND category=?"
        params.append(category)
    return query, params


def get_all_kb_entries(category: str = None, active_only: bool = True) -> list[dict]:
    """כל רשומות בסיס הידע, אופציונלית מסוננות לפי קטגוריה."""
    where, params = _kb_filter_sql(category, active_only)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM kb_entries" + where + " ORDER BY category, title", params
        ).fetchall()
        return [dict(r) for r in rows]


def count_kb_entries(category: str | None = None, active_only: bool = True) -> int:
    """ספירת רשומות בסיס הידע."""
    where, params = _kb_filter_sql(category, active_only)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM kb_entries" + where, params
        ).fetchone()
        return int(row["count"]) if row else 0


def get_kb_categories() -> list[str]:
    """הקטגוריות הקיימות בבסיס הידע הפעיל."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM kb_entries WHERE is_active=1 ORDER BY category"
        ).fetchall()
        return [r["category"] for r in rows]


def search_kb_entries(query: str, limit: int = 20) -> list[dict]:
    """חיפוש טקסטואלי (LIKE) בכותרת ובתוכן — מחליף את החיפוש הסמנטי.

    הקלט עובר escape_like ומועבר עם ‏`ESCAPE '\\'` (דפוס קריטי #8):
    בלי זה, ‏`%` שמשתמש מקליד היה סורק את כל הטבלה ו-`_` היה תופס כל תו.
    """
    q = (query or "").strip()
    if not q:
        return []
    pattern = f"%{escape_like(q)}%"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM kb_entries "
            "WHERE is_active=1 AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\') "
            "ORDER BY category, title, id LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_kb_version() -> str:
    """חתימת הגרסה של בסיס הידע — מפתח ה-cache של kb_service.

    מונה מונוטוני שה-triggers על `kb_entries` מעלים בכל INSERT/UPDATE/
    DELETE. **לא** ‏MAX(updated_at): הרזולוציה שם היא שנייה, ולכן שתי
    כתיבות באותה שנייה היו נראות זהות וה-cache היה מגיש תוכן ישן.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT revision FROM kb_meta WHERE id = 1").fetchone()
        return str(row["revision"]) if row else "0"


# ─── שיחות ───────────────────────────────────────────────────────────────


def save_message(
    user_id: str,
    username: str,
    role: str,
    message: str,
    sources: str = "",
    channel: str = CHANNEL,
    authored_by: str = "bot",
    tg_chat_id: Optional[int] = None,
    tg_message_id: Optional[int] = None,
) -> int:
    """שמירת הודעה בהיסטוריה. מחזיר את המזהה הפנימי.

    authored_by רלוונטי רק ל-role='assistant': ‏'bot' (אנחנו ענינו) מול
    'owner' (בעל העסק ענה בעצמו). ל-role='user' הערך 'customer'.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO conversations "
            "(user_id, username, role, message, sources, channel, authored_by, "
            " tg_chat_id, tg_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, role, message, sources, channel, authored_by,
             tg_chat_id, tg_message_id),
        )
        return cur.lastrowid


def get_conversation_history(user_id: str, limit: int = 20) -> list[dict]:
    """היסטוריית השיחה האחרונה של משתמש (בסדר כרונולוגי עולה)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT role, username, message, sources, authored_by, created_at "
            "FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_all_conversations(limit: int = 100) -> list[dict]:
    """כל ההודעות האחרונות — לתצוגת הפאנל."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, username, role, message, sources, authored_by, created_at "
            "FROM conversations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def update_message_by_tg_id(tg_chat_id: int, tg_message_id: int, new_text: str) -> int:
    """עדכון העותק השמור אחרי edited_business_message. מחזיר כמה שורות עודכנו."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE conversations SET message = ? WHERE tg_chat_id = ? AND tg_message_id = ?",
            (new_text, tg_chat_id, tg_message_id),
        )
        return cur.rowcount or 0


def delete_messages_by_tg_ids(tg_chat_id: int, tg_message_ids: list[int]) -> dict:
    """מחיקת העותקים **והנגזרות** אחרי deleted_business_messages.

    חובת פרטיות ולא אופציה (‏PLAN §6, ‏CLAUDE.md): טלגרם הודיעה שהתוכן
    נמחק אצל המשתמש, ולכן העותק שלנו חייב להימחק מיד — **כולל מה שנגזר
    ממנו**. מחיקת שורת ההודעה בלבד משאירה את התוכן חי בשני מקומות:

    1. **עובדות זיכרון** שחולצו ממנה (`customer_facts.source_message_id`).
    2. **סיכום השיחה**, אם ההודעה כבר סוכמה. אי אפשר לנתח סיכום שנוצר
       ע"י LLM ולהוציא ממנו את תרומתה של הודעה אחת, ולכן הסיכום נמחק
       כולו. זה מאבד זיכרון ארוך-טווח על הלקוח — וזו העלות הנכונה:
       ה-high-water mark מתאפס, וסיכום חדש ייבנה מההודעות ששרדו.

    מחזיר counts פר-סוג. הכול בטרנזקציה אחת: מחיקה חלקית היא בדיוק המצב
    שאסור להישאר בו.
    """
    ids = [int(i) for i in (tg_message_ids or [])]
    result = {"conversations": 0, "customer_facts": 0, "summaries": 0}
    if not ids:
        return result

    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, user_id FROM conversations WHERE tg_chat_id = ? "
            f"AND tg_message_id IN ({placeholders})",
            (tg_chat_id, *ids),
        ).fetchall()
        if not rows:
            return result

        row_ids = [r["id"] for r in rows]
        user_ids = {r["user_id"] for r in rows}
        row_ph = ",".join("?" * len(row_ids))

        # 1 — עובדות זיכרון שנגזרו מההודעות האלה
        cur = conn.execute(
            f"DELETE FROM customer_facts WHERE source_message_id IN ({row_ph})",
            row_ids,
        )
        result["customer_facts"] = cur.rowcount or 0

        # 2 — הסיכום, אם ההודעה נכנסה אליו. מחיקה גורפת פר-משתמש ולא
        #     לפי high-water mark: הסיכום ממוזג רקורסיבית, ולכן גם תוכן
        #     ישן ממשיך לחיות בו אחרי שהמונה התקדם.
        for user_id in user_ids:
            cur = conn.execute(
                "DELETE FROM conversation_summaries WHERE user_id = ?", (user_id,),
            )
            result["summaries"] += cur.rowcount or 0

        # 3 — ההודעות עצמן, **אחרונות**: מחיקתן קודם הייתה מותירה את
        #     ה-ids של הנגזרות בלי מקור לשייך אליו.
        cur = conn.execute(
            f"DELETE FROM conversations WHERE id IN ({row_ph})", row_ids,
        )
        result["conversations"] = cur.rowcount or 0

    logger.info("deleted_business_messages: נמחקו %s", result)
    return result


def invalidate_summary_for_message(tg_chat_id: int, tg_message_id: int) -> bool:
    """מחיקת הסיכום אם ההודעה שנערכה כבר נכנסה אליו.

    ‏`edited_business_message` מעדכן את העותק ב-`conversations`, אבל אם
    ההודעה כבר סוכמה, **הנוסח הישן ממשיך לחיות בתוך הסיכום** — והוא זה
    שנשלח ל-LLM בכל פנייה. לקוח שערך הודעה כדי להסיר ממנה פרט שמסר
    בטעות היה ממשיך לראות אותו משפיע על התשובות.

    יורה רק כשההודעה כבר סוכמה. עריכה של הודעה טרייה אינה דורשת כלום —
    הנוסח המעודכן ייכנס לסיכום הבא ממילא.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, user_id FROM conversations "
            "WHERE tg_chat_id = ? AND tg_message_id = ?",
            (int(tg_chat_id), int(tg_message_id)),
        ).fetchone()
        if row is None:
            return False
        if row["id"] > _last_summarized_message_id(conn, row["user_id"]):
            return False
        cur = conn.execute(
            "DELETE FROM conversation_summaries WHERE user_id = ?", (row["user_id"],),
        )
    if cur.rowcount:
        logger.info("edited_business_message: הסיכום בוטל — ההודעה כבר נכללה בו")
    return bool(cur.rowcount)


def get_unique_users() -> list[dict]:
    """רשימת המשתמשים לתצוגת "שיחות" בפאנל."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, username, chat_id, last_active_at, last_inbound_at, "
            "message_count, send_failure_reason FROM users ORDER BY last_active_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def count_unique_users() -> int:
    """כמה משתמשי קצה מוכרים ל-tenant."""
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
        return int(row["c"]) if row else 0


# ─── משתמשי קצה ──────────────────────────────────────────────────────────


def upsert_user(
    user_id: str,
    username: str = "",
    chat_id: str = "",
    channel: str = CHANNEL,
    inbound: bool = False,
) -> None:
    """יצירה/עדכון של שורת המשתמש.

    inbound=True — הודעה נכנסת מהלקוח: מעדכן גם `last_inbound_at` (חלון
    24 השעות, ‏PLAN §1.4) ומאפס את סימון כשל השליחה, כי הצ'אט התעורר.
    """
    now_sql = "datetime('now')"
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO users (user_id, username, chat_id, channel, message_count, "
            f"        last_active_at, last_inbound_at) "
            f"VALUES (?, ?, ?, ?, 1, {now_sql}, CASE WHEN ? THEN {now_sql} ELSE NULL END) "
            f"ON CONFLICT(user_id) DO UPDATE SET "
            f"  username = CASE WHEN excluded.username != '' THEN excluded.username "
            f"                  ELSE users.username END, "
            f"  chat_id = CASE WHEN excluded.chat_id != '' THEN excluded.chat_id "
            f"                 ELSE users.chat_id END, "
            f"  last_active_at = {now_sql}, "
            f"  message_count = users.message_count + 1, "
            f"  last_inbound_at = CASE WHEN ? THEN {now_sql} ELSE users.last_inbound_at END, "
            f"  send_failure_reason = CASE WHEN ? THEN '' ELSE users.send_failure_reason END, "
            f"  send_failure_at = CASE WHEN ? THEN NULL ELSE users.send_failure_at END",
            (user_id, username, chat_id, channel,
             1 if inbound else 0, 1 if inbound else 0,
             1 if inbound else 0, 1 if inbound else 0),
        )


def get_user(user_id: str) -> Optional[dict]:
    """שורת המשתמש, או None."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def user_exists(user_id: str) -> bool:
    """האם המשתמש מוכר לנו (שורה ב-users)."""
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None


def get_consecutive_fallbacks(user_id: str) -> int:
    """מונה ה-fallbacks הרצופים של המשתמש (‏0 כשאין שורה)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT consecutive_fallbacks FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return int(row["consecutive_fallbacks"]) if row else 0


def set_consecutive_fallbacks(user_id: str, count: int) -> None:
    """עדכון מונה ה-fallbacks (‏0 = איפוס אחרי תשובה מוצלחת)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET consecutive_fallbacks = ? WHERE user_id = ?",
            (int(count), user_id),
        )


def mark_send_failure(user_id: str, reason: str) -> None:
    """סימון כשל שליחה מסווג (‏window_closed / no_permission / other).

    לא retry עיוור (PLAN §1.4): הסימון נשמר, הבעלים מקבל התראה, והפאנל
    מציג שהשיחה לא ניתנת למענה עד שהלקוח יכתוב שוב.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET send_failure_reason = ?, send_failure_at = datetime('now') "
            "WHERE user_id = ?",
            (reason, user_id),
        )


def is_within_reply_window(user_id: str, hours: int = 24) -> bool:
    """האם הצ'אט היה פעיל בחלון האחרון — כלומר מותר לשלוח בשם הבעלים.

    אינדיקציה בלבד לתצוגה ולהחלטות יזומות; הסמכות הסופית היא תשובת ה-API
    (‏V5 — הניסוח "בכפוף להגדרות החיבור" בתיעוד טרם אומת אמפירית).
    """
    row = get_user(user_id)
    if not row or not row.get("last_inbound_at"):
        return False
    try:
        last = datetime.strptime(row["last_inbound_at"], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        logger.error("is_within_reply_window: last_inbound_at לא תקין למשתמש %s", user_id)
        return False
    last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last < timedelta(hours=hours)


# ─── סיכומי שיחה ─────────────────────────────────────────────────────────


def _last_summarized_message_id(conn, user_id: str) -> int:
    """ה-high-water mark של הסיכום האחרון (‏0 כשאין)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(last_summarized_message_id), 0) AS m "
        "FROM conversation_summaries WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return int(row["m"]) if row else 0


def get_unsummarized_message_count(user_id: str) -> int:
    """כמה הודעות טרם סוכמו."""
    with get_connection() as conn:
        last_id = _last_summarized_message_id(conn, user_id)
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM conversations WHERE user_id = ? AND id > ?",
            (user_id, last_id),
        ).fetchone()
        return int(row["c"]) if row else 0


def get_messages_for_summarization(user_id: str, limit: int) -> list[dict]:
    """ההודעות הבאות לסיכום (הישנות ביותר שטרם סוכמו)."""
    with get_connection() as conn:
        last_id = _last_summarized_message_id(conn, user_id)
        rows = conn.execute(
            "SELECT id, role, message FROM conversations "
            "WHERE user_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (user_id, last_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def save_conversation_summary(
    user_id: str, summary_text: str, message_count: int,
    last_summarized_message_id: int = 0,
) -> None:
    """שמירת סיכום ממוזג — מחליף את הקודמים (סיכום רקורסיבי, שורה אחת)."""
    with get_connection() as conn:
        conn.execute("DELETE FROM conversation_summaries WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT INTO conversation_summaries "
            "(user_id, summary_text, message_count, last_summarized_message_id) "
            "VALUES (?, ?, ?, ?)",
            (user_id, summary_text, message_count, last_summarized_message_id),
        )


def get_latest_summary(user_id: str) -> Optional[dict]:
    """הסיכום העדכני של המשתמש, או None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM conversation_summaries WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


# ─── פערי ידע ────────────────────────────────────────────────────────────


def save_unanswered_question(
    user_id: str, username: str, question: str,
    intent: str = "", channel: str = CHANNEL,
) -> int:
    """רישום שאלה שהבוט לא ידע לענות עליה (טריגר: זיהוי [HANDOFF])."""
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO unanswered_questions (user_id, username, question, intent, channel) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, question, intent, channel),
        )
        return cur.lastrowid


def _unanswered_filter_sql(status: Optional[str]) -> tuple[str, list]:
    """‏WHERE משותף ל-get/count של פערי ידע (helper משותף — CLAUDE.md)."""
    if status:
        return " WHERE status = ?", [status]
    return "", []


def get_unanswered_questions(status: str | None = None, limit: int | None = None) -> list[dict]:
    """פערי ידע, אופציונלית מסוננים לפי סטטוס."""
    where, params = _unanswered_filter_sql(status)
    sql = "SELECT * FROM unanswered_questions" + where + " ORDER BY id DESC"
    if limit:
        sql += " LIMIT ?"
        params = params + [limit]
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def count_unanswered_questions(status: str | None = None) -> int:
    """ספירת פערי ידע לפי אותו סינון."""
    where, params = _unanswered_filter_sql(status)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM unanswered_questions" + where, params
        ).fetchone()
        return int(row["c"]) if row else 0


def get_unanswered_question(question_id: int) -> Optional[dict]:
    """פער ידע בודד."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM unanswered_questions WHERE id = ?", (question_id,)
        ).fetchone()
        return dict(row) if row else None


def update_unanswered_question_status(question_id: int, status: str) -> None:
    """עדכון סטטוס פער ידע (open / resolved / not_relevant)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE unanswered_questions SET status = ?, "
            "resolved_at = CASE WHEN ? = 'open' THEN NULL ELSE datetime('now') END "
            "WHERE id = ?",
            (status, status, question_id),
        )


# ─── השתקה / takeover ────────────────────────────────────────────────────


def start_live_chat(
    chat_id: str, user_id: str = "", username: str = "",
    started_by: str = "owner_message",
) -> int:
    """פתיחת השתקה לצ'אט. אם כבר פעילה — רק מחדש את `updated_at`.

    מחזיר את מזהה ה-session הפעיל.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM live_chats WHERE chat_id = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE live_chats SET updated_at = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO live_chats (chat_id, user_id, username, started_by) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, user_id, username, started_by),
        )
        return cur.lastrowid


def touch_live_chat(chat_id: str) -> None:
    """חידוש ה-timeout של ההשתקה (כל הודעת בעלים נוספת)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE live_chats SET updated_at = datetime('now') "
            "WHERE chat_id = ? AND is_active = 1",
            (chat_id,),
        )


def end_live_chat(chat_id: str) -> None:
    """סיום ההשתקה — הבוט חוזר לענות. שקט מוחלט כלפי הלקוח."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE live_chats SET is_active = 0, ended_at = datetime('now') "
            "WHERE chat_id = ? AND is_active = 1",
            (chat_id,),
        )


def get_active_live_chat(chat_id: str) -> Optional[dict]:
    """ה-session הפעיל של הצ'אט, או None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM live_chats WHERE chat_id = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def is_live_chat_active(chat_id: str) -> bool:
    """האם הצ'אט מושתק כרגע (בלי בדיקת timeout — זו באחריות השירות)."""
    return get_active_live_chat(chat_id) is not None


def get_all_active_live_chats() -> list[dict]:
    """כל ההשתקות הפעילות — לתצוגת הפאנל ולפקודת /status."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM live_chats WHERE is_active = 1 ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def count_active_live_chats() -> int:
    """כמה שיחות מושתקות כרגע."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM live_chats WHERE is_active = 1"
        ).fetchone()
        return int(row["c"]) if row else 0


def end_expired_live_chats(timeout_minutes: int) -> int:
    """סגירת השתקות שלא עודכנו יותר מ-timeout_minutes. מחזיר כמה נסגרו.

    השוואה ב-SQL מול datetime('now') — שני הצדדים UTC, ולכן אין תלות
    באזור הזמן של התהליך.
    """
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE live_chats SET is_active = 0, ended_at = datetime('now') "
            "WHERE is_active = 1 "
            "AND updated_at <= datetime('now', ?)",
            (f"-{int(timeout_minutes)} minutes",),
        )
        return cur.rowcount or 0


def cleanup_stale_live_chats() -> int:
    """סגירת כל ההשתקות בעליית תהליך — מונע לקוחות שנשארו מושתקים לנצח."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE live_chats SET is_active = 0, ended_at = datetime('now') "
            "WHERE is_active = 1"
        )
        return cur.rowcount or 0


# ─── חסימות ──────────────────────────────────────────────────────────────


def block_user(
    user_id: str, username: str = "", block_category: str = "manual",
    block_reason_internal: str = "", appeal_contact_method: str = "",
) -> None:
    """חסימת משתמש קצה (הבוט מתעלם ממנו לחלוטין — בלי הודעה)."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blocked_users "
            "(user_id, username, block_category, block_reason_internal, appeal_contact_method) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, block_category, block_reason_internal, appeal_contact_method),
        )


def unblock_user(user_id: str) -> None:
    """הסרת חסימה."""
    with get_connection() as conn:
        conn.execute("DELETE FROM blocked_users WHERE user_id = ?", (user_id,))


def is_user_blocked(user_id: str) -> bool:
    """האם המשתמש חסום."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None


def get_blocked_users() -> list[dict]:
    """רשימת החסומים לפאנל."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM blocked_users ORDER BY blocked_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── הגדרות ה-tenant ─────────────────────────────────────────────────────

_SETTINGS_COLUMNS = (
    "tone", "custom_phrases", "custom_prompt", "full_system_prompt",
    "business_phone", "business_address", "business_website",
    "handoff_bridge_message", "media_bridge_message", "autopilot_enabled",
)


def get_bot_settings() -> dict:
    """שורת ההגדרות (תמיד id=1). ‏dict ריק אם הטבלה חסרה/פגומה."""
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM bot_settings WHERE id = 1").fetchone()
            return dict(row) if row else {}
    except Exception:
        logger.error("get_bot_settings: כשל בקריאת ההגדרות", exc_info=True)
        return {}


def update_bot_settings(**kwargs) -> None:
    """עדכון חלקי של ההגדרות — רק עמודות מוכרות (הגנת whitelist)."""
    fields = {k: v for k, v in kwargs.items() if k in _SETTINGS_COLUMNS}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE bot_settings SET {assignments}, updated_at = datetime('now') WHERE id = 1",
            tuple(fields.values()),
        )


def is_autopilot_enabled() -> bool:
    """האם הבוט עונה בכלל (פקודות /pause ו-/resume של הבעלים)."""
    return bool(get_bot_settings().get("autopilot_enabled", 1))


def set_autopilot_enabled(enabled: bool) -> None:
    """הדלקה/כיבוי של ה-autopilot הגלובלי (‏`/pause`, ‏`/resume`)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE bot_settings SET autopilot_enabled = ?, "
            "updated_at = datetime('now') WHERE id = 1",
            (1 if enabled else 0,),
        )
    logger.info("autopilot %s", "הודלק" if enabled else "כובה")


# ─── מיפוי התראה לבעלים ⇐ לקוח ───────────────────────────────────────────


def record_owner_alert_target(
    owner_message_id: int, user_id: str, chat_id: str, owner_chat_id: str = "",
) -> None:
    """שמירת היעד של התראה שנשלחה לבעלים, לצורך `/pause` בתגובה.

    ‏`owner_chat_id` הוא חלק מהמפתח: ‏message_id ייחודי פר-צ'אט בלבד
    (ראה ההערה בסכימה). ‏`INSERT OR REPLACE` כי טלגרם ממחזרת מזהים
    לאורך זמן, וכתיבה חוזרת עדיפה על כשל.
    """
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO owner_alert_targets "
            "(owner_chat_id, owner_message_id, user_id, chat_id) VALUES (?, ?, ?, ?)",
            (str(owner_chat_id), int(owner_message_id), str(user_id), str(chat_id)),
        )


def get_owner_alert_target(
    owner_message_id: int, owner_chat_id: str = "",
) -> dict | None:
    """הלקוח שההתראה עסקה בו, או None אם ההודעה אינה התראה שלנו."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT user_id, chat_id FROM owner_alert_targets "
            "WHERE owner_chat_id = ? AND owner_message_id = ?",
            (str(owner_chat_id), int(owner_message_id)),
        ).fetchone()
        return dict(row) if row else None


# ה-retention של הטבלה הזו חי ב-`purge_old_data` יחד עם כל השאר, ולא
# בפונקציה נפרדת: שני מקומות שמוחקים מאותה טבלה מזמינים סטייה שקטה
# כשמעדכנים רק אחד (‏CLAUDE.md → DB).


# ─── מונים ל-digest היומי ────────────────────────────────────────────────


def get_activity_counts(hours: int = 24) -> dict[str, int]:
    """מונים לחלון האחרון — הבסיס ל-digest של הבעלים.

    ‏`answered` — תשובות שהבוט שלח בפועל (‏`authored_by='bot'`), לא
    הודעות נכנסות: זה מה שהבעלים רוצה לדעת שקרה בשמו.
    ‏`waiting` — שיחות שמושתקות בגלל handoff, כלומר ממתינות לו ממש.
    """
    window = f"-{int(hours)} hours"
    with get_connection() as conn:
        def _count(sql: str, params: tuple = ()) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row["c"]) if row else 0

        return {
            "answered": _count(
                "SELECT COUNT(*) AS c FROM conversations WHERE authored_by = 'bot' "
                "AND created_at >= datetime('now', ?)", (window,),
            ),
            "incoming": _count(
                "SELECT COUNT(*) AS c FROM conversations WHERE authored_by = 'customer' "
                "AND created_at >= datetime('now', ?)", (window,),
            ),
            "customers": _count(
                "SELECT COUNT(DISTINCT user_id) AS c FROM conversations "
                "WHERE created_at >= datetime('now', ?)", (window,),
            ),
            "waiting": _count(
                "SELECT COUNT(*) AS c FROM live_chats WHERE is_active = 1 "
                "AND started_by = 'handoff'",
            ),
            "silenced": _count(
                "SELECT COUNT(*) AS c FROM live_chats WHERE is_active = 1",
            ),
            "gaps": _count(
                "SELECT COUNT(*) AS c FROM unanswered_questions WHERE status = 'open' "
                "AND created_at >= datetime('now', ?)", (window,),
            ),
        }


# ─── זיכרון לקוחות ───────────────────────────────────────────────────────


def get_customer_facts(
    user_id: str, business_id: str = "default", status: str = "active",
) -> list[dict]:
    """עובדות הזיכרון של לקוח, לפי סטטוס.

    המיון הסופי (כולל tiebreaker) נעשה ב-memory/context.py — כאן רק
    שליפה יציבה עם tiebreaker על id.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM customer_facts "
            "WHERE user_id = ? AND business_id = ? AND status = ? "
            "ORDER BY confidence DESC, last_confirmed_at DESC, id DESC",
            (user_id, business_id, status),
        ).fetchall()
        return [dict(r) for r in rows]


def add_customer_fact(
    user_id: str, fact_type: str, content: str, confidence: float,
    business_id: str = "default", source: str = "inferred",
    requires_consent: bool = False, status: str = "active", evidence: str = "",
    source_message_id: int | None = None,
) -> Optional[int]:
    """הוספת עובדת זיכרון. מחזיר את המזהה, או None אם כבר קיימת זהה
    (‏partial UNIQUE על facts פעילים — dedup ברמת DB)."""
    try:
        with get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO customer_facts "
                "(user_id, business_id, fact_type, content, confidence, source, "
                " requires_consent, status, evidence, source_message_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, business_id, fact_type, content, confidence, source,
                 1 if requires_consent else 0, status, evidence, source_message_id),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        logger.info("add_customer_fact: עובדה זהה כבר קיימת — מדלגים")
        return None


def delete_customer_facts_for_user(user_id: str, business_id: str = "default") -> int:
    """מחיקת עובדות הזיכרון של לקוח (נגזרת של deleted_business_messages
    ושל זכות המחיקה). מחזיר כמה נמחקו."""
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM customer_facts WHERE user_id = ? AND business_id = ?",
            (user_id, business_id),
        )
        return cur.rowcount or 0


# ─── זכות מחיקה ──────────────────────────────────────────────────────────

# הטבלאות שמחזיקות user_id ונמחקות בזכות המחיקה. **כל טבלה חדשה עם
# user_id חייבת להתווסף כאן באותו commit** — הטסט הלולאתי על סכימת ה-DB
# (tests/test_privacy_delete.py) נכשל אחרת.
_USER_DATA_TABLES = (
    "conversations",
    "conversation_summaries",
    "unanswered_questions",
    "live_chats",
    "customer_facts",
    "owner_alert_targets",
)

# טבלאות עם user_id שנשארות בכוונה: blocked_users — hold צר לאכיפה
# (אינטרס לגיטימי). מתועד ב-docs/privacy_data_matrix.md.
_USER_DATA_TABLES_RETAINED = ("blocked_users",)

_deletion_in_progress: set = set()
_deletion_lock = threading.Lock()


def _deletion_key(user_id: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), user_id)


def delete_user_data(user_id: str) -> dict:
    """מחיקה מלאה של מידע משתמש קצה — זכות המחיקה.

    מחזיר dict של counts פר-טבלה, או `{"already_in_progress": True}` אם
    בקשה זהה כבר בעיבוד (‏idempotency מול קליקים/הודעות כפולות).

    נכתב ל-consent_ledger: ‏deletion_requested לפני, ואז
    deletion_completed (status=full/partial) או deletion_failed.
    """
    key = _deletion_key(user_id)
    with _deletion_lock:
        if key in _deletion_in_progress:
            logger.info("delete_user_data: בקשה כבר בעיבוד — מדלגים")
            return {"already_in_progress": True}
        _deletion_in_progress.add(key)
    try:
        return _delete_user_data_impl(user_id)
    finally:
        with _deletion_lock:
            _deletion_in_progress.discard(key)


def _delete_user_data_impl(user_id: str) -> dict:
    """המימוש בפועל — מופרד כדי שה-finally של ה-idempotency יעטוף הכול."""
    from utils.consent_ledger import (
        EVENT_DELETION_COMPLETED,
        EVENT_DELETION_FAILED,
        EVENT_DELETION_REQUESTED,
        record_consent_event,
    )

    try:
        record_consent_event(
            user_id=user_id, channel=CHANNEL, event_type=EVENT_DELETION_REQUESTED,
        )
    except Exception:
        logger.error("delete_user_data: כשל ברישום deletion_requested", exc_info=True)

    counts: dict[str, int] = {}
    failed_tables: list[str] = []
    errors: list[str] = []

    # לולאת I/O על רשימת טבלאות — try/except פר-פריט (CLAUDE.md): כשל
    # בטבלה אחת לא מונע את מחיקת השאר.
    with get_connection() as conn:
        for table in (*_USER_DATA_TABLES, "users"):
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                if cur.rowcount:
                    counts[table] = cur.rowcount
            except Exception as exc:
                failed_tables.append(table)
                errors.append(f"{table}: {type(exc).__name__}")
                logger.error("delete_user_data: שגיאה ב-%s", table, exc_info=True)

    logger.info(
        "delete_user_data: counts=%s failed=%s", counts, failed_tables,
    )

    try:
        if not counts and failed_tables:
            record_consent_event(
                user_id=user_id, channel=CHANNEL, event_type=EVENT_DELETION_FAILED,
                metadata={"counts": {}, "failed_tables": failed_tables, "errors": errors},
            )
        else:
            metadata: dict = {
                "status": "partial" if failed_tables else "full",
                "counts": counts,
                "total": sum(counts.values()),
            }
            if failed_tables:
                metadata["failed_tables"] = failed_tables
                metadata["errors"] = errors
            record_consent_event(
                user_id=user_id, channel=CHANNEL,
                event_type=EVENT_DELETION_COMPLETED, metadata=metadata,
            )
    except Exception:
        logger.error("delete_user_data: כשל ברישום תוצאת המחיקה ל-ledger", exc_info=True)

    result: dict = dict(counts)
    if failed_tables:
        result["__failed_tables__"] = list(failed_tables)
        result["__deletion_status__"] = "partial" if counts else "failed"
    return result


def purge_old_data(
    conversation_days: int = 365,
    consent_ledger_years: int = 5,
    audit_months: int = 24,
    alert_target_days: int = 30,
) -> dict:
    """‏retention אוטומטי — מחיקת נתונים ישנים לפי המדיניות.

    שיחות מעל conversation_days, ‏ledger מקטגוריית consent מעל
    consent_ledger_years, ומקטגוריית audit מעל audit_months.
    כל מחיקה בנפרד עם לוג — כשל באחת לא עוצר את השאר.
    """
    result: dict[str, int] = {}
    deletions = (
        ("conversations",
         "DELETE FROM conversations WHERE created_at < datetime('now', ?)",
         (f"-{int(conversation_days)} days",)),
        ("conversation_summaries",
         "DELETE FROM conversation_summaries WHERE created_at < datetime('now', ?)",
         (f"-{int(conversation_days)} days",)),
        ("consent_ledger_consent",
         "DELETE FROM consent_ledger WHERE category = 'consent' AND event_at < datetime('now', ?)",
         (f"-{int(consent_ledger_years)} years",)),
        ("consent_ledger_audit",
         "DELETE FROM consent_ledger WHERE category = 'audit' AND event_at < datetime('now', ?)",
         (f"-{int(audit_months)} months",)),
        ("owner_alert_targets",
         "DELETE FROM owner_alert_targets WHERE created_at < datetime('now', ?)",
         (f"-{int(alert_target_days)} days",)),
    )
    for label, sql, params in deletions:
        try:
            with get_connection() as conn:
                cur = conn.execute(sql, params)
                result[label] = cur.rowcount or 0
        except Exception:
            logger.error("purge_old_data: כשל ב-%s", label, exc_info=True)
            result[label] = -1

    # ניקוז תור ה-retry של ה-ledger. בלי קורא, `ledger_write_retry` היה
    # מצטבר לנצח — וההבטחה במטריצת הפרטיות שהוא טבלה זמנית (עם user_id
    # גלוי בתוך ה-payload!) לא הייתה מתקיימת. ה-job היומי הזה הוא הקורא.
    try:
        from utils.consent_ledger import process_ledger_retry_queue

        for key, value in process_ledger_retry_queue().items():
            result[f"ledger_retry_{key}"] = value
    except Exception:
        logger.error("purge_old_data: כשל בניקוז תור ה-ledger", exc_info=True)
        result["ledger_retry_failed"] = -1

    logger.info("purge_old_data: %s", result)
    return result


def get_dashboard_counts() -> dict[str, int]:
    """מונים לתצוגת הפאנל."""
    with get_connection() as conn:
        def _count(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row["c"]) if row else 0

        return {
            "kb_entries": _count("SELECT COUNT(*) AS c FROM kb_entries WHERE is_active=1"),
            "users": _count("SELECT COUNT(*) AS c FROM users"),
            "open_gaps": _count(
                "SELECT COUNT(*) AS c FROM unanswered_questions WHERE status='open'"
            ),
            "active_takeovers": _count(
                "SELECT COUNT(*) AS c FROM live_chats WHERE is_active=1"
            ),
        }
