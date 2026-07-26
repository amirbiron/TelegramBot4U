"""
שורת הגילוי — יידוע הלקוח שהוא מדבר עם עוזר (‏ROADMAP T4.3).

**למה זה קיים ולמה זה לא "מסך הסכמה":** בערוץ הזה אין `/start` ואין
כפתורים, ולכן אי אפשר לבקש הסכמה מלקוח הקצה — וגם אין צורך: ה-tenant
הוא בעל המאגר וחב בחובת **היידוע**, לא בקבלת הסכמה (‏PLAN §6,
‏CLAUDE.md → פרטיות). שורת הגילוי היא מימוש חובת היידוע.

**איפה היא מופיעה:** משורשרת לתשובה הראשונה בצ'אט, לא כהודעה נפרדת.
הודעה נפרדת נקראת כמו הודעת מערכת, וזה בדיוק מה שהערוץ הזה נמנע ממנו.

**פעם אחת פר-לקוח.** ‏`users.disclosure_sent_at` הוא הסימון, והוא
נכתב **אחרי** שליחה מוצלחת: אם השליחה נכשלה הלקוח לא ראה כלום, ולסמן
היה אומר שהוא לא יראה לעולם.

**הכיבוי הוא החלטה שנרשמת.** ברירת המחדל דלוקה. בעל עסק שמכבה מקבל
אזהרה בפאנל, וההחלטה נרשמת ב-`consent_ledger` — כדי שאם תישאל שאלה
מי החליט ומתי, תהיה תשובה.
"""

from __future__ import annotations

import logging

import database as db

logger = logging.getLogger(__name__)

# הנוסח המובנה. ‏{business} מוחלף בשם העסק בזמן ריצה.
DEFAULT_TEMPLATE = "כאן העוזר של {business} — אני עונה כשהוא לא זמין."

# מפתח ה-placeholder היחיד הנתמך. תבנית עם placeholder אחר תישלח
# כלשונה במקום להיכשל — ראה `render_disclosure`.
_PLACEHOLDER = "{business}"


def is_enabled() -> bool:
    """האם שורת הגילוי דלוקה ל-tenant הנוכחי (ברירת מחדל: כן)."""
    try:
        return bool(db.get_bot_settings().get("disclosure_enabled", 1))
    except Exception:
        # ‏fail **open**, בניגוד לרוב המקומות: כשל בקריאת ההגדרה לא אמור
        # להשתיק חובת יידוע. עדיף שורה מיותרת על היעדר גילוי.
        logger.error("disclosure: כשל בקריאת ההגדרה — ממשיכים כדלוק", exc_info=True)
        return True


def render_disclosure() -> str:
    """נוסח שורת הגילוי ל-tenant הנוכחי, או '' אם היא כבויה."""
    if not is_enabled():
        return ""
    try:
        settings = db.get_bot_settings() or {}
        template = (settings.get("disclosure_template") or "").strip()
    except Exception:
        logger.error("disclosure: כשל בקריאת התבנית", exc_info=True)
        template = ""
    template = template or DEFAULT_TEMPLATE

    business_name = ""
    try:
        from config import get_business_config

        business_name = (get_business_config().name or "").strip()
    except Exception:
        logger.error("disclosure: כשל בקריאת שם העסק", exc_info=True)

    if _PLACEHOLDER not in template:
        # תבנית שהבעלים כתב בלי ה-placeholder — שולחים כלשונה. זו
        # החלטה שלו, ולא סיבה להשתיק את היידוע.
        return template
    if not business_name:
        # בלי שם עסק, "כאן העוזר של  —" נראה שבור. נוסח חלופי שעדיין
        # מיידע, בלי להסגיר תקלה.
        return "כאן העוזר האישי — אני עונה כשבעל העסק לא זמין."
    return template.replace(_PLACEHOLDER, business_name)


def is_due(user_id: str) -> bool:
    """האם יש לצרף את שורת הגילוי לתשובה הזו."""
    if not user_id or not is_enabled():
        return False
    try:
        row = db.get_user(user_id)
    except Exception:
        logger.error("disclosure: כשל בקריאת המשתמש", exc_info=True)
        # ‏fail closed **כאן**: בספק, עדיף לא לשלוח שוב שורה שכבר נשלחה
        # מאשר להטריד לקוח ותיק בהודעה שנראית כמו תקלה.
        return False
    if row is None:
        return True
    return not (row.get("disclosure_sent_at") or "").strip()


def prepend(text: str, user_id: str) -> str:
    """שרשור שורת הגילוי לתשובה, אם היא חלה. אחרת — הטקסט כמו שהוא."""
    if not text or not is_due(user_id):
        return text
    line = render_disclosure()
    if not line:
        return text
    return f"{line}\n\n{text}"


def mark_sent(user_id: str) -> None:
    """סימון שהגילוי נמסר. נקרא **רק** אחרי שליחה מוצלחת."""
    if not user_id:
        return
    try:
        db.mark_disclosure_sent(user_id)
    except Exception:
        logger.error("disclosure: סימון השליחה נכשל", exc_info=True)


def record_toggle(enabled: bool, actor: str = "owner") -> None:
    """רישום החלטת בעל העסק ב-consent_ledger.

    הכיבוי הוא ההחלטה המעניינת, אבל גם ההדלקה נרשמת — אחרת רצף
    ההחלטות לא שלם ואי אפשר לענות "ממתי זה היה כבוי".
    """
    try:
        from utils.consent_ledger import (
            EVENT_DISCLOSURE_DISABLED,
            EVENT_DISCLOSURE_ENABLED,
            record_consent_event,
        )

        record_consent_event(
            user_id=f"tenant:{actor}",
            channel=db.CHANNEL,
            event_type=(
                EVENT_DISCLOSURE_ENABLED if enabled else EVENT_DISCLOSURE_DISABLED
            ),
            metadata={"setting": "disclosure_enabled", "value": bool(enabled)},
        )
    except Exception:
        logger.error("disclosure: רישום ההחלטה ב-ledger נכשל", exc_info=True)
