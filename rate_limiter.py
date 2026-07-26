"""
Rate Limiter — הגבלת קצב הודעות פר-(tenant, משתמש), בזיכרון.

הועתק מ-`ai-business-bot/rate_limiter.py` (‏ROADMAP T0.8). מה שיצא:
**כל הדקורטורים**. בערוץ הזה יש נקודת כניסה אחת (`on_business_message`)
וה-guards בה ליניאריים — הכלל הישן "דקורטור על כל handler" הוחלף
(‏CLAUDE.md → "הערוץ — כללי ברזל").

הבדל התנהגותי מהותי: חריגה **אינה** מייצרת הודעה ללקוח. מאדם, "אנא
המתן, יותר מדי הודעות" נשמע מוזר ומסגיר אוטומציה — לכן שתיקה ללקוח
והתראה לבעלים (‏PLAN §4.3).

שלושה חלונות הזזה: לדקה, לשעה וליום. הנתונים בזיכרון ומתאפסים בהפעלה
מחדש — מקובל לבוט של עסק קטן: אין overhead של התמדה, וחלונות ההתעללות
חסומים ממילא.
"""

import bisect
import logging
import time
from collections import OrderedDict, deque

from config import (
    RATE_LIMIT_PER_DAY,
    RATE_LIMIT_PER_HOUR,
    RATE_LIMIT_PER_MINUTE,
)

logger = logging.getLogger(__name__)

# תקרת משתמשים במעקב — פינוי LRU כשנגמר מקום.
_MAX_TRACKED_USERS = 10_000

# המפתח: (tenant, user_id) — אותו לקוח מול שני עסקים נספר בנפרד,
# והמגבלות לא מתערבבות בין tenants.
_user_timestamps: "OrderedDict[tuple[str, str], deque[float]]" = OrderedDict()

# (חלון בשניות, מקסימום הודעות, שם החלון ללוג)
_WINDOWS = [
    (60, RATE_LIMIT_PER_MINUTE, "minute"),
    (3600, RATE_LIMIT_PER_HOUR, "hour"),
    (86400, RATE_LIMIT_PER_DAY, "day"),
]


def _bucket_key(user_id: str) -> tuple[str, str]:
    from tenancy import get_current_tenant

    return (get_current_tenant(), user_id)


def _prune(timestamps: "deque[float]", now: float) -> None:
    """הסרת חותמות ישנות מהחלון הגדול ביותר (יממה)."""
    cutoff = now - 86400
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()


def _touch(key: tuple[str, str]) -> "deque[float]":
    """מחזיר את ה-deque של המפתח, יוצר אם צריך, ומפנה LRU בעת הצורך."""
    if key not in _user_timestamps:
        _user_timestamps[key] = deque()
        # פינוי גם ב-check ולא רק ב-record — אחרת משתמש חסום היה מגדיל
        # את המילון בלי גבול
        while len(_user_timestamps) > _MAX_TRACKED_USERS:
            _user_timestamps.popitem(last=False)
    else:
        _user_timestamps.move_to_end(key)
    return _user_timestamps[key]


def check_rate_limit(user_id: str) -> str | None:
    """בדיקה אם המשתמש חרג ממגבלה כלשהי.

    מחזיר את שם החלון שנחרג (‏'minute' / ‏'hour' / ‏'day') ל**לוג ולהתראה
    לבעלים**, או None כשהכול תקין. הערך אינו טקסט ללקוח — בערוץ הזה לא
    שולחים ללקוח הודעת מערכת.

    הפונקציה **אינה** רושמת חותמת חדשה — יש לקרוא ל-`record_message`
    אחרי שהוחלט שההודעה תעובד.
    """
    now = time.time()
    timestamps = _touch(_bucket_key(user_id))
    _prune(timestamps, now)

    # bisect על רשימה ממוינת (ה-deque ממוינת כי החותמות עולות)
    ts_list = list(timestamps)
    for window_seconds, max_messages, name in _WINDOWS:
        cutoff = now - window_seconds
        idx = bisect.bisect_left(ts_list, cutoff)
        count = len(ts_list) - idx
        if count >= max_messages:
            logger.info(
                "rate limit hit: %d הודעות ב-%d שניות (מגבלה %d)",
                count, window_seconds, max_messages,
            )
            return name
    return None


def record_message(user_id: str) -> None:
    """רישום חותמת זמן חדשה למשתמש."""
    _touch(_bucket_key(user_id)).append(time.time())


def reset_all() -> None:
    """איפוס מלא — לטסטים בלבד."""
    _user_timestamps.clear()
