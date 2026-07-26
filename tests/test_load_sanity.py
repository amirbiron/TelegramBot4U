"""בידוד בין tenants תחת פרץ (‏T4.7).

השאלה שה-ROADMAP שואל: פרץ הודעות אצל לקוח אחד — מזיז את הלטנסי של
לקוח שני?

עד `core/pipeline_executor.py` התשובה הייתה **כן**. הצינור רץ ב-
`asyncio.to_thread`, שמריץ על ה-executor של ברירת המחדל שגודלו
`cpu_count + 4` — ‏8 threads על מכונת פיתוח, ‏5 על instance קטן.
במדידה: פרץ של 20 הודעות ל-tenant אחד הפך הודעה של 2 שניות אצל
tenant אחר ל-6 שניות.

הטסטים כאן מודדים את מנגנון ההרצה עם `sleep` במקום קריאת LLM: הנמדד
הוא התור, לא הספק. הספים רחבים בכוונה — טסט זמנים שנכשל על מכונת CI
עמוסה הוא רעש, לא ממצא.
"""

import asyncio
import os
import time

import pytest

from core import pipeline_executor
from tenancy import tenant_context

# קריאת LLM מדומה. קצר מספיק כדי שהטסט ירוץ מהר, ארוך מספיק כדי
# שהפרש בתור יהיה גדול מרעש התזמון.
FAKE_LLM_SECONDS = 0.3
BURST = 40


def _slow_call() -> str:
    time.sleep(FAKE_LLM_SECONDS)
    return "ok"


@pytest.fixture(autouse=True)
def _fresh_executor():
    pipeline_executor.reset_for_tests()
    yield
    pipeline_executor.reset_for_tests()


class TestTenantIsolation:
    async def test_a_burst_does_not_stall_another_tenant(self):
        """הבדיקה המרכזית של T4.7."""
        async def run_for(tenant_id: str) -> float:
            started = time.monotonic()
            with tenant_context(tenant_id):
                await pipeline_executor.run_pipeline(_slow_call)
            return time.monotonic() - started

        burst = [asyncio.create_task(run_for("noisy")) for _ in range(BURST)]
        await asyncio.sleep(0.05)  # לתת לפרץ לתפוס threads
        quiet_latency = await run_for("quiet")
        await asyncio.gather(*burst)

        # ה-tenant השקט ממתין לכל היותר לסבב אחד מלא של התקרה שלו.
        # בלי התקרה הוא היה ממתין ל-BURST/pool_size סבבים.
        assert quiet_latency < FAKE_LLM_SECONDS * 3, (
            f"‏tenant שקט חיכה {quiet_latency:.2f}s — פרץ אצל שכן מעכב אותו"
        )

    async def test_a_single_tenant_cannot_take_the_whole_pool(self):
        """התקרה פר-tenant נאכפת בפועל, ולא רק מוגדרת."""
        active = 0
        peak = 0
        lock = asyncio.Lock()

        def _track() -> None:
            time.sleep(FAKE_LLM_SECONDS)

        async def one() -> None:
            nonlocal active, peak
            with tenant_context("noisy"):
                async with lock:
                    pass
                await pipeline_executor.run_pipeline(_track)

        # נמדד דרך הסמפור עצמו: כמה מקומות תפוסים בשיא.
        sem = pipeline_executor._semaphore("noisy")
        tasks = [asyncio.create_task(one()) for _ in range(BURST)]
        for _ in range(12):
            await asyncio.sleep(0.02)
            in_flight = pipeline_executor.PER_TENANT_LIMIT - sem._value
            active = in_flight
            peak = max(peak, active)
        await asyncio.gather(*tasks)

        assert peak <= pipeline_executor.PER_TENANT_LIMIT, (
            f"‏{peak} הרצות במקביל ל-tenant אחד, התקרה היא "
            f"{pipeline_executor.PER_TENANT_LIMIT}"
        )
        assert peak > 1, "התקרה חונקת יותר מדי — לא רצה כלום במקביל"


class TestExecutorSizing:
    def test_pool_is_not_derived_from_cpu_count(self):
        """הצינור ממתין לרשת. גזירה מ-`cpu_count` היא המדד הלא נכון.

        זו הייתה הסיבה השורשית: על instance קטן ‏`cpu_count` הוא 1,
        וברירת המחדל של `to_thread` נותנת 5 threads לכל הפלטפורמה.
        """
        assert pipeline_executor.POOL_SIZE >= 16
        assert pipeline_executor.POOL_SIZE > (os.cpu_count() or 1) + 4

    def test_per_tenant_limit_leaves_room_for_others(self):
        """תקרה שגדולה מדי שקולה לאין תקרה."""
        assert pipeline_executor.PER_TENANT_LIMIT < pipeline_executor.POOL_SIZE / 2

    def test_executor_is_created_once(self):
        first = pipeline_executor.get_executor()
        assert pipeline_executor.get_executor() is first


class TestTenantContextCrossesTheThread:
    """‏`run_in_executor` אינו מעתיק contextvars — ההעתקה כאן מפורשת.

    בלעדיה כל הרצה הייתה נופלת ל-tenant של ברירת המחדל, כלומר כותבת
    ל-DB הלא נכון. זה הכשל השקט שהכלל ב-CLAUDE.md מזהיר מפניו, והוא
    לא היה מתגלה בטסט פונקציונלי של tenant יחיד.
    """

    async def test_tenant_survives_the_hop(self):
        from tenancy import get_current_tenant

        def _who() -> str:
            return get_current_tenant()

        with tenant_context("acme"):
            assert await pipeline_executor.run_pipeline(_who) == "acme"
        with tenant_context("globex"):
            assert await pipeline_executor.run_pipeline(_who) == "globex"

    async def test_concurrent_tenants_do_not_bleed(self):
        """שני tenants במקביל — כל אחד רואה את שלו.

        זה התרחיש שבו כשל היה גורם לתשובה של לקוח אחד להישמר אצל אחר.
        """
        from tenancy import get_current_tenant

        def _who() -> str:
            time.sleep(0.05)
            return get_current_tenant()

        async def run_for(tenant_id: str) -> str:
            with tenant_context(tenant_id):
                return await pipeline_executor.run_pipeline(_who)

        names = [f"tenant{i}" for i in range(12)]
        results = await asyncio.gather(*(run_for(n) for n in names))
        assert results == names


class TestArguments:
    async def test_args_and_kwargs_pass_through(self):
        def _fn(a, b, *, c):
            return (a, b, c)

        with tenant_context("acme"):
            assert await pipeline_executor.run_pipeline(_fn, 1, 2, c=3) == (1, 2, 3)

    async def test_exceptions_propagate(self):
        """חריגה בצינור חייבת לחזור לקורא — לא להיבלע ב-thread."""
        def _boom():
            raise ValueError("נפל")

        with tenant_context("acme"):
            with pytest.raises(ValueError, match="נפל"):
                await pipeline_executor.run_pipeline(_boom)

    async def test_the_semaphore_is_released_after_a_failure(self):
        """חריגה שלא משחררת מקום בתקרה חונקת את ה-tenant לצמיתות."""
        def _boom():
            raise ValueError("נפל")

        sem = pipeline_executor._semaphore("acme")
        before = sem._value
        for _ in range(pipeline_executor.PER_TENANT_LIMIT + 2):
            with tenant_context("acme"):
                with pytest.raises(ValueError):
                    await pipeline_executor.run_pipeline(_boom)
        assert sem._value == before
