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


def run_migrations(conn) -> None:
    """הפעלת כל המיגרציות — נקראת מתוך `init_db()` עם חיבור פתוח."""
    # ── T4.3 — שורת הגילוי ──
    # שלוש עמודות לשלוש טבלאות **קיימות**, ולכן כאן ולא ב-executescript:
    # ב-DB של tenant שכבר נוצר, ה-CREATE TABLE IF NOT EXISTS אינו רץ,
    # וכל כתיבה שמפנה לעמודות האלה הייתה נופלת על "no such column".
    _ensure_column(conn, "users", "disclosure_sent_at", "TEXT")
    _ensure_column(
        conn, "bot_settings", "disclosure_enabled", "INTEGER NOT NULL DEFAULT 1",
    )
    _ensure_column(conn, "bot_settings", "disclosure_template", "TEXT DEFAULT ''")
