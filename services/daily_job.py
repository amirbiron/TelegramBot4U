"""
מנגנון ה"פעם ביום" המשותף לעבודות המתוזמנות.

היה משוכפל שלוש פעמים (‏backup, ‏digest, ‏retention) באותו נוסח בדיוק —
וזה בדיוק הדפוס שמזמין סטייה שקטה כשמתקנים רק אחד (‏CLAUDE.md → DB).
עכשיו יש מימוש אחד.

**שתי שכבות סימון, ובכוונה:**

1. **‏`platform_meta` — מקור האמת.** שורד restart, ולכן deploy אחרי
   שעת העבודה לא מריץ אותה שוב.
2. **סימון בזיכרון — רשת ביטחון.** ‏`is_due` נשען על ה-DB, ו-`mark_ran`
   רק כתב ללוג כשהכתיבה נכשלה. התוצאה: ‏`platform.db` נעול לרגע ⇒
   ‏`is_due` ממשיך להחזיר True בכל tick ⇒ ה-scheduler מריץ **גיבוי מלא
   של כל ה-tenants כל דקה עד חצות**. בארכיטקטורת worker יחיד שחולק
   משאבים עם תעבורת לקוחות חיה, זו לא הצטברות תיאורטית.

הסימון בזיכרון הוא **תוספת ולא תחליף**: הוא נמחק בעליית תהליך, ואז
ה-DB חוזר להיות מקור האמת היחיד — כלומר restart אחרי כשל כתיבה כן
יריץ את העבודה, וזו ההתנהגות הרצויה.

‏state ברמת מודול בלי מפתח tenant — חריג מכוון מהכלל: העבודות האלה הן
**פלטפורמתיות** ורצות פעם אחת על כל ה-tenants יחד, ולכן אין כאן מה
למפתח.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from memory.context import now_israel

logger = logging.getLogger(__name__)

# ‏{meta_key: תאריך הריצה האחרונה שידועה לתהליך הזה}
_ran_in_process: dict[str, str] = {}
_lock = threading.Lock()


def today_key(now: datetime | None = None) -> str:
    """מפתח היום (‏שעון ישראל) — היחידה שלפיה נמדד "פעם ביום"."""
    return (now or now_israel()).strftime("%Y-%m-%d")


def is_due(meta_key: str, hour_local: int, now: datetime | None = None) -> bool:
    """האם להריץ את העבודה עכשיו.

    התנאי הוא "השעה כבר הגיעה **והיום טרם רץ**", ולא "השעה היא בדיוק X":
    תהליך שעלה ב-23:30 עדיין אמור להריץ את העבודה של אותו יום, ואיחור
    מקצר אותה במקום לבטל.
    """
    now = now or now_israel()
    if now.hour < hour_local:
        return False

    key = today_key(now)
    with _lock:
        if _ran_in_process.get(meta_key) == key:
            return False

    import control_plane as cp

    try:
        last = cp.get_platform_meta(meta_key, "")
    except Exception:
        logger.error(
            "daily_job(%s): כשל בקריאת סימון הריצה האחרונה", meta_key, exc_info=True,
        )
        # ‏fail closed לכיוון "לא מריצים": עבודה כפולה גרועה מעבודה
        # שתחזור מחר ממילא.
        return False
    return last != key


def mark_ran(meta_key: str, now: datetime | None = None) -> None:
    """סימון שהעבודה של היום רצה.

    הסימון בזיכרון נכתב **תמיד ולפני** ניסיון הכתיבה ל-DB: זה מה שמונע
    את לולאת הריצה החוזרת גם כשה-DB לא זמין.
    """
    key = today_key(now)
    with _lock:
        _ran_in_process[meta_key] = key

    import control_plane as cp

    try:
        cp.set_platform_meta(meta_key, key)
    except Exception:
        logger.error(
            "daily_job(%s): הסימון לא נשמר — הריצה לא תחזור היום, אבל "
            "restart לפני חצות כן יריץ אותה שוב",
            meta_key, exc_info=True,
        )


def reset_process_marks() -> None:
    """איפוס הסימון בזיכרון — לטסטים בלבד."""
    with _lock:
        _ran_in_process.clear()
