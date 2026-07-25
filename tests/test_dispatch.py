"""טסטים ל-dispatch היוצא — סיווג כשלים, פיצול הודעות ודה-דופ התראות."""

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

import database as db
from bot import dispatch
from services import owner_channel
from tests.doubles import FakeBot, FakeContext


class TestErrorClassification:
    @pytest.mark.parametrize("message", [
        "Bad Request: BUSINESS_PEER_INVALID",
        "Bad Request: chat not found",
        "Bad Request: the chat must have been active in the last 24 hours",
    ])
    def test_window_closed(self, message):
        assert dispatch.classify_send_error(BadRequest(message)) == \
            dispatch.FAILURE_WINDOW_CLOSED

    @pytest.mark.parametrize("exc_type,message", [
        (BadRequest, "Bad Request: not enough rights to send text messages"),
        (BadRequest, "Bad Request: BUSINESS_CONNECTION_INVALID"),
        # טלגרם מחזירה את זה כ-Forbidden, לא כ-BadRequest — הסיווג חייב
        # לעבוד על הטיפוס האמיתי ולא רק על הטקסט
        (Forbidden, "Forbidden: bot can_reply right is missing"),
    ])
    def test_no_permission(self, exc_type, message):
        assert dispatch.classify_send_error(exc_type(message)) == \
            dispatch.FAILURE_NO_PERMISSION

    def test_forbidden_defaults_to_no_permission(self):
        assert dispatch.classify_send_error(Forbidden("blocked by user")) == \
            dispatch.FAILURE_NO_PERMISSION

    def test_unknown_error_is_other(self):
        """ברירת מחדל בטוחה: כשל שלא מזוהה מסווג כ-other ומדווח לבעלים."""
        assert dispatch.classify_send_error(NetworkError("timeout")) == \
            dispatch.FAILURE_OTHER


class TestSplitMessage:
    def test_short_message_unchanged(self):
        assert dispatch.split_message("שלום") == ["שלום"]

    def test_empty_returns_empty(self):
        assert dispatch.split_message("") == []
        assert dispatch.split_message("   ") == []

    def test_splits_on_paragraph(self):
        text = ("א" * 30 + "\n\n") * 10
        parts = dispatch.split_message(text, limit=100)
        assert all(len(p) <= 100 for p in parts)
        assert "".join(parts).replace("\n", "") == text.replace("\n", "")

    def test_splits_on_sentence_when_no_paragraph(self):
        text = ("משפט מספר אחד. " * 20).strip()
        parts = dispatch.split_message(text, limit=100)
        assert all(len(p) <= 100 for p in parts)
        assert parts[0].endswith(".")

    def test_never_cuts_midword_when_possible(self):
        text = " ".join(["מילה"] * 100)
        parts = dispatch.split_message(text, limit=60)
        for p in parts:
            assert not p.startswith(" ")
            assert "מיל " not in p


class TestSendToCustomer:
    async def test_sends_with_connection_id(self, default_tenant_db):
        bot = FakeBot()
        db.upsert_user("1", "דנה", inbound=True)
        sent = await dispatch.send_to_customer(bot, 10, "conn-1", "שלום", "1", "דנה")
        assert sent == "שלום"
        assert bot.messages[0]["business_connection_id"] == "conn-1"

    async def test_long_message_split_into_chunks(self, default_tenant_db, monkeypatch):
        bot = FakeBot()
        monkeypatch.setattr(dispatch, "TELEGRAM_MAX_MESSAGE_LENGTH", 100)
        db.upsert_user("1", "דנה", inbound=True)
        text = ("משפט ארוך מאוד לצורך הבדיקה. " * 20).strip()
        await dispatch.send_to_customer(bot, 10, "conn-1", text, "1", "דנה")
        assert len(bot.messages) > 1
        assert all(len(m["text"]) <= 100 for m in bot.messages)

    async def test_window_closed_marks_and_notifies(self, default_tenant_db):
        owner_channel.reset_dedup()
        bot = FakeBot(fail_send=BadRequest("Bad Request: BUSINESS_PEER_INVALID"))
        db.upsert_user("1", "דנה", inbound=True)
        conn = {"connection_id": "conn-1", "user_chat_id": 999}

        sent = await dispatch.send_to_customer(bot, 10, "conn-1", "שלום", "1", "דנה", conn)

        # שום צ'אנק לא נמסר ⇒ מחרוזת ריקה (falsy, כמו ה-False הקודם)
        assert sent == ""
        assert db.get_user("1")["send_failure_reason"] == dispatch.FAILURE_WINDOW_CLOSED
        # הלקוח לא קיבל כלום; הבעלים כן
        assert bot.customer_messages == []
        assert len(bot.owner_messages) == 1
        assert "24 שעות" in bot.owner_messages[0]["text"]

    async def test_no_permission_marks_and_notifies(self, default_tenant_db):
        owner_channel.reset_dedup()
        bot = FakeBot(fail_send=BadRequest("Bad Request: not enough rights"))
        db.upsert_user("1", "דנה", inbound=True)
        conn = {"connection_id": "conn-1", "user_chat_id": 999}

        await dispatch.send_to_customer(bot, 10, "conn-1", "שלום", "1", "דנה", conn)

        assert db.get_user("1")["send_failure_reason"] == dispatch.FAILURE_NO_PERMISSION
        assert "הרשאה" in bot.owner_messages[0]["text"]

    async def test_no_blind_retry_on_failure(self, default_tenant_db):
        """כשל שליחה לא מייצר ניסיון חוזר — הוא ייכשל באותה צורה."""
        attempts = {"n": 0}

        class CountingBot(FakeBot):
            async def send_message(self, chat_id, text, business_connection_id=None, **kw):
                if business_connection_id:
                    attempts["n"] += 1
                    raise BadRequest("Bad Request: BUSINESS_PEER_INVALID")
                return await super().send_message(
                    chat_id, text, business_connection_id, **kw
                )

        db.upsert_user("1", "דנה", inbound=True)
        await dispatch.send_to_customer(
            CountingBot(), 10, "conn-1", "שלום", "1", "דנה",
            {"connection_id": "conn-1", "user_chat_id": 999},
        )
        assert attempts["n"] == 1

    async def test_retry_after_is_honored_once(self, default_tenant_db, monkeypatch):
        """‏RetryAfter הוא היוצא מן הכלל: טלגרם אמרה כמה להמתין."""
        import asyncio

        calls = {"n": 0}
        slept: list[float] = []

        class FloodBot(FakeBot):
            async def send_message(self, chat_id, text, business_connection_id=None, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RetryAfter(2)
                return await super().send_message(
                    chat_id, text, business_connection_id, **kw
                )

        async def _fake_sleep(seconds):
            # אוגרים כל המתנה: הפאוזה בין צ'אנקים קוראת לאותו sleep,
            # וקודם רק הערך האחרון נשמר — כך שהבדיקה על ההמתנה של
            # טלגרם הייתה עלולה לעבור על ערך שאינו שלה.
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
        db.upsert_user("1", "דנה", inbound=True)
        bot = FloodBot()
        sent = await dispatch.send_to_customer(bot, 10, "conn-1", "שלום", "1", "דנה")
        assert sent == "שלום"
        assert calls["n"] == 2
        assert any(s >= 2 for s in slept), f"לא הייתה המתנה של 2ש' לפחות: {slept}"


class TestOwnerChannelDedup:
    async def test_same_kind_suppressed_within_window(self, default_tenant_db):
        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}
        assert await owner_channel.notify_missing_permission(bot, conn) is True
        assert await owner_channel.notify_missing_permission(bot, conn) is False
        assert len(bot.messages) == 1

    async def test_different_subject_not_suppressed(self, default_tenant_db):
        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}
        await owner_channel.notify_window_closed(bot, conn, "דנה")
        await owner_channel.notify_window_closed(bot, conn, "יוסי")
        assert len(bot.messages) == 2

    async def test_handoff_never_deduped(self, default_tenant_db):
        """כל פנייה שדורשת את הבעלים היא אירוע נפרד — אסור לבלוע."""
        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}
        await owner_channel.notify_handoff(bot, conn, "דנה", "שאלה ראשונה")
        await owner_channel.notify_handoff(bot, conn, "דנה", "שאלה שנייה")
        assert len(bot.messages) == 2

    async def test_missing_chat_id_is_not_a_crash(self, default_tenant_db):
        bot = FakeBot()
        assert await owner_channel.notify(bot, {"connection_id": "c"}, "טקסט") is False

    async def test_owner_message_has_no_connection_id(self, default_tenant_db):
        """הודעה לבעלים יוצאת מהבוט — **בלי** business_connection_id."""
        owner_channel.reset_dedup()
        bot = FakeBot()
        await owner_channel.notify_handoff(
            bot, {"connection_id": "c", "user_chat_id": 999}, "דנה", "שאלה",
        )
        assert bot.messages[0]["business_connection_id"] is None

    async def test_long_question_truncated(self, default_tenant_db):
        owner_channel.reset_dedup()
        bot = FakeBot()
        await owner_channel.notify_handoff(
            bot, {"connection_id": "c", "user_chat_id": 999}, "דנה", "א" * 500,
        )
        assert "…" in bot.messages[0]["text"]
