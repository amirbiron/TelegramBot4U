"""
העבודה המתוזמנת של הגיבוי (‏ROADMAP T4.4).

מפרידה בין **מתי** לגבות לבין **איך** — ‏`backup_service` יודע לגבות,
והמודול הזה יודע מתי. אותו דפוס כמו `retention_service`, ומאותה סיבה:
הסימון ב-`platform_meta` הוא מה שמונע גיבוי כפול אחרי deploy.
"""

from __future__ import annotations

import logging
from datetime import datetime

from memory.context import now_israel

logger = logging.getLogger(__name__)

_META_KEY = "backup_last_run_date"

# ‏03:00 — לפני ה-retention (04:00), כך שהגיבוי מכיל גם את מה שה-purge
# עומד למחוק. זה הסדר הנכון: גיבוי אחרי מחיקה כבר לא מכיל את מה שנמחק,
# ומחיקה בטעות הופכת לבלתי הפיכה.
BACKUP_HOUR_LOCAL = 3


def _today_key(now: datetime) -> str:
    return now.strftime("%Y-%m-%d")


def is_backup_due(now: datetime | None = None) -> bool:
    """האם להריץ גיבוי עכשיו — לפי השעה ולפי מה שכבר רץ היום."""
    import control_plane as cp

    now = now or now_israel()
    if now.hour < BACKUP_HOUR_LOCAL:
        return False
    try:
        last = cp.get_platform_meta(_META_KEY, "")
    except Exception:
        logger.error("backup: כשל בקריאת סימון הריצה האחרונה", exc_info=True)
        return False
    return last != _today_key(now)


def mark_backup_ran(now: datetime | None = None) -> None:
    """סימון שהגיבוי של היום רץ."""
    import control_plane as cp

    now = now or now_israel()
    try:
        cp.set_platform_meta(_META_KEY, _today_key(now))
    except Exception:
        logger.error("backup: כשל בסימון הריצה", exc_info=True)


def run_backup_now(now: datetime | None = None) -> dict:
    """הרצת הגיבוי. ה-stamp וה-epoch נגזרים כאן ומועברים פנימה.

    ‏`backup_service` אינו קורא לשעון בעצמו בכוונה: אותו stamp משמש את
    כל ה-tenants בריצה, כך שריצה שחוצה חצות לא מפזרת אותם בין שתי
    תיקיות ומשאירה גיבוי חלקי בכל אחת.
    """
    import backup_service

    now = now or now_israel()
    return backup_service.run_backup(
        stamp=_today_key(now), now_epoch=now.timestamp(),
    )
