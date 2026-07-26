"""הרצת הצינור הסינכרוני מחוץ ללולאה — עם הוגנות בין tenants (‏T4.7).

**הבעיה שנמדדה.** הצינור (‏DB + LLM) סינכרוני, ולכן רץ ב-thread נפרד.
עד כאן טוב: לולאת הבוט לא נחסמת. אבל `asyncio.to_thread` מריץ הכול על
ה-executor של ברירת המחדל, שגודלו `cpu_count + 4` — ‏8 threads על
מכונת פיתוח, ‏5 על instance קטן ב-Render. פרץ של 20 הודעות ל-tenant
אחד תופס את כולם, והודעה בודדת של tenant אחר ממתינה בתור.

מדידה בפועל (‏20 הודעות, קריאת LLM מדומה של 2 שניות): ההודעה של
ה-tenant השכן חיכתה **6 שניות במקום 2**. עם `cpu_count=1` בפרודקשן זה
גרוע בהרבה. זה בדיוק התרחיש ש-T4.7 בא לבדוק, והתשובה הייתה "לא, פרץ
אצל לקוח אחד כן מזיז את הלטנסי של לקוח שני".

**שני תיקונים, ושניהם נחוצים:**

1. ‏executor ייעודי שגודלו נגזר מהעבודה ולא מהמעבד. הצינור ממתין
   לרשת, לא מחשב; ‏`cpu_count` אינו מדד רלוונטי, והוא מה שיצר תקרה
   נמוכה במיוחד דווקא על המכונות הקטנות שבהן זה כואב.
2. תקרה **פר-tenant**. בלעדיה, הגדלת ה-pool רק מזיזה את הרף: ‏tenant
   עם 60 הודעות עדיין היה תופס את הכול. התקרה מבטיחה שגם בפרץ נשארים
   ‏threads פנויים לשאר.

**‏contextvars.** ‏`to_thread` מעתיק את ה-context אוטומטית, ו-`tenant`
עובר איתו. ‏`run_in_executor` **אינו** עושה זאת — ולכן ההעתקה כאן
מפורשת. בלעדיה כל הרצה הייתה נופלת ל-tenant של ברירת המחדל, כלומר
כותבת ל-DB הלא נכון (‏CLAUDE.md — "מעבר בין threads לא מעביר את
ה-context").
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# עבודה תלוית-רשת: ‏thread שממתין ל-HTTP אינו צורך מעבד, ולכן המספר
# נגזר מכמה שיחות מקבילות אנחנו רוצים לשרת ולא מ-`cpu_count`.
POOL_SIZE = int(os.getenv("PIPELINE_POOL_SIZE") or "32")

# כמה הודעות של **אותו** tenant יעובדו במקביל. מתחת לרבע מה-pool כדי
# שארבעה לקוחות בפרץ בו-זמנית עדיין לא ירעיבו את החמישי.
PER_TENANT_LIMIT = int(os.getenv("PIPELINE_PER_TENANT_LIMIT") or "6")

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_semaphores: dict[str, asyncio.Semaphore] = {}


def get_executor() -> ThreadPoolExecutor:
    """ה-executor המשותף. נוצר עצלנית, פעם אחת."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=POOL_SIZE, thread_name_prefix="pipeline",
                )
                logger.info("pipeline executor עלה עם %d threads", POOL_SIZE)
    return _executor


def _semaphore(tenant_id: str) -> asyncio.Semaphore:
    """הסמפור של ה-tenant.

    ‏state ברמת מודול ⇒ ממופתח ב-tenant מהיום הראשון (‏CLAUDE.md).
    מ-Python 3.10 ‏`asyncio.Semaphore` אינו נקשר ללולאה בזמן היצירה,
    ולכן מותר ליצור אותו כאן; בפרודקשן יש ממילא לולאת בוטים אחת.
    """
    sem = _semaphores.get(tenant_id)
    if sem is None:
        sem = asyncio.Semaphore(PER_TENANT_LIMIT)
        _semaphores[tenant_id] = sem
    return sem


async def run_pipeline(func, /, *args, **kwargs):
    """הרצת פונקציה סינכרונית ב-thread, תחת התקרה של ה-tenant הנוכחי.

    תחליף ישיר ל-`asyncio.to_thread` בנתיב הצינור.
    """
    from tenancy import get_current_tenant

    try:
        tenant_id = get_current_tenant()
    except Exception:
        logger.error("run_pipeline: כשל בזיהוי ה-tenant", exc_info=True)
        tenant_id = ""

    ctx = contextvars.copy_context()
    loop = asyncio.get_running_loop()

    async with _semaphore(tenant_id or ""):
        return await loop.run_in_executor(
            get_executor(), lambda: ctx.run(func, *args, **kwargs),
        )


def shutdown() -> None:
    """סגירת ה-executor — ביציאה מסודרת ובטסטים."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None


def reset_for_tests() -> None:
    """איפוס הסמפורים בין טסטים (הם נקשרים ללולאה שרצה)."""
    _semaphores.clear()
