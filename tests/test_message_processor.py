"""טסטים לליבת העיבוד — handoff, פערי ידע ואיסור הסגרת אוטומציה."""

import pytest

import config
import database as db
from core import message_processor as mp
from intent import Intent


@pytest.fixture
def fake_llm(monkeypatch):
    """מחליף את generate_answer בתשובה קבועה. אין קריאות רשת בטסטים."""
    state = {"answer": "תספורת עולה 99 ש\"ח", "llm_failed": False}

    def _fake(**kwargs):
        state["last_call"] = kwargs
        return {
            "answer": state["answer"],
            "kb_empty": False,
            "kb_tokens": 10,
            "llm_failed": state["llm_failed"],
        }

    monkeypatch.setattr(mp, "generate_answer", _fake)
    return state


class TestHandoffDetection:
    def test_marker_at_start_detected(self):
        assert mp.should_handoff_to_human(f"{config.HANDOFF_MARKER}\n\nבודק ואחזור") is True

    def test_marker_midtext_not_detected(self):
        """‏startswith בלבד — בלי fuzzy matching."""
        assert mp.should_handoff_to_human("התשובה היא [HANDOFF] במקום כלשהו") is False

    def test_innocent_phrasing_not_detected(self):
        """הביטויים שהריפו הישן זיהה ב-fuzzy — כאן לא מפעילים handoff."""
        assert mp.should_handoff_to_human("אעביר את הפנייה לדנה בהמשך") is False

    def test_exact_fallback_detected(self):
        assert mp.should_handoff_to_human(config.FALLBACK_RESPONSE) is True

    def test_marker_stripped(self):
        text = f"{config.HANDOFF_MARKER}\n\nבודק ואחזור אליך"
        assert mp.strip_handoff_marker(text) == "בודק ואחזור אליך"

    def test_strip_is_idempotent_without_marker(self):
        assert mp.strip_handoff_marker("סתם תשובה") == "סתם תשובה"


class TestOutgoingSanitization:
    def test_marker_never_leaks(self):
        out = mp.sanitize_outgoing(f"{config.HANDOFF_MARKER}\n\n<b>בודק</b>\nמקור: מחירון")
        assert config.HANDOFF_MARKER not in out
        assert "<b>" not in out
        assert "מקור:" not in out


class TestProcessing:
    def test_plain_answer(self, default_tenant_db, fake_llm):
        result = mp.process_incoming_message(
            "1", "כמה עולה תספורת?", {"display_name": "דנה"},
            rate_limit_already_checked=True,
        )
        assert result.action == "reply"
        assert result.text == "תספורת עולה 99 ש\"ח"
        assert result.consecutive_fallbacks == 0
        assert result.needs_summarization is True

    def test_handoff_records_knowledge_gap(self, default_tenant_db, fake_llm):
        """הטריגר לפער ידע עבר מ-chunks_used==0 לזיהוי HANDOFF."""
        fake_llm["answer"] = f"{config.HANDOFF_MARKER}\n\nבודק ואחזור אליך"
        result = mp.process_incoming_message(
            "1", "יש מכשיר X במלאי?", {"display_name": "דנה"},
            rate_limit_already_checked=True,
        )
        assert result.action == "handoff"
        assert result.handoff_reason == "יש מכשיר X במלאי?"
        gaps = db.get_unanswered_questions()
        assert len(gaps) == 1
        assert gaps[0]["question"] == "יש מכשיר X במלאי?"

    def test_handoff_text_has_no_marker(self, default_tenant_db, fake_llm):
        fake_llm["answer"] = f"{config.HANDOFF_MARKER}\n\nבודק ואחזור אליך"
        result = mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"}, rate_limit_already_checked=True,
        )
        assert config.HANDOFF_MARKER not in result.text

    def test_explicit_human_request_is_handoff(self, default_tenant_db, fake_llm):
        """בקשה מפורשת לדבר עם אדם — handoff גם אם המודל לא סימן."""
        fake_llm["answer"] = "בשמחה, במה אפשר לעזור?"
        result = mp.process_incoming_message(
            "1", "אפשר לדבר עם בעל העסק?", {"display_name": "דנה"},
            rate_limit_already_checked=True,
        )
        assert result.intent == Intent.HUMAN_AGENT
        assert result.action == "handoff"

    def test_escalation_after_three_handoffs(self, default_tenant_db, fake_llm):
        fake_llm["answer"] = f"{config.HANDOFF_MARKER}\n\nבודק"
        first = mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"},
            consecutive_fallbacks=0, rate_limit_already_checked=True,
        )
        assert first.escalate_takeover is False
        third = mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"},
            consecutive_fallbacks=2, rate_limit_already_checked=True,
        )
        assert third.consecutive_fallbacks == mp.ESCALATION_THRESHOLD
        assert third.escalate_takeover is True

    def test_successful_answer_resets_counter(self, default_tenant_db, fake_llm):
        result = mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"},
            consecutive_fallbacks=2, rate_limit_already_checked=True,
        )
        assert result.consecutive_fallbacks == 0

    def test_empty_answer_falls_back_to_bridge(self, default_tenant_db, fake_llm):
        fake_llm["answer"] = "   "
        db.update_bot_settings(handoff_bridge_message="אחזור אליך תכף")
        result = mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"}, rate_limit_already_checked=True,
        )
        assert result.text == "אחזור אליך תכף"

    def test_history_passed_through(self, default_tenant_db, fake_llm):
        history = [{"role": "user", "message": "קודם"}]
        mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"},
            rate_limit_already_checked=True, conversation_history=history,
        )
        assert fake_llm["last_call"]["conversation_history"] == history


class TestRateLimit:
    def test_rate_limited_is_silent(self, default_tenant_db, fake_llm, monkeypatch):
        """חריגה = שתיקה מוחלטת ללקוח, בלי הודעת מערכת."""
        monkeypatch.setattr(mp, "check_rate_limit", lambda _: "minute")
        result = mp.process_incoming_message(
            "1", "שאלה", {"display_name": "דנה"},
        )
        assert result.action == "rate_limited"
        assert result.text == ""
        assert result.rate_limit_window == "minute"


class TestNoBotTells:
    """אין הודעות שמסגירות אוטומציה בשום נתיב של הצינור."""

    FORBIDDEN = ("כפתור", "לחצו", "תפריט", "מספר פנייה", "אנא המתן", "המערכת")

    def test_module_texts_are_clean(self):
        import inspect

        source = inspect.getsource(mp)
        # מחפשים רק מחרוזות שנשלחות ללקוח — ה-fallback ומשפט הגישור
        for forbidden in self.FORBIDDEN:
            assert forbidden not in config.FALLBACK_RESPONSE
        assert "reply_markup" not in source

    def test_bridge_default_is_human_sounding(self, default_tenant_db):
        assert mp._bridge_message() == "בודק ואחזור אליך בהקדם"
