"""
‏takeover — הבעלים לא "מצטרף לשיחה", הוא פשוט עונה.

ההיפוך המרכזי מול `live_chat_service` של הריפו המקור (‏PLAN §4.3):

| היבט | הריפו המקור | כאן |
|---|---|---|
| טריגר | כפתור בפאנל | הודעת בעלים אנושית בצ'אט |
| הודעות מעבר ללקוח | "בעל העסק הצטרף" / "הבוט חזר" | **אין. מעברים שקטים** |
| מפתח | `user_id` | `chat_id` (‏פר-tenant, ה-DB מבודד) |
| חזרת הבוט | ידנית או timeout | אותו מנגנון; כל הודעת בעלים מחדשת |

הלקוח לעולם לא יודע שמשהו קרה. זה כל העניין: הוא חושב שהוא מדבר עם
אדם, ואדם לא מכריז על עצמו שהוא נכנס לשיחה.

**היקף שלב 1:** הודעת בעלים משתיקה את הצ'אט, ה-timeout מחזיר את הבוט.
שלב 3 מוסיף: כניסה מהפאנל, כניסה מ-handoff (ממתין-לבעלים), ופקודות
`/pause` ו-`/resume`.
"""

from __future__ import annotations

import logging

import database as db

logger = logging.getLogger(__name__)


def timeout_minutes() -> int:
    """‏timeout ההשתקה — נקרא בזמן ריצה כדי לכבד patches ושינויי env."""
    import config as _cfg

    return getattr(_cfg, "TAKEOVER_TIMEOUT_MINUTES", 120)



def on_owner_message(chat_id: str, user_id: str = "", username: str = "") -> None:
    """הבעלים כתב בצ'אט — משתיקים את הבוט ומחדשים את ה-timeout.

    אידמפוטנטי: `start_live_chat` מזהה session פעיל ורק מרענן אותו.
    """
    db.start_live_chat(
        str(chat_id), user_id=str(user_id), username=username,
        started_by="owner_message",
    )
    logger.info("takeover: הצ'אט הושתק בעקבות הודעת בעלים")


def is_paused(chat_id: str) -> bool:
    """האם הצ'אט מושתק כרגע.

    בדיקת ה-timeout נעשית כאן ולא ב-DB: session שלא עודכן מעבר לסף
    נסגר בשקט, והבוט חוזר לענות בהודעה הבאה. **בלי הודעה ללקוח.**
    """
    chat_id = str(chat_id)
    session = db.get_active_live_chat(chat_id)
    if not session:
        return False
    # סגירה עצלה של sessions שפג תוקפם. ההשוואה נעשית ב-SQL מול
    # datetime('now') — שני הצדדים UTC, בלי תלות באזור הזמן של התהליך.
    if db.end_expired_live_chats(timeout_minutes()):
        return db.get_active_live_chat(chat_id) is not None
    return True


def resume(chat_id: str) -> None:
    """החזרת הבוט לצ'אט (סיום ההשתקה). שקט מוחלט כלפי הלקוח."""
    db.end_live_chat(str(chat_id))
    logger.info("takeover: ההשתקה בוטלה — הבוט חזר לענות")


def cleanup_on_boot() -> int:
    """סגירת כל ההשתקות בעליית תהליך.

    בלי זה, קריסה באמצע השתקה משאירה לקוחות בלי מענה לנצח — ובערוץ הזה
    הם אפילו לא יודעים שיש בוט שאמור לענות.
    """
    closed = db.cleanup_stale_live_chats()
    if closed:
        logger.info("takeover: נסגרו %d השתקות שנשארו מהריצה הקודמת", closed)
    return closed
