"""טסטים ל-handlers של ערוץ ה-Secretary.

הטסטים רצים על ה-fixtures האמיתיים של עדכוני Business (‏tests/fixtures),
נפרסים דרך `telegram.Update.de_json` — כך שאם מבנה העדכון של טלגרם
ישתנה, הטסטים ייפלו ולא הפרודקשן.

אין רשת: ה-`Bot` מוחלף ב-double שמתעד קריאות.
"""

import json
import pathlib

import pytest

import control_plane as cp
import database as db
from bot import business_handlers as bh
from services import owner_channel, takeover_service
from tenancy import tenant_context

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

OWNER_ID = 900001
CUSTOMER_ID = 500042
CONNECTION_ID = "conn-demo-0001"


def load_update(name: str):
    from telegram import Update

    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return Update.de_json(data, None)


class FakeBot:
    """‏double ל-`telegram.Bot` שמתעד קריאות במקום לבצע אותן."""

    def __init__(self, fail_send: Exception | None = None):
        self.messages: list[dict] = []
        self.actions: list[dict] = []
        self.fail_send = fail_send

    async def send_message(self, chat_id, text, business_connection_id=None, **kwargs):
        if self.fail_send is not None and business_connection_id is not None:
            raise self.fail_send
        self.messages.append({
            "chat_id": chat_id, "text": text,
            "business_connection_id": business_connection_id,
        })
        return None

    async def send_chat_action(self, chat_id, action, business_connection_id=None, **kwargs):
        self.actions.append({
            "chat_id": chat_id, "action": action,
            "business_connection_id": business_connection_id,
        })
        return None

    # ── עזרי בדיקה ──
    @property
    def customer_messages(self) -> list[dict]:
        """הודעות שיצאו ללקוח (עם business_connection_id)."""
        return [m for m in self.messages if m["business_connection_id"]]

    @property
    def owner_messages(self) -> list[dict]:
        """הודעות שיצאו לצ'אט הבעלים (בלי business_connection_id)."""
        return [m for m in self.messages if not m["business_connection_id"]]


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


@pytest.fixture
def channel(tenant, monkeypatch):
    """‏tenant עם חיבור Secretary פעיל, ובוט מזויף. מחזיר (ctx, bot)."""
    owner_channel.reset_dedup()
    cp.upsert_business_connection(
        CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
        is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
    )
    bot = FakeBot()
    return FakeContext(bot), bot


@pytest.fixture
def fake_llm(monkeypatch):
    """‏generate_answer מוחלף — אין קריאות רשת."""
    from core import message_processor as mp

    state = {"answer": "תספורת עולה 120 ש\"ח"}
    monkeypatch.setattr(
        mp, "generate_answer",
        lambda **kw: {"answer": state["answer"], "kb_empty": False,
                      "kb_tokens": 5, "llm_failed": False},
    )
    # הסיכום ברקע פותח thread — מנטרלים אותו בטסטים
    monkeypatch.setattr(bh, "_schedule_summary", lambda user_id: None)
    return state


class TestFixtures:
    """ה-fixtures חייבים להיפרס למבנה שה-handlers מצפים לו."""

    @pytest.mark.parametrize("name,attr", [
        ("business_connection.json", "business_connection"),
        ("business_message_customer.json", "business_message"),
        ("business_message_owner.json", "business_message"),
        ("business_message_offline.json", "business_message"),
        ("business_message_media.json", "business_message"),
        ("edited_business_message.json", "edited_business_message"),
        ("deleted_business_messages.json", "deleted_business_messages"),
    ])
    def test_parses(self, name, attr):
        assert getattr(load_update(name), attr) is not None

    def test_offline_flag_present(self):
        assert load_update("business_message_offline.json").business_message.is_from_offline

    def test_media_message_has_no_text(self):
        assert load_update("business_message_media.json").business_message.text is None


class TestConnectionHandler:
    async def test_new_connection_stored(self, tenant):
        owner_channel.reset_dedup()
        bot = FakeBot()
        with tenant_context("acme"):
            await bh.on_business_connection(
                load_update("business_connection.json"), FakeContext(bot),
            )
        row = cp.get_business_connection(CONNECTION_ID)
        assert row["tenant_id"] == "acme"
        assert row["owner_user_id"] == OWNER_ID
        assert row["can_reply"] == 1
        assert row["user_chat_id"] == OWNER_ID
        assert "can_reply" in row["rights_json"]
        # הבעלים מקבל אישור בצ'אט הבוט שלו
        assert len(bot.owner_messages) == 1

    async def test_revocation_marks_disabled(self, tenant):
        owner_channel.reset_dedup()
        bot = FakeBot()
        with tenant_context("acme"):
            await bh.on_business_connection(
                load_update("business_connection.json"), FakeContext(bot),
            )
            await bh.on_business_connection(
                load_update("business_connection_revoked.json"), FakeContext(bot),
            )
        row = cp.get_business_connection(CONNECTION_ID)
        assert row["is_enabled"] == 0
        assert row["can_reply"] == 0

    async def test_different_owner_rejected(self, tenant):
        """אימות הבעלים: משתמש אחר לא יכול להשתלט על החיבור (fail closed)."""
        owner_channel.reset_dedup()
        bot = FakeBot()
        with tenant_context("acme"):
            await bh.on_business_connection(
                load_update("business_connection.json"), FakeContext(bot),
            )
            update = load_update("business_connection.json")
            update.business_connection.user._id_attrs = None
            object.__setattr__(update.business_connection.user, "id", 123456)
            await bh.on_business_connection(update, FakeContext(bot))
        assert cp.get_business_connection(CONNECTION_ID)["owner_user_id"] == OWNER_ID


class TestOwnerDetection:
    async def test_owner_message_triggers_takeover(self, channel):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(
                load_update("business_message_owner.json"), ctx,
            )
            assert takeover_service.is_paused(str(CUSTOMER_ID)) is True
            # ההודעה נשמרה כתשובה שהבעלים כתב
            history = db.get_conversation_history(str(CUSTOMER_ID))
            assert history[-1]["authored_by"] == "owner"
        assert bot.customer_messages == []

    async def test_offline_message_is_not_takeover(self, channel, fake_llm):
        """הודעה אוטומטית של טלגרם (away/greeting) — **לא** התערבות אנושית.

        זה הבאג שסינון לפי from.id בלבד היה מייצר: הבוט היה משתיק את
        עצמו בכל פעם שטלגרם שולחת הודעת "אני לא זמין".
        """
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(
                load_update("business_message_offline.json"), ctx,
            )
            assert takeover_service.is_paused(str(CUSTOMER_ID)) is False

    async def test_message_from_business_bot_is_not_takeover(self, channel):
        """הגנת עומק: הודעה שנשלחה ע"י בוט עסקי אינה התערבות של הבעלים."""
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(
                load_update("business_message_from_bot.json"), ctx,
            )
            assert takeover_service.is_paused(str(CUSTOMER_ID)) is False

    async def test_bot_is_silent_while_owner_is_in(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_owner.json"), ctx)
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            # ההודעה נשמרה, אבל אין תשובה — ואין שום הודעת מערכת ללקוח
            assert bot.customer_messages == []
            assert any(
                m["message"] == "היי, כמה עולה תספורת?"
                for m in db.get_conversation_history(str(CUSTOMER_ID))
            )

    async def test_bot_returns_after_timeout(self, channel, fake_llm, monkeypatch):
        ctx, bot = channel
        import config

        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_owner.json"), ctx)
            with db.get_connection() as conn:
                conn.execute(
                    "UPDATE live_chats SET updated_at = datetime('now', '-121 minutes')"
                )
            monkeypatch.setattr(config, "TAKEOVER_TIMEOUT_MINUTES", 120)
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
        assert len(bot.customer_messages) == 1


class TestIncomingFlow:
    async def test_customer_message_gets_answer(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)

        assert len(bot.customer_messages) == 1
        sent = bot.customer_messages[0]
        assert sent["text"] == "תספורת עולה 120 ש\"ח"
        # כל קריאה יוצאת ללקוח נושאת את מזהה החיבור
        assert sent["business_connection_id"] == CONNECTION_ID
        assert sent["chat_id"] == CUSTOMER_ID

    async def test_typing_action_sent_with_connection_id(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
        assert bot.actions
        assert bot.actions[0]["business_connection_id"] == CONNECTION_ID

    async def test_incoming_saved_and_window_updated(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            user = db.get_user(str(CUSTOMER_ID))
            assert user["last_inbound_at"] is not None
            assert db.is_within_reply_window(str(CUSTOMER_ID)) is True
            history = db.get_conversation_history(str(CUSTOMER_ID))
            assert history[0]["role"] == "user"
            assert history[0]["authored_by"] == "customer"

    async def test_unknown_connection_dropped(self, tenant, fake_llm):
        """הגנת cross-wiring: חיבור שלא רשום ל-tenant הזה — אין תשובה."""
        bot = FakeBot()
        with tenant_context("acme"):
            await bh.on_business_message(
                load_update("business_message_customer.json"), FakeContext(bot),
            )
            assert db.get_conversation_history(str(CUSTOMER_ID)) == []
        assert bot.messages == []

    async def test_connection_of_other_tenant_rejected(self, channel, fake_llm):
        """אותו connection_id שנרשם ל-tenant אחר — נדחה."""
        cp.create_tenant("beta", "עסק אחר")
        ctx, bot = channel
        with tenant_context("beta"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
        assert bot.messages == []

    async def test_disabled_connection_dropped(self, channel, fake_llm):
        ctx, bot = channel
        cp.disable_business_connection(CONNECTION_ID)
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
        assert bot.messages == []

    async def test_blocked_user_gets_silence(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            db.block_user(str(CUSTOMER_ID), "דנה", block_category="spam")
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            # שקט מוחלט — אבל ההודעה נשמרה
            assert bot.messages == []
            assert len(db.get_conversation_history(str(CUSTOMER_ID))) == 1

    async def test_rate_limited_is_silent_and_notifies_owner(self, channel, fake_llm, monkeypatch):
        ctx, bot = channel
        import rate_limiter

        monkeypatch.setattr(rate_limiter, "_WINDOWS", [(60, 1, "minute")])
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
        # תשובה אחת ללקוח בלבד, והבעלים קיבל התראה
        assert len(bot.customer_messages) == 1
        assert len(bot.owner_messages) == 1
        assert "הרבה הודעות" in bot.owner_messages[0]["text"]

    async def test_missing_can_reply_is_silent_and_notifies(self, tenant, fake_llm):
        owner_channel.reset_dedup()
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=True, can_reply=False,
        )
        bot = FakeBot()
        with tenant_context("acme"):
            await bh.on_business_message(
                load_update("business_message_customer.json"), FakeContext(bot),
            )
        assert bot.customer_messages == []
        assert len(bot.owner_messages) == 1
        assert "הרשאה" in bot.owner_messages[0]["text"]

    async def test_autopilot_off_is_silent(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            db.update_bot_settings(autopilot_enabled=0)
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
        assert bot.messages == []


class TestHandoff:
    async def test_handoff_notifies_owner_and_bridges_customer(self, channel, fake_llm):
        import config

        ctx, bot = channel
        fake_llm["answer"] = f"{config.HANDOFF_MARKER}\n\nבודק ואחזור אליך בהקדם"
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            gaps = db.get_unanswered_questions()

        assert len(bot.customer_messages) == 1
        # הטוקן לעולם לא דולף ללקוח
        assert config.HANDOFF_MARKER not in bot.customer_messages[0]["text"]
        assert len(bot.owner_messages) == 1
        assert "כמה עולה תספורת" in bot.owner_messages[0]["text"]
        assert len(gaps) == 1

    async def test_escalation_silences_chat(self, channel, fake_llm):
        import config

        ctx, bot = channel
        fake_llm["answer"] = f"{config.HANDOFF_MARKER}\n\nבודק"
        with tenant_context("acme"):
            for _ in range(3):
                await bh.on_business_message(
                    load_update("business_message_customer.json"), ctx,
                )
            assert takeover_service.is_paused(str(CUSTOMER_ID)) is True
            assert db.get_active_live_chat(str(CUSTOMER_ID))["started_by"] == "handoff"


class TestMedia:
    async def test_media_bridges_and_notifies(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            db.update_bot_settings(media_bridge_message="קיבלתי, אעבור על זה")
            await bh.on_business_message(load_update("business_message_media.json"), ctx)

            assert len(bot.customer_messages) == 1
            assert bot.customer_messages[0]["text"] == "קיבלתי, אעבור על זה"
            assert len(bot.owner_messages) == 1
            assert "קובץ" in bot.owner_messages[0]["text"]
            # לא שומרים מדיה — רק placeholder
            history = db.get_conversation_history(str(CUSTOMER_ID))
            assert history[0]["message"] == "[מדיה]"


class TestEditAndDelete:
    async def test_edit_updates_stored_copy(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            await bh.on_edited_business_message(load_update("edited_business_message.json"), ctx)
            texts = [m["message"] for m in db.get_conversation_history(str(CUSTOMER_ID))]
        assert "היי, כמה עולה תספורת וצבע?" in texts
        assert "היי, כמה עולה תספורת?" not in texts

    async def test_delete_removes_copies(self, channel, fake_llm):
        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            await bh.on_business_message(load_update("business_message_owner.json"), ctx)
            await bh.on_deleted_business_messages(
                load_update("deleted_business_messages.json"), ctx,
            )
            remaining = db.get_conversation_history(str(CUSTOMER_ID))
        # שתי ההודעות שהלקוח מחק נעלמו; התשובה של הבוט (בלי message_id) נשארה
        assert all(m["message"] != "היי, כמה עולה תספורת?" for m in remaining)

    async def test_delete_writes_to_consent_ledger(self, channel, fake_llm):
        from utils.consent_ledger import get_events_for_subject

        ctx, bot = channel
        with tenant_context("acme"):
            await bh.on_business_message(load_update("business_message_customer.json"), ctx)
            await bh.on_deleted_business_messages(
                load_update("deleted_business_messages.json"), ctx,
            )
            events = get_events_for_subject(str(CUSTOMER_ID), db.CHANNEL)
        assert any(e["event_type"] == "deletion_completed" for e in events)

    async def test_delete_from_unknown_connection_ignored(self, tenant):
        bot = FakeBot()
        with tenant_context("acme"):
            db.save_message("1", "x", "user", "טקסט", tg_chat_id=CUSTOMER_ID, tg_message_id=5501)
            await bh.on_deleted_business_messages(
                load_update("deleted_business_messages.json"), FakeContext(bot),
            )
            assert len(db.get_conversation_history("1")) == 1
