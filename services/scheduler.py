"""
‏scheduler פלטפורמתי — העבודות היומיות של כל ה-tenants.

**‏scheduler אחד ולא JobQueue פר-בוט.** ‏`create_business_application`
בונה את הבוטים-הבנים בלי JobQueue בכוונה: העבודות כאן חוצות tenants
(‏digest לכל אחד, ‏retention על כל DB), ועותק פר-בוט היה מריץ אותן
פעמיים ויותר.

**איך זה עובד:** ‏task אחד שרץ על לולאת הבוטים, מתעורר כל
`TICK_SECONDS`, ובודק לכל עבודה אם הגיע זמנה. הבדיקה עצמה נשענת על
סימון ב-`platform_meta` ולא על "כמה זמן עבר מהתעוררות הקודמת" — תהליך
שנפל ועלה מחדש לא אמור לאבד את היום, ולא אמור להריץ פעמיים.

**למה לא `while True: sleep(24h)`:** בפרודקשן התהליך עולה ויורד
(‏deploys, ‏autoscaling). ‏sleep ארוך אומר שכל restart מאפס את השעון,
וב-deploy יומי ה-digest לעולם לא נשלח.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# תדירות ההתעוררות. דקה היא רזולוציה מספקת לעבודות יומיות, והעלות
# זניחה — הבדיקה עצמה היא קריאת שורה אחת מ-platform_meta.
TICK_SECONDS = 60

_task: asyncio.Task | None = None
_loop = None


async def _run_digest_if_due() -> None:
    """‏digest יומי, אם הגיעה השעה והיום טרם רץ."""
    from services import digest_service

    if not digest_service.is_digest_due():
        return
    logger.info("scheduler: מריץ digest יומי")
    try:
        await digest_service.run_daily_digest()
    finally:
        # הסימון ב-finally: גם ריצה שנפלה באמצע סימנה כבר חלק
        # מה-tenants, ואיטרציה חוזרת באותו יום הייתה שולחת להם פעמיים.
        # ‏run_daily_digest בולע כשלים פר-tenant ממילא, כך שהמקרה הזה
        # הוא כשל רוחבי — והוא יטופל מחר.
        digest_service.mark_digest_ran()


async def _run_backup_if_due() -> None:
    """גיבוי לילי, אם הגיעה השעה והיום טרם רץ."""
    from services import backup_job

    if not backup_job.is_backup_due():
        return
    logger.info("scheduler: מריץ גיבוי לילי")
    try:
        await asyncio.to_thread(backup_job.run_backup_now)
    finally:
        backup_job.mark_backup_ran()


async def _run_retention_if_due() -> None:
    """‏retention יומי על ה-DB של כל tenant."""
    from services import retention_service

    if not retention_service.is_retention_due():
        return
    logger.info("scheduler: מריץ retention יומי")
    try:
        await asyncio.to_thread(retention_service.run_retention)
    finally:
        retention_service.mark_retention_ran()


async def _tick() -> None:
    """התעוררות אחת. כל עבודה עטופה בנפרד — כשל באחת לא עוצר את השנייה."""
    # הסדר משמעותי: גיבוי (03:00) לפני retention (04:00). ‏purge שרץ
    # לפני הגיבוי היה מוציא מהגיבוי בדיוק את מה שנמחק, ומחיקה בטעות
    # הייתה הופכת לבלתי הפיכה.
    for name, job in (
        ("backup", _run_backup_if_due),
        ("digest", _run_digest_if_due),
        ("retention", _run_retention_if_due),
    ):
        try:
            await job()
        except RuntimeError as exc:
            # ‏tick שנתפס באמצע כיבוי התהליך: ה-executor כבר נסגר
            # ואי אפשר לתזמן עבודה חדשה. זו לא תקלה — זה סדר הכיבוי,
            # וכל deploy היה מייצר ממנו שגיאה ב-Sentry. מדווח כ-info
            # ולא נבלע.
            if "shutdown" in str(exc).lower():
                logger.info("scheduler: %s דולג — התהליך בכיבוי", name)
            else:
                logger.error("scheduler: העבודה %s נכשלה", name, exc_info=True)
        except Exception:
            logger.error("scheduler: העבודה %s נכשלה", name, exc_info=True)


async def _run_forever() -> None:
    while True:
        await _tick()
        await asyncio.sleep(TICK_SECONDS)


def start(loop) -> None:
    """הפעלת ה-scheduler על לולאת הבוטים. אידמפוטנטי.

    בדיקת ה"כבר רץ" חיה **בתוך** ה-callback ולא כאן: ‏`start` נקרא
    מ-thread אחר, ו-`call_soon_threadsafe` רק מתזמן. שתי קריאות רצופות
    היו שתיהן רואות `_task is None` (ה-callback הראשון עוד לא רץ)
    ומייצרות שתי לולאות — כלומר שני digests באותו יום. בתוך
    ה-callback הכול מסודר על thread הלולאה.
    """
    global _loop
    _loop = loop

    def _create() -> None:
        global _task
        if _task is not None and not _task.done():
            logger.info("scheduler: כבר רץ")
            return
        _task = asyncio.ensure_future(_run_forever())
        logger.info("scheduler: הופעל (‏tick כל %d שניות)", TICK_SECONDS)

    loop.call_soon_threadsafe(_create)


def stop() -> None:
    """עצירה — לטסטים ולכיבוי מסודר.

    ‏`Task.cancel` אינו thread-safe, ולכן הביטול מתוזמן ללולאה עצמה.
    """
    global _task
    task, loop = _task, _loop
    _task = None
    if task is None:
        return
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(task.cancel)
    else:
        task.cancel()
