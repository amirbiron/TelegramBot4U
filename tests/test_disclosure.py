"""טסטים לשורת הגילוי (‏T4.3).

הדרישה המחייבת: **פעם אחת בלבד פר-לקוח**. שורה שנשלחת פעמיים נקראת
כמו תקלה, ושורה שלא נשלחת בכלל היא כשל בחובת היידוע.
"""

import pytest

import control_plane as cp
import database as db
from bot import dispatch
from core.message_processor import Intent, MessageResult
from services import disclosure, owner_channel
from tenancy import tenant_context
from tests.doubles import FakeBot

OWNER_ID = 900001
CUSTOMER_ID = 500042
CONNECTION_ID = "conn-demo-0001"


class FakeIncoming:
    """‏double להודעה הנכנסת שממנה נגזרים היעדים."""

    def __init__(self, user_id: int = CUSTOMER_ID):
        self.from_user = type("U", (), {"id": user_id})()
        self.chat = type("C", (), {"id": user_id})()
        self.business_connection_id = CONNECTION_ID


@pytest.fixture
def channel(tenant):
    owner_channel.reset_dedup()
    cp.upsert_business_connection(
        CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
        is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
    )
    with tenant_context("acme"):
        db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
    return cp.get_business_connection(CONNECTION_ID)


async def _reply(conn, text: str = "תספורת עולה 120 ש\"ח", bot=None):
    bot = bot or FakeBot()
    with tenant_context("acme"):
        await dispatch.dispatch_result(
            bot, MessageResult(text=text, intent=Intent.GENERAL, action="reply"),
            FakeIncoming(), conn, "דנה",
        )
    return bot


class TestSentExactlyOnce:
    async def test_first_reply_carries_the_line(self, channel):
        bot = await _reply(channel)
        assert "כאן העוזר" in bot.customer_messages[0]["text"]

    async def test_second_reply_does_not(self, channel):
        await _reply(channel)
        bot = await _reply(channel, "ובשבת אנחנו סגורים")
        assert "כאן העוזר" not in bot.customer_messages[0]["text"]

    async def test_the_answer_itself_is_intact(self, channel):
        bot = await _reply(channel, "התשובה שלי")
        text = bot.customer_messages[0]["text"]
        assert text.endswith("התשובה שלי")
        assert "\n\n" in text, "השורה משורשרת עם הפרדה, לא נדבקת לתשובה"

    async def test_it_is_not_a_separate_message(self, channel):
        """הודעה נפרדת נקראת כמו הודעת מערכת — בדיוק מה שנמנעים ממנו."""
        bot = await _reply(channel)
        assert len(bot.customer_messages) == 1

    async def test_a_different_customer_gets_their_own(self, channel):
        await _reply(channel)
        other = FakeIncoming(user_id=777001)
        bot = FakeBot()
        with tenant_context("acme"):
            db.upsert_user("777001", "יוסי", inbound=True)
            await dispatch.dispatch_result(
                bot, MessageResult(text="שלום", intent=Intent.GENERAL, action="reply"), other, channel, "יוסי",
            )
        assert "כאן העוזר" in bot.customer_messages[0]["text"]


class TestNotMarkedOnFailure:
    async def test_failed_send_leaves_it_pending(self, channel):
        """הלקוח לא ראה את השורה — היא חייבת להישלח בפעם הבאה."""
        from telegram.error import BadRequest

        failing = FakeBot(fail_send=BadRequest("Bad Request: chat not found"))
        await _reply(channel, bot=failing)

        with tenant_context("acme"):
            assert disclosure.is_due(str(CUSTOMER_ID)) is True

        bot = await _reply(channel)
        assert "כאן העוזר" in bot.customer_messages[0]["text"]

    async def test_partial_send_leaves_it_pending(self, channel, monkeypatch):
        """ייתכן שהצ'אנק עם השורה לא הגיע — לא מסמנים."""
        from telegram.error import BadRequest

        monkeypatch.setattr(dispatch, "TELEGRAM_MAX_MESSAGE_LENGTH", 60)

        class FailsLater(FakeBot):
            def __init__(self):
                super().__init__()
                self.n = 0

            async def send_message(self, chat_id, text, business_connection_id=None, **kw):
                self.n += 1
                if self.n == 2:
                    raise BadRequest("Bad Request: chat not found")
                return await super().send_message(
                    chat_id, text, business_connection_id, **kw
                )

        await _reply(channel, "משפט ארוך לבדיקה. " * 20, bot=FailsLater())
        with tenant_context("acme"):
            assert disclosure.is_due(str(CUSTOMER_ID)) is True


class TestToggle:
    async def test_disabled_means_no_line(self, channel):
        with tenant_context("acme"):
            db.update_bot_settings(disclosure_enabled=0)
        bot = await _reply(channel)
        assert "כאן העוזר" not in bot.customer_messages[0]["text"]
        assert bot.customer_messages[0]["text"] == "תספורת עולה 120 ש\"ח"

    def test_enabled_by_default(self, tenant):
        with tenant_context("acme"):
            assert disclosure.is_enabled() is True

    def test_toggle_is_recorded_in_the_ledger(self, tenant):
        from utils.consent_ledger import get_events_for_subject

        with tenant_context("acme"):
            disclosure.record_toggle(False, actor="admin")
            events = get_events_for_subject("tenant:admin", db.CHANNEL)
        assert any(e["event_type"] == "disclosure_disabled" for e in events)

    def test_enabling_is_recorded_too(self, tenant):
        """בלי רישום ההדלקה אי אפשר לענות 'ממתי זה היה כבוי'."""
        from utils.consent_ledger import get_events_for_subject

        with tenant_context("acme"):
            disclosure.record_toggle(True, actor="admin")
            events = get_events_for_subject("tenant:admin", db.CHANNEL)
        assert any(e["event_type"] == "disclosure_enabled" for e in events)


class TestTemplate:
    def test_business_name_is_substituted(self, tenant):
        with tenant_context("acme"):
            assert "עסק לדוגמה" in disclosure.render_disclosure()

    def test_custom_template_is_used(self, tenant):
        with tenant_context("acme"):
            db.update_bot_settings(disclosure_template="שלום, אני הבוט של {business}")
            assert disclosure.render_disclosure() == "שלום, אני הבוט של עסק לדוגמה"

    def test_template_without_placeholder_is_sent_verbatim(self, tenant):
        """החלטה של הבעלים — לא סיבה להשתיק את היידוע."""
        with tenant_context("acme"):
            db.update_bot_settings(disclosure_template="הודעה אוטומטית")
            assert disclosure.render_disclosure() == "הודעה אוטומטית"

    def test_missing_business_name_falls_back(self, tenant, monkeypatch):
        import config as _cfg

        monkeypatch.setattr(
            _cfg, "get_business_config",
            lambda: _cfg.BusinessConfig(name="", phone="", address="", website=""),
        )
        with tenant_context("acme"):
            line = disclosure.render_disclosure()
        assert "העוזר האישי" in line
        assert "{business}" not in line

    def test_disabled_renders_nothing(self, tenant):
        with tenant_context("acme"):
            db.update_bot_settings(disclosure_enabled=0)
            assert disclosure.render_disclosure() == ""


class TestFailureModes:
    def test_db_failure_keeps_it_enabled(self, tenant, monkeypatch):
        """כשל בקריאת ההגדרה לא אמור להשתיק חובת יידוע — ‏fail open."""
        monkeypatch.setattr(
            db, "get_bot_settings", lambda: (_ for _ in ()).throw(RuntimeError("נעול")),
        )
        with tenant_context("acme"):
            assert disclosure.is_enabled() is True

    def test_unknown_user_is_due(self, tenant):
        """לקוח שטרם נשמר — השורה חלה עליו."""
        with tenant_context("acme"):
            assert disclosure.is_due("never-seen") is True

    def test_user_read_failure_is_fail_closed(self, tenant, monkeypatch):
        """בספק, עדיף לא לשלוח שוב שורה שכבר נשלחה."""
        monkeypatch.setattr(
            db, "get_user", lambda uid: (_ for _ in ()).throw(RuntimeError("נעול")),
        )
        with tenant_context("acme"):
            assert disclosure.is_due("500042") is False

    def test_prepend_on_empty_text_is_a_noop(self, tenant):
        with tenant_context("acme"):
            assert disclosure.prepend("", "500042") == ""


class TestMigration:
    def test_columns_exist_on_an_existing_db(self, tenant):
        """‏DB שנוצר לפני T4.3 מקבל את העמודות דרך `run_migrations`."""
        with tenant_context("acme"), db.get_connection() as conn:
            users = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            settings = {
                r["name"] for r in conn.execute("PRAGMA table_info(bot_settings)")
            }
        assert "disclosure_sent_at" in users
        assert {"disclosure_enabled", "disclosure_template"} <= settings

    def test_migration_is_idempotent(self, tenant):
        from migrations import run_migrations

        with tenant_context("acme"), db.get_connection() as conn:
            run_migrations(conn)
            run_migrations(conn)
