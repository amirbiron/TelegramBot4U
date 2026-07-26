"""
‏digest יומי לבעל העסק (‏ROADMAP T3.4).

פעם ביום, בשעה שנקבעת ב-env, כל בעל עסק מחובר מקבל בצ'אט שלו עם הבוט
סיכום של מה שקרה: כמה עניתי, כמה לקוחות כתבו, ומה ממתין לו.

**שלוש החלטות שמעצבות את המודול:**

1. **דילוג על יום שקט.** אפס פעילות ⇒ אין הודעה. ‏digest של "עניתי על
   0 הודעות" הוא בדיוק סוג ההתראה שגורם לאנשים להשתיק בוט, ואז הם
   מפספסים גם את ההתראות שכן חשובות.
2. **‏scheduler פלטפורמתי אחד, לא JobQueue פר-בוט.** ‏`create_business_application`
   בונה את הבוטים-הבנים בלי JobQueue בכוונה: אחרת כל בוט היה מריץ עותק
   של אותה עבודה, והמונים היו נספרים פעמיים.
3. **‏last-run נשמר ב-`platform_meta`.** ‏deploy באמצע היום הוא אירוע
   שגרתי בפרודקשן; בלי סימון, כל restart אחרי שעת ה-digest היה שולח
   אותו שוב.
"""

from __future__ import annotations

import logging
from datetime import datetime

from services import daily_job

logger = logging.getLogger(__name__)

_META_KEY = "digest_last_run_date"


def digest_hour() -> int:
    """השעה (שעון ישראל) שבה ה-digest נשלח. מחוץ ל-0–23 ⇒ ברירת מחדל."""
    import config as _cfg

    hour = getattr(_cfg, "DIGEST_HOUR_LOCAL", 20)
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        logger.error("DIGEST_HOUR_LOCAL אינו מספר (%r) — נופלים ל-20", hour)
        return 20
    if not 0 <= hour <= 23:
        logger.error("DIGEST_HOUR_LOCAL מחוץ לטווח (%d) — נופלים ל-20", hour)
        return 20
    return hour


def build_digest_text(counts: dict, display_name: str = "") -> str:
    """הטקסט ל-digest, או '' כשאין על מה לדווח.

    פונקציה טהורה — כל הטסטים על התוכן רצים עליה בלי רשת ובלי DB.
    """
    answered = counts.get("answered", 0)
    customers = counts.get("customers", 0)
    waiting = counts.get("waiting", 0)
    gaps = counts.get("gaps", 0)

    # יום שקט לגמרי — אין הודעה. `waiting` נספר גם הוא: שיחה שממתינה
    # לבעלים מיום קודם היא בדיוק מה שהוא צריך תזכורת עליה.
    if not any((answered, customers, waiting, gaps)):
        return ""

    lines = ["🌙 סיכום היום:"]
    if answered:
        plural = "הודעות" if answered != 1 else "הודעה"
        lines.append(f"• עניתי על {answered} {plural} מ-{customers} לקוחות")
    elif customers:
        lines.append(f"• {customers} לקוחות כתבו — לא עניתי לאף אחד")

    if waiting:
        lines.append(f"• ⏳ {waiting} שיחות ממתינות לתשובה שלך")
    if gaps:
        plural = "שאלות" if gaps != 1 else "שאלה"
        lines.append(f"• {gaps} {plural} שלא ידעתי לענות עליהן — שווה להוסיף לבסיס הידע")

    if not waiting and not gaps:
        lines.append("\nהכול טופל. לילה טוב 🙂")
    return "\n".join(lines)


async def send_digest_for_tenant(tenant_id: str) -> bool:
    """‏digest ל-tenant אחד. מחזיר האם נשלח בפועל.

    ‏False גם כשהיום היה שקט וגם כשאין חיבור — שני מקרים לגיטימיים
    שאינם שגיאה.
    """
    import control_plane as cp
    import database as db
    from bot.registry import ensure_application
    from services import owner_channel
    from tenancy import tenant_context

    conn = cp.get_business_connection_for_tenant(tenant_id)
    if not conn or not conn.get("is_enabled"):
        logger.info("digest: ל-tenant %s אין חיבור פעיל — מדלגים", tenant_id)
        return False

    with tenant_context(tenant_id):
        counts = db.get_activity_counts(hours=24)
    text = build_digest_text(counts)
    if not text:
        logger.info("digest: יום שקט ב-tenant %s — לא נשלח", tenant_id)
        return False

    app = await ensure_application(tenant_id)
    if app is None:
        logger.warning("digest: אין אפליקציה ל-tenant %s — לא נשלח", tenant_id)
        return False

    # ה-context נקבע שוב סביב השליחה: `owner_channel` כותב ל-DB של
    # ה-tenant (מיפוי היעד), ו-`ensure_application` הוא await שעלול
    # להחזיר אותנו אחרי ש-context אחר כבר רץ.
    with tenant_context(tenant_id):
        return await owner_channel.notify(app.bot, conn, text)


async def run_daily_digest() -> dict:
    """‏digest לכל ה-tenants הפעילים. מחזיר counts לדיווח.

    לולאת I/O על רשימת פריטים — ‏try/except **פר-tenant** (‏CLAUDE.md):
    בוט אחד שנפל לא מונע את ה-digest מכל השאר.
    """
    import control_plane as cp

    result = {"sent": 0, "skipped": 0, "failed": 0}
    for tenant_id in cp.list_schedulable_tenant_ids():
        try:
            if await send_digest_for_tenant(tenant_id):
                result["sent"] += 1
            else:
                result["skipped"] += 1
        except Exception:
            logger.error("digest: כשל ב-tenant %s", tenant_id, exc_info=True)
            result["failed"] += 1
    logger.info("digest יומי: %s", result)
    return result


# ─── האם הגיע הזמן ───────────────────────────────────────────────────────


def is_digest_due(now: datetime | None = None) -> bool:
    """האם להריץ digest עכשיו — לפי השעה ולפי מה שכבר רץ היום."""
    return daily_job.is_due(_META_KEY, digest_hour(), now)


def mark_digest_ran(now: datetime | None = None) -> None:
    """סימון שה-digest של היום רץ.

    נקרא **אחרי** הריצה: תהליך שנפל באמצע אמור לנסות שוב בתעוררות
    הבאה, לא לוותר על היום.
    """
    daily_job.mark_ran(_META_KEY, now)
