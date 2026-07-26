"""
‏retention יומי — הפעלת `purge_old_data` על ה-DB של כל tenant.

‏`database.purge_old_data` מגדיר את המדיניות; המודול הזה רק דואג שמישהו
יקרא לה. בלי קורא, כל מה שכתוב ב-`docs/privacy_data_matrix.md` על
תקופות שמירה הוא הצהרה בלבד — וזה בדיוק הפער שנתפס בסקירה על תור
ה-retry של ה-ledger.
"""

from __future__ import annotations

import logging
from datetime import datetime

from services import daily_job

logger = logging.getLogger(__name__)

_META_KEY = "retention_last_run_date"

# שעה קבועה ולא ניתנת לתצורה: זו עבודת תחזוקה שאיש לא רואה, והשעה
# היחידה שחשובה בה היא "לא באותו רגע כמו ה-digest" — שתי לולאות על כל
# ה-tenants בו-זמנית מתחרות על אותם קבצי SQLite.
RETENTION_HOUR_LOCAL = 4


def is_retention_due(now: datetime | None = None) -> bool:
    """האם להריץ retention עכשיו — לפי השעה ולפי מה שכבר רץ היום."""
    return daily_job.is_due(_META_KEY, RETENTION_HOUR_LOCAL, now)


def mark_retention_ran(now: datetime | None = None) -> None:
    """סימון שה-retention של היום רץ."""
    daily_job.mark_ran(_META_KEY, now)


def run_retention() -> dict:
    """‏purge על כל tenant פעיל. מחזיר את הסיכום פר-tenant.

    לולאת I/O על רשימת פריטים — ‏try/except **פר-tenant**: ‏DB נעול של
    לקוח אחד לא מונע את ה-retention מכל השאר.
    """
    import control_plane as cp
    import database as db
    from tenancy import tenant_context

    summary: dict[str, dict] = {}
    for tenant_id in cp.list_schedulable_tenant_ids():
        try:
            with tenant_context(tenant_id):
                summary[tenant_id] = db.purge_old_data()
        except Exception:
            logger.error("retention: כשל ב-tenant %s", tenant_id, exc_info=True)
            summary[tenant_id] = {"failed": True}
    logger.info("retention יומי הושלם על %d tenants", len(summary))
    return summary
