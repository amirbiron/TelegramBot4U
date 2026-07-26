"""טסטים למחיקת נגזרות ולבקשת מחיקה בשפה חופשית (‏T4.1, ‏T4.2).

הכלל שנבדק כאן: **מחיקת הודעה שלא מוחקת את מה שנגזר ממנה אינה מחיקה.**
תוכן שהלקוח מחק ממשיך לחיות בסיכום השיחה ובעובדות הזיכרון, ושניהם
נשלחים ל-LLM בכל פנייה.
"""

import pytest

import control_plane as cp
import database as db
from bot import business_handlers as bh
from bot import owner_commands as oc
from intent import Intent
from tenancy import tenant_context
from tests.doubles import FakeBot, FakeContext

OWNER_ID = 900001
CUSTOMER_ID = 500042
CONNECTION_ID = "conn-demo-0001"


# ─── T4.1 — נגזרות ─────────────────────────────────────────────────────


class TestDeletedMessageDerivatives:
    def _seed(self, chat_id: int = 10) -> int:
        """הודעה + סיכום שכולל אותה + עובדת זיכרון שנגזרה ממנה."""
        msg_id = db.save_message(
            "1", "דנה", "user", "מספר הזהות שלי 123456789",
            tg_chat_id=chat_id, tg_message_id=55,
        )
        db.save_conversation_summary(
            "1", "דנה מסרה את מספר הזהות שלה", 1, last_summarized_message_id=msg_id,
        )
        db.add_customer_fact(
            "1", "personal_info", "מספר זהות 123456789", 0.9,
            source_message_id=msg_id,
        )
        return msg_id

    def test_summary_is_removed(self, default_tenant_db):
        """הסיכום מכיל את התוכן שנמחק, והוא נשלח ל-LLM בכל פנייה."""
        self._seed()
        result = db.delete_messages_by_tg_ids(10, [55])
        assert result["summaries"] == 1
        assert db.get_latest_summary("1") is None

    def test_derived_facts_are_removed(self, default_tenant_db):
        self._seed()
        result = db.delete_messages_by_tg_ids(10, [55])
        assert result["customer_facts"] == 1
        assert db.get_customer_facts("1") == []

    def test_the_message_itself_is_removed(self, default_tenant_db):
        self._seed()
        result = db.delete_messages_by_tg_ids(10, [55])
        assert result["conversations"] == 1
        assert db.get_conversation_history("1") == []

    def test_unrelated_facts_survive(self, default_tenant_db):
        """עובדה שבעל העסק הזין ידנית אינה נגזרת של ההודעה."""
        self._seed()
        db.add_customer_fact(
            "1", "preference", "מעדיפה בוקר", 0.9, source="business_owner",
        )
        db.delete_messages_by_tg_ids(10, [55])
        remaining = db.get_customer_facts("1")
        assert len(remaining) == 1
        assert remaining[0]["content"] == "מעדיפה בוקר"

    def test_another_customer_is_untouched(self, default_tenant_db):
        self._seed()
        other = db.save_message(
            "2", "יוסי", "user", "שלום", tg_chat_id=20, tg_message_id=77,
        )
        db.add_customer_fact("2", "preference", "אוהב ערב", 0.9, source_message_id=other)
        db.save_conversation_summary("2", "יוסי אוהב ערב", 1, last_summarized_message_id=other)

        db.delete_messages_by_tg_ids(10, [55])

        assert len(db.get_customer_facts("2")) == 1
        assert db.get_latest_summary("2") is not None

    def test_nothing_deleted_leaves_everything(self, default_tenant_db):
        self._seed()
        result = db.delete_messages_by_tg_ids(10, [9999])
        assert result == {"conversations": 0, "customer_facts": 0, "summaries": 0}
        assert db.get_latest_summary("1") is not None

    def test_future_summary_rebuilds_from_survivors(self, default_tenant_db):
        """אחרי מחיקת הסיכום, ה-high-water mark מתאפס והבנייה מתחדשת."""
        self._seed()
        db.save_message("1", "דנה", "user", "הודעה ששרדה", tg_chat_id=10, tg_message_id=56)
        db.delete_messages_by_tg_ids(10, [55])
        assert db.get_unsummarized_message_count("1") == 1
        texts = [m["message"] for m in db.get_messages_for_summarization("1", 10)]
        assert texts == ["הודעה ששרדה"]


class TestEditedMessageInvalidatesSummary:
    def test_summarized_message_invalidates(self, default_tenant_db):
        """הנוסח הישן חי בסיכום, והוא זה שנשלח ל-LLM."""
        msg_id = db.save_message(
            "1", "דנה", "user", "הטלפון שלי 0501234567",
            tg_chat_id=10, tg_message_id=55,
        )
        db.save_conversation_summary("1", "דנה מסרה טלפון", 1, last_summarized_message_id=msg_id)

        assert db.invalidate_summary_for_message(10, 55) is True
        assert db.get_latest_summary("1") is None

    def test_unsummarized_message_leaves_the_summary(self, default_tenant_db):
        """עריכה של הודעה טרייה — הנוסח המעודכן ייכנס לסיכום הבא ממילא."""
        old = db.save_message("1", "דנה", "user", "ישנה", tg_chat_id=10, tg_message_id=1)
        db.save_conversation_summary("1", "סיכום", 1, last_summarized_message_id=old)
        db.save_message("1", "דנה", "user", "חדשה", tg_chat_id=10, tg_message_id=2)

        assert db.invalidate_summary_for_message(10, 2) is False
        assert db.get_latest_summary("1") is not None

    def test_unknown_message_is_a_noop(self, default_tenant_db):
        assert db.invalidate_summary_for_message(10, 9999) is False


class TestHandlerWiring:
    @pytest.fixture
    def channel(self, tenant):
        from services import owner_channel

        owner_channel.reset_dedup()
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
        )
        bot = FakeBot()
        return FakeContext(bot), bot

    async def test_delete_handler_removes_derivatives(self, channel):
        import json
        import pathlib

        ctx, _bot = channel
        fixtures = pathlib.Path(__file__).parent / "fixtures"
        raw = json.loads(
            (fixtures / "deleted_business_messages.json").read_text(encoding="utf-8")
        )
        from telegram import Update

        with tenant_context("acme"):
            msg_id = db.save_message(
                str(CUSTOMER_ID), "דנה", "user", "רגיש",
                tg_chat_id=CUSTOMER_ID, tg_message_id=5501,
            )
            db.save_conversation_summary(
                str(CUSTOMER_ID), "סיכום רגיש", 1, last_summarized_message_id=msg_id,
            )
            db.add_customer_fact(
                str(CUSTOMER_ID), "personal_info", "פרט רגיש", 0.9,
                source_message_id=msg_id,
            )

            await bh.on_deleted_business_messages(Update.de_json(raw, None), ctx)

            assert db.get_latest_summary(str(CUSTOMER_ID)) is None
            assert db.get_customer_facts(str(CUSTOMER_ID)) == []


# ─── T4.2 — בקשת מחיקה בשפה חופשית ─────────────────────────────────────


class TestDeleteRequestIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "תמחקו את המידע שלי",
            "אני רוצה שתמחקו הכל",
            "מחק את הפרטים שלי בבקשה",
            "תסירו אותי מהמערכת",
            "delete all my data",
            "please forget everything about me",
        ],
    )
    def test_detected(self, text, monkeypatch):
        import config as _cfg
        from intent import detect_intent_with_llm

        monkeypatch.setattr(_cfg, "LLM_INTENT_ENABLED", False, raising=False)
        assert detect_intent_with_llm(text) == Intent.DELETE_REQUEST

    @pytest.mark.parametrize(
        "text",
        ["כמה עולה תספורת?", "אני רוצה לדבר עם נציג", "מתי אתם פתוחים?"],
    )
    def test_not_over_matching(self, text, monkeypatch):
        import config as _cfg
        from intent import detect_intent_with_llm

        monkeypatch.setattr(_cfg, "LLM_INTENT_ENABLED", False, raising=False)
        assert detect_intent_with_llm(text) != Intent.DELETE_REQUEST

    def test_beats_human_agent(self, monkeypatch):
        """זכות לפי חוק גוברת על בקשה לנציג."""
        import config as _cfg
        from intent import detect_intent_with_llm

        monkeypatch.setattr(_cfg, "LLM_INTENT_ENABLED", False, raising=False)
        assert detect_intent_with_llm(
            "תמחקו את המידע שלי ואני רוצה לדבר עם נציג"
        ) == Intent.DELETE_REQUEST

    def test_forces_handoff_without_promising(self, default_tenant_db, monkeypatch):
        """הבוט לא אומר "מחקתי" — הוא לא מחק כלום."""
        from core import message_processor as mp

        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "בטח, מחקתי הכל!", "kb_empty": False,
                          "kb_tokens": 1, "llm_failed": False},
        )
        result = mp.process_incoming_message(
            "1", "תמחקו את המידע שלי", {"display_name": "דנה"},
        )
        assert result.action == "handoff"
        assert result.intent == Intent.DELETE_REQUEST

    def test_the_llm_answer_is_replaced_not_just_backfilled(
        self, default_tenant_db, monkeypatch,
    ):
        """‏handoff לבדו לא מספיק — הטקסט ללקוח היה נשאר תשובת ה-LLM.

        מודל שנשאל "תמחקו את המידע שלי" עונה באופן טבעי "בוצע, הכול
        נמחק". זו הבטחה שקרית על פעולה שדורשת אישור אנושי ועוד לא
        קרתה, והיא נשלחת ללקוח כלשונה כל עוד היא אינה ריקה.
        """
        from core import message_processor as mp

        db.update_bot_settings(handoff_bridge_message="אבדוק ואחזור אליך.")
        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "בטח, מחקתי הכל!", "kb_empty": False,
                          "kb_tokens": 1, "llm_failed": False},
        )
        result = mp.process_incoming_message(
            "1", "תמחקו את המידע שלי", {"display_name": "דנה"},
        )
        assert result.action == "handoff"
        assert "מחקתי" not in result.text
        assert result.text == "אבדוק ואחזור אליך."

    def test_other_handoffs_keep_the_llm_answer(self, default_tenant_db, monkeypatch):
        """ההחלפה חלה על בקשת מחיקה בלבד ולא על כל handoff."""
        from core import message_processor as mp

        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "[HANDOFF] אבדוק מול בעל העסק", "kb_empty": False,
                          "kb_tokens": 1, "llm_failed": False},
        )
        result = mp.process_incoming_message(
            "1", "אני רוצה לדבר עם נציג", {"display_name": "דנה"},
        )
        assert result.action == "handoff"
        assert "אבדוק מול בעל העסק" in result.text

    def test_nothing_is_deleted_automatically(self, default_tenant_db, monkeypatch):
        from core import message_processor as mp

        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "אבדוק", "kb_empty": False,
                          "kb_tokens": 1, "llm_failed": False},
        )
        db.upsert_user("1", "דנה", inbound=True)
        db.save_message("1", "דנה", "user", "היסטוריה")
        mp.process_incoming_message("1", "תמחקו את המידע שלי", {"display_name": "דנה"})
        assert db.get_user("1") is not None
        assert db.get_conversation_history("1")


class TestOwnerApproval:
    @pytest.fixture
    def owner_chat(self, tenant):
        from services import owner_channel

        owner_channel.reset_dedup()
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
        )
        return cp.get_business_connection(CONNECTION_ID)

    def _msg(self, text: str, reply_to=None):
        class M:
            def __init__(self):
                self.text = text
                self.from_user = type("U", (), {"id": OWNER_ID})()
                self.reply_to_message = reply_to
                self.replies: list[str] = []

            async def reply_text(self, t, **kw):
                self.replies.append(t)

        return M()

    def _replied(self, message_id: int = 4242):
        return type(
            "R", (),
            {"message_id": message_id, "chat": type("C", (), {"id": OWNER_ID})()},
        )()

    async def test_delete_without_reply_deletes_nothing(self, owner_chat):
        with tenant_context("acme"):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
        msg = self._msg("/delete")
        with tenant_context("acme"):
            await oc.on_owner_command(type("U", (), {"message": msg})(), None)
            assert db.get_user(str(CUSTOMER_ID)) is not None
        assert "בתגובה" in msg.replies[0]

    async def test_delete_in_reply_removes_the_customer(self, owner_chat):
        with tenant_context("acme"):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
            db.save_message(str(CUSTOMER_ID), "דנה", "user", "היסטוריה")
            db.record_owner_alert_target(
                4242, str(CUSTOMER_ID), str(CUSTOMER_ID), owner_chat_id=str(OWNER_ID),
            )

        msg = self._msg("/delete", reply_to=self._replied())
        with tenant_context("acme"):
            await oc.on_owner_command(type("U", (), {"message": msg})(), None)
            assert db.get_user(str(CUSTOMER_ID)) is None
            assert db.get_conversation_history(str(CUSTOMER_ID)) == []
        assert "נמחק" in msg.replies[0]

    async def test_non_owner_cannot_delete(self, owner_chat):
        with tenant_context("acme"):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
            db.record_owner_alert_target(
                4242, str(CUSTOMER_ID), str(CUSTOMER_ID), owner_chat_id=str(OWNER_ID),
            )

        msg = self._msg("/delete", reply_to=self._replied())
        msg.from_user = type("U", (), {"id": 777777})()
        with tenant_context("acme"):
            await oc.on_owner_command(type("U", (), {"message": msg})(), None)
            assert db.get_user(str(CUSTOMER_ID)) is not None
        assert msg.replies == []

    async def test_partial_deletion_is_not_reported_as_success(
        self, owner_chat, monkeypatch,
    ):
        """מחיקה חלקית שמדווחת כ"נמחק" גורמת לבעלים לשקר ללקוח.

        ‏`delete_user_data` ממשיך לטבלה הבאה כשאחת נכשלת — וזה נכון,
        עדיף למחוק את מה שאפשר. אבל התוצאה חייבת להיאמר: מול בקשת
        מחיקה לפי חוק, דיווח שגוי גרוע מכישלון גלוי.
        """
        with tenant_context("acme"):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
            db.record_owner_alert_target(
                4242, str(CUSTOMER_ID), str(CUSTOMER_ID), owner_chat_id=str(OWNER_ID),
            )

        monkeypatch.setattr(
            db, "delete_user_data",
            lambda uid: {
                "conversations": 3,
                "__failed_tables__": ["customer_facts", "users"],
                "__deletion_status__": "partial",
            },
        )
        msg = self._msg("/delete", reply_to=self._replied())
        with tenant_context("acme"):
            await oc.on_owner_command(type("U", (), {"message": msg})(), None)

        reply = msg.replies[0]
        assert "חלקית" in reply
        assert "אל תדווח ללקוח" in reply
        # מפתחות הסימון הם פרט מימוש ואסור שיודלפו לבעלים
        assert "__" not in reply

    async def test_deletion_is_recorded_in_the_ledger(self, owner_chat):
        from utils.consent_ledger import get_events_for_subject

        with tenant_context("acme"):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
            db.record_owner_alert_target(
                4242, str(CUSTOMER_ID), str(CUSTOMER_ID), owner_chat_id=str(OWNER_ID),
            )

        msg = self._msg("/delete", reply_to=self._replied())
        with tenant_context("acme"):
            await oc.on_owner_command(type("U", (), {"message": msg})(), None)
            events = get_events_for_subject(str(CUSTOMER_ID), db.CHANNEL)
        assert any(e["event_type"] == "deletion_completed" for e in events)


class TestDeletionRequestAlert:
    async def test_alert_says_how_to_approve(self, tenant):
        from services import owner_channel

        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": CONNECTION_ID, "user_chat_id": OWNER_ID}
        with tenant_context("acme"):
            await owner_channel.notify_deletion_request(
                bot, conn, "דנה", "תמחקו את המידע שלי",
                target=(str(CUSTOMER_ID), str(CUSTOMER_ID)),
            )
        text = bot.owner_messages[0]["text"]
        assert "/delete" in text
        assert "בלתי הפיכה" in text
        # לא הבטחנו ללקוח כלום
        assert "לא הבטחתי" in text

    async def test_repeated_request_is_not_deduped(self, tenant):
        """בקשה חוזרת אינה רעש אלא הסלמה."""
        from services import owner_channel

        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": CONNECTION_ID, "user_chat_id": OWNER_ID}
        with tenant_context("acme"):
            first = await owner_channel.notify_deletion_request(bot, conn, "דנה", "שוב")
            second = await owner_channel.notify_deletion_request(bot, conn, "דנה", "שוב")
        assert first is True
        assert second is True


class TestRequestIsRecordedOnArrival:
    """הבקשה נרשמת ביומן ברגע שהגיעה — לא ברגע שהבעלים אישר.

    זה ההבדל בין "יש לנו הוכחה" ל"יש לנו הוכחה רק כשהתנהגנו יפה":
    דווקא המקרה שבו הבעלים **לא** אישר הוא זה שבו הראיה נחוצה.
    """

    @pytest.fixture
    def wired(self, tenant, monkeypatch):
        from core import message_processor as mp
        from services import owner_channel

        owner_channel.reset_dedup()
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
        )
        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "אבדוק ואחזור", "kb_empty": False,
                          "kb_tokens": 1, "llm_failed": False},
        )
        return cp.get_business_connection(CONNECTION_ID)

    def _incoming(self):
        """הודעת לקוח מינימלית — רק מה ש-`dispatch_result` קורא ממנה."""
        return type(
            "Msg", (),
            {
                "from_user": type("U", (), {"id": CUSTOMER_ID})(),
                "chat": type("C", (), {"id": CUSTOMER_ID})(),
                "business_connection_id": CONNECTION_ID,
            },
        )()

    async def _run(self, conn, bot):
        from bot import dispatch
        from core import message_processor as mp

        result = mp.process_incoming_message(
            str(CUSTOMER_ID), "תמחקו את המידע שלי", {"display_name": "דנה"},
        )
        await dispatch.dispatch_result(bot, result, self._incoming(), conn, "דנה")
        return result

    async def test_recorded_when_the_request_arrives(self, wired):
        from utils.consent_ledger import get_events_for_subject

        bot = FakeBot()
        with tenant_context("acme"):
            await self._run(wired, bot)
            events = get_events_for_subject(str(CUSTOMER_ID), db.CHANNEL)

        requested = [e for e in events if e["event_type"] == "deletion_requested"]
        assert len(requested) == 1
        assert "customer_message" in (requested[0]["metadata_json"] or "")
        # ועדיין לא נמחק כלום — הרישום אינו ביצוע. נבדק על אותו
        # ה-snapshot שנשלף אחרי הריצה; ‏tenant_context כאן היה מטעה,
        # כי `events` כבר בזיכרון ואינו נקרא שוב מה-DB.
        assert not any(e["event_type"] == "deletion_completed" for e in events)

    async def test_recorded_even_when_the_alert_fails(self, wired):
        """ההתראה לבעלים נכשלה — הראיה שהבקשה הוגשה לא הולכת לאיבוד."""
        from utils.consent_ledger import get_events_for_subject

        bot = FakeBot(fail_owner_send=True)
        with tenant_context("acme"):
            await self._run(wired, bot)
            events = get_events_for_subject(str(CUSTOMER_ID), db.CHANNEL)

        assert any(e["event_type"] == "deletion_requested" for e in events)
