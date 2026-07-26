"""טסטים לפקודות הבעלים בצ'אט הבוט-הבן (‏T3.3).

הצ'אט הזה הוא בוט↔בעלים ולא צ'אט לקוח. שתי התכונות שנבדקות כאן הן
מה שמפריד בין השניים: זיהוי הבעלים (מי שאינו הוא — שתיקה), והעובדה
ששום דבר מכאן לא מגיע ללקוח.
"""

import pytest

import control_plane as cp
import database as db
from bot import owner_commands as oc
from services import owner_channel, takeover_service
from tenancy import tenant_context
from tests.doubles import FakeBot

OWNER_ID = 900001
CUSTOMER_ID = 500042
CUSTOMER_CHAT = "500042"
CONNECTION_ID = "conn-demo-0001"


class FakeMessage:
    """‏double ל-`telegram.Message` בצ'אט הבעלים."""

    def __init__(self, text: str, from_id: int = OWNER_ID, reply_to=None):
        self.text = text
        self.from_user = type("U", (), {"id": from_id})()
        self.reply_to_message = reply_to
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return None


def _replied(message_id: int, chat_id: int = OWNER_ID):
    """‏double להודעה שהבעלים הגיב עליה — עם `chat`, כמו ב-API האמיתי."""
    return type(
        "RepliedMessage", (),
        {"message_id": message_id, "chat": type("Chat", (), {"id": chat_id})()},
    )()


class FakeUpdate:
    def __init__(self, message):
        self.message = message


@pytest.fixture
def owner_chat(tenant):
    """‏tenant עם חיבור פעיל. מחזיר את רשומת החיבור."""
    owner_channel.reset_dedup()
    cp.upsert_business_connection(
        CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
        is_enabled=True, can_reply=True, rights_json='{"can_reply": true}',
    )
    return cp.get_business_connection(CONNECTION_ID)


async def _run(text: str, from_id: int = OWNER_ID, reply_to=None) -> FakeMessage:
    msg = FakeMessage(text, from_id=from_id, reply_to=reply_to)
    with tenant_context("acme"):
        await oc.on_owner_command(FakeUpdate(msg), None)
    return msg


class TestOwnerIdentification:
    async def test_non_owner_gets_nothing(self, owner_chat):
        """שתיקה ולא הודעת שגיאה: מי שמצא את הבוט לא צריך לדעת מה יש בו."""
        msg = await _run("/status", from_id=777777)
        assert msg.replies == []

    async def test_owner_gets_an_answer(self, owner_chat):
        msg = await _run("/status")
        assert len(msg.replies) == 1

    async def test_no_connection_means_silence(self, tenant):
        """בלי חיבור רשום אין ממי לאמת בעלות — ‏fail closed."""
        msg = await _run("/status")
        assert msg.replies == []

    async def test_unknown_command_is_ignored(self, owner_chat):
        msg = await _run("/delete_everything")
        assert msg.replies == []

    async def test_command_with_bot_suffix_works(self, owner_chat):
        """`/status@acme_bot` — הצורה שטלגרם מייצרת בקבוצות."""
        msg = await _run("/status@acme_bot")
        assert len(msg.replies) == 1


class TestPauseResume:
    async def test_pause_disables_autopilot_globally(self, owner_chat):
        with tenant_context("acme"):
            assert db.is_autopilot_enabled() is True
        msg = await _run("/pause")
        with tenant_context("acme"):
            assert db.is_autopilot_enabled() is False
        assert "כיביתי" in msg.replies[0]

    async def test_resume_restores_autopilot(self, owner_chat):
        await _run("/pause")
        msg = await _run("/resume")
        with tenant_context("acme"):
            assert db.is_autopilot_enabled() is True
        assert "חזרתי" in msg.replies[0]

    async def test_resume_when_already_on_says_so(self, owner_chat):
        msg = await _run("/resume")
        assert "כבר היה דלוק" in msg.replies[0]

    async def test_pause_hints_about_the_targeted_form(self, owner_chat):
        """הבעלים שרצה להשתיק לקוח אחד וכיבה את הכול צריך לדעת מיד."""
        msg = await _run("/pause")
        assert "/pause בתגובה" in msg.replies[0]


class TestTargetedPause:
    """‏`/pause` בתגובה להתראה — משתיק שיחה אחת בלבד."""

    def _record_alert(self, message_id: int = 4242, owner_chat: int = OWNER_ID):
        with tenant_context("acme"):
            db.record_owner_alert_target(
                message_id, str(CUSTOMER_ID), CUSTOMER_CHAT,
                owner_chat_id=str(owner_chat),
            )
        return _replied(message_id, owner_chat)

    async def test_pause_in_reply_silences_only_that_chat(self, owner_chat):
        replied = self._record_alert()
        msg = await _run("/pause", reply_to=replied)

        with tenant_context("acme"):
            assert takeover_service.is_paused(CUSTOMER_CHAT) is True
            # ה-autopilot הגלובלי **לא** נגע
            assert db.is_autopilot_enabled() is True
        assert "שקט בשיחה הזו" in msg.replies[0]

    async def test_resume_in_reply_restores_only_that_chat(self, owner_chat):
        replied = self._record_alert()
        await _run("/pause", reply_to=replied)
        msg = await _run("/resume", reply_to=replied)

        with tenant_context("acme"):
            assert takeover_service.is_paused(CUSTOMER_CHAT) is False
        assert "בשיחה הזו" in msg.replies[0]

    async def test_reply_to_unknown_message_falls_back_to_global(self, owner_chat):
        """תגובה להודעה שאינה התראה שלנו — פעולה גלובלית, לא קריסה."""
        replied = _replied(999999)
        msg = await _run("/pause", reply_to=replied)
        with tenant_context("acme"):
            assert db.is_autopilot_enabled() is False
        assert "כיביתי" in msg.replies[0]


class TestStatus:
    async def test_reports_connection_and_permission(self, owner_chat):
        msg = await _run("/status")
        text = msg.replies[0]
        assert "מחובר לחשבון שלך" in text
        assert "מותר לי לענות בשמך" in text

    async def test_reports_missing_permission(self, tenant):
        cp.upsert_business_connection(
            CONNECTION_ID, "acme", owner_user_id=OWNER_ID, user_chat_id=OWNER_ID,
            is_enabled=True, can_reply=False, rights_json="{}",
        )
        msg = await _run("/status")
        assert "אין לי הרשאה לענות" in msg.replies[0]

    async def test_reports_paused_autopilot(self, owner_chat):
        await _run("/pause")
        msg = await _run("/status")
        assert "כבוי" in msg.replies[0]

    async def test_counts_answered_messages(self, owner_chat):
        with tenant_context("acme"):
            db.upsert_user(str(CUSTOMER_ID), "דנה", inbound=True)
            db.save_message(str(CUSTOMER_ID), "דנה", "user", "שאלה", authored_by="customer")
            db.save_message(str(CUSTOMER_ID), "דנה", "assistant", "תשובה", authored_by="bot")
        msg = await _run("/status")
        assert "עניתי על 1 הודעות" in msg.replies[0]

    async def test_counts_waiting_conversations(self, owner_chat):
        with tenant_context("acme"):
            db.start_live_chat(CUSTOMER_CHAT, str(CUSTOMER_ID), "דנה", started_by="handoff")
        msg = await _run("/status")
        assert "ממתינות לך" in msg.replies[0]


class TestAlertTargetMapping:
    async def test_handoff_alert_records_its_target(self, owner_chat, default_tenant_db):
        bot = FakeBot()
        conn = {"connection_id": CONNECTION_ID, "user_chat_id": OWNER_ID}
        with tenant_context("acme"):
            await owner_channel.notify_handoff(
                bot, conn, "דנה", "יש מלאי?", target=(str(CUSTOMER_ID), CUSTOMER_CHAT),
            )
            # ה-FakeBot מחזיר message_id עוקב
            target = db.get_owner_alert_target(
                bot.messages[0]["message_id"], owner_chat_id=str(OWNER_ID),
            )
        assert target == {"user_id": str(CUSTOMER_ID), "chat_id": CUSTOMER_CHAT}

    async def test_alert_without_target_records_nothing(self, owner_chat):
        bot = FakeBot()
        conn = {"connection_id": CONNECTION_ID, "user_chat_id": OWNER_ID}
        with tenant_context("acme"):
            await owner_channel.notify_missing_permission(bot, conn)
            assert db.get_owner_alert_target(
                bot.messages[0]["message_id"], owner_chat_id=str(OWNER_ID),
            ) is None

    async def test_send_failure_does_not_record(self, owner_chat):
        """אין התראה בצ'אט ⇒ אין למה להגיב ⇒ אין מיפוי."""
        bot = FakeBot(fail_owner_send=True)
        conn = {"connection_id": CONNECTION_ID, "user_chat_id": OWNER_ID}
        with tenant_context("acme"):
            sent = await owner_channel.notify_handoff(
                bot, conn, "דנה", "יש מלאי?", target=(str(CUSTOMER_ID), CUSTOMER_CHAT),
            )
        assert sent is False


class TestRetention:
    def test_purge_removes_old_mappings(self, default_tenant_db):
        db.record_owner_alert_target(1, "u1", "c1", owner_chat_id="900001")
        db.record_owner_alert_target(2, "u2", "c2", owner_chat_id="900001")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE owner_alert_targets SET created_at = datetime('now', '-40 days') "
                "WHERE owner_message_id = 1"
            )
        result = db.purge_old_data()
        assert result["owner_alert_targets"] == 1
        assert db.get_owner_alert_target(1, owner_chat_id="900001") is None
        assert db.get_owner_alert_target(2, owner_chat_id="900001") is not None

    def test_delete_user_data_removes_mappings(self, default_tenant_db):
        db.upsert_user("u1", "דנה", inbound=True)
        db.record_owner_alert_target(7, "u1", "c1", owner_chat_id="900001")
        db.delete_user_data("u1")
        assert db.get_owner_alert_target(7, owner_chat_id="900001") is None
