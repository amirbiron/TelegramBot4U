"""טסטים ל-digest היומי, ל-retention ול-scheduler שמריץ אותם (‏T3.4)."""

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

import control_plane as cp
import database as db
from services import digest_service, retention_service, scheduler
from tenancy import tenant_context
from tests.doubles import FakeBot

IL = ZoneInfo("Asia/Jerusalem")
OWNER_ID = 900001
CUSTOMER_ID = 500042
CONNECTION_ID = "conn-demo-0001"


def _at(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 7, day, hour, 30, tzinfo=IL)


class TestDigestText:
    """פונקציה טהורה — כאן נבדק כל התוכן, בלי רשת ובלי DB."""

    def test_quiet_day_produces_nothing(self):
        counts = {"answered": 0, "customers": 0, "waiting": 0, "gaps": 0}
        assert digest_service.build_digest_text(counts) == ""

    def test_waiting_alone_is_enough_to_send(self):
        """שיחה שממתינה מאתמול היא בדיוק מה שצריך תזכורת."""
        counts = {"answered": 0, "customers": 0, "waiting": 2, "gaps": 0}
        text = digest_service.build_digest_text(counts)
        assert "2 שיחות ממתינות" in text

    def test_reports_answered_and_customers(self):
        counts = {"answered": 34, "customers": 7, "waiting": 0, "gaps": 0}
        text = digest_service.build_digest_text(counts)
        assert "34 הודעות" in text
        assert "7 לקוחות" in text

    def test_singular_form_for_one(self):
        counts = {"answered": 1, "customers": 1, "waiting": 0, "gaps": 1}
        text = digest_service.build_digest_text(counts)
        assert "1 הודעה מ-1 לקוחות" in text
        assert "1 שאלה שלא ידעתי" in text

    def test_all_clear_gets_a_closing_line(self):
        counts = {"answered": 5, "customers": 2, "waiting": 0, "gaps": 0}
        assert "הכול טופל" in digest_service.build_digest_text(counts)

    def test_pending_work_has_no_closing_line(self):
        counts = {"answered": 5, "customers": 2, "waiting": 1, "gaps": 0}
        assert "הכול טופל" not in digest_service.build_digest_text(counts)

    def test_customers_without_answers_is_reported(self):
        """לקוחות כתבו ולא ענינו — הבעלים חייב לדעת."""
        counts = {"answered": 0, "customers": 3, "waiting": 0, "gaps": 0}
        assert "לא עניתי לאף אחד" in digest_service.build_digest_text(counts)


class TestDigestHour:
    def test_default_when_unset(self, monkeypatch):
        import config as _cfg

        monkeypatch.delattr(_cfg, "DIGEST_HOUR_LOCAL", raising=False)
        assert digest_service.digest_hour() == 20

    @pytest.mark.parametrize("bad", [-1, 24, 99, "לא מספר", None])
    def test_invalid_falls_back(self, monkeypatch, bad):
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", bad, raising=False)
        assert digest_service.digest_hour() == 20

    def test_valid_value_is_used(self, monkeypatch):
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 7, raising=False)
        assert digest_service.digest_hour() == 7


class TestDigestScheduling:
    def test_not_due_before_the_hour(self, platform_db, monkeypatch):
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 20, raising=False)
        assert digest_service.is_digest_due(_at(19)) is False

    def test_due_at_the_hour(self, platform_db, monkeypatch):
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 20, raising=False)
        assert digest_service.is_digest_due(_at(20)) is True

    def test_late_start_still_runs_the_same_day(self, platform_db, monkeypatch):
        """תהליך שעלה ב-23:30 לא אמור לוותר על ה-digest של אותו יום."""
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 20, raising=False)
        assert digest_service.is_digest_due(_at(23)) is True

    def test_not_due_twice_the_same_day(self, platform_db, monkeypatch):
        """‏deploy אחרי שעת ה-digest לא שולח אותו שוב."""
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 20, raising=False)
        digest_service.mark_digest_ran(_at(20))
        assert digest_service.is_digest_due(_at(21)) is False
        assert digest_service.is_digest_due(_at(23)) is False

    def test_due_again_the_next_day(self, platform_db, monkeypatch):
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 20, raising=False)
        digest_service.mark_digest_ran(_at(20, day=15))
        assert digest_service.is_digest_due(_at(20, day=16)) is True


class TestDigestSending:
    @pytest.fixture
    def connected(self, tenant):
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
        )
        return "acme"

    def _with_activity(self, tenant_id: str):
        with tenant_context(tenant_id):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
            db.save_message(str(CUSTOMER_ID), "דנה", "user", "שאלה", authored_by="customer")
            db.save_message(str(CUSTOMER_ID), "דנה", "assistant", "תשובה", authored_by="bot")

    async def test_sends_to_the_owner_chat(self, connected):
        self._with_activity(connected)
        bot = FakeBot()
        app = type("App", (), {"bot": bot})()
        with patch("bot.registry.ensure_application", AsyncMock(return_value=app)):
            sent = await digest_service.send_digest_for_tenant(connected)

        assert sent is True
        assert len(bot.messages) == 1
        # לצ'אט הבעלים — **בלי** business_connection_id
        assert bot.messages[0]["business_connection_id"] is None
        assert bot.messages[0]["chat_id"] == OWNER_ID
        assert "סיכום היום" in bot.messages[0]["text"]

    async def test_quiet_day_sends_nothing(self, connected):
        bot = FakeBot()
        app = type("App", (), {"bot": bot})()
        with patch("bot.registry.ensure_application", AsyncMock(return_value=app)):
            sent = await digest_service.send_digest_for_tenant(connected)

        assert sent is False
        assert bot.messages == []

    async def test_disabled_connection_is_skipped(self, tenant):
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=False, can_reply=False, rights_json="{}",
        )
        self._with_activity("acme")
        assert await digest_service.send_digest_for_tenant("acme") is False

    async def test_no_connection_is_skipped(self, tenant):
        self._with_activity("acme")
        assert await digest_service.send_digest_for_tenant("acme") is False

    async def test_one_failing_tenant_does_not_stop_the_rest(self, platform_db):
        platform_db.create_tenant("a", "עסק א")
        platform_db.create_tenant("b", "עסק ב")

        async def _fake(tenant_id):
            if tenant_id == "a":
                raise RuntimeError("נפל")
            return True

        with patch.object(digest_service, "send_digest_for_tenant", _fake):
            result = await digest_service.run_daily_digest()

        assert result["failed"] == 1
        assert result["sent"] == 1


class TestRetention:
    def test_not_due_before_the_hour(self, platform_db):
        assert retention_service.is_retention_due(_at(3)) is False

    def test_due_after_the_hour(self, platform_db):
        assert retention_service.is_retention_due(_at(5)) is True

    def test_not_due_twice_the_same_day(self, platform_db):
        retention_service.mark_retention_ran(_at(5))
        assert retention_service.is_retention_due(_at(9)) is False

    def test_runs_purge_per_tenant(self, platform_db):
        platform_db.create_tenant("a", "עסק א")
        platform_db.create_tenant("b", "עסק ב")
        summary = retention_service.run_retention()
        assert set(summary) == {"a", "b"}
        assert all("conversations" in s for s in summary.values())

    def test_one_failing_tenant_does_not_stop_the_rest(self, platform_db):
        platform_db.create_tenant("a", "עסק א")
        platform_db.create_tenant("b", "עסק ב")
        calls = {"n": 0}
        real = db.purge_old_data

        def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DB נעול")
            return real()

        with patch.object(db, "purge_old_data", _flaky):
            summary = retention_service.run_retention()

        assert summary["a"] == {"failed": True}
        assert "conversations" in summary["b"]

    def test_digest_and_retention_hours_do_not_collide(self, monkeypatch):
        """שתי לולאות על אותם קובצי SQLite בו-זמנית — מיותר."""
        import config as _cfg

        monkeypatch.setattr(_cfg, "DIGEST_HOUR_LOCAL", 20, raising=False)
        assert retention_service.RETENTION_HOUR_LOCAL != digest_service.digest_hour()


class TestSchedulerTick:
    async def test_tick_runs_nothing_when_not_due(self, platform_db):
        with patch.object(digest_service, "is_digest_due", return_value=False), \
                patch.object(retention_service, "is_retention_due", return_value=False), \
                patch.object(digest_service, "run_daily_digest", AsyncMock()) as digest, \
                patch.object(retention_service, "run_retention") as retention:
            await scheduler._tick()
        digest.assert_not_called()
        retention.assert_not_called()

    async def test_failing_job_does_not_stop_the_other(self, platform_db):
        with patch.object(digest_service, "is_digest_due", side_effect=RuntimeError("נפל")), \
                patch.object(retention_service, "is_retention_due", return_value=True), \
                patch.object(retention_service, "run_retention", return_value={}) as retention:
            await scheduler._tick()
        retention.assert_called_once()

    async def test_digest_is_marked_even_when_it_fails(self, platform_db):
        """כשל רוחבי לא אמור לגרום לניסיונות חוזרים כל דקה עד חצות."""
        with patch.object(digest_service, "is_digest_due", return_value=True), \
                patch.object(retention_service, "is_retention_due", return_value=False), \
                patch.object(
                    digest_service, "run_daily_digest",
                    AsyncMock(side_effect=RuntimeError("נפל")),
                ), \
                patch.object(digest_service, "mark_digest_ran") as mark:
            await scheduler._tick()
        mark.assert_called_once()


class TestSchedulerLifecycle:
    """שתי לולאות scheduler = שני digests באותו יום."""

    def _fresh_loop(self):
        import asyncio
        import threading

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run():
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        ready.wait(timeout=5)
        return loop, thread

    def test_repeated_start_creates_one_task(self, monkeypatch):
        import asyncio

        loop, thread = self._fresh_loop()
        monkeypatch.setattr(scheduler, "TICK_SECONDS", 3600)
        try:
            # שלוש קריאות רצופות — הבדיקה חייבת לרוץ על thread הלולאה,
            # אחרת כולן רואות `_task is None` ומייצרות לולאה כל אחת.
            scheduler.start(loop)
            scheduler.start(loop)
            scheduler.start(loop)
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)

            alive = [t for t in asyncio.all_tasks(loop) if not t.done()]
            assert len(alive) == 1
        finally:
            scheduler.stop()
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_stop_cancels_the_task(self, monkeypatch):
        import asyncio

        loop, thread = self._fresh_loop()
        monkeypatch.setattr(scheduler, "TICK_SECONDS", 3600)
        try:
            scheduler.start(loop)
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)
            scheduler.stop()
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)

            assert [t for t in asyncio.all_tasks(loop) if not t.done()] == []
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()

    def test_stop_without_start_is_safe(self):
        scheduler.stop()
        scheduler.stop()
