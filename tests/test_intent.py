"""טסטים לסיווג כוונות (‏regex — ה-LLM כבוי כברירת מחדל בערוץ הזה)."""

import pytest

from intent import Intent, _detect_intent_regex_full, detect_intent, detect_intent_with_llm


class TestFastPath:
    @pytest.mark.parametrize("text", ["שלום", "היי!", "בוקר טוב", "hi", "Hello"])
    def test_greetings(self, text):
        assert detect_intent(text) == Intent.GREETING

    @pytest.mark.parametrize("text", ["תודה", "ביי", "להתראות", "thanks"])
    def test_farewells(self, text):
        assert detect_intent(text) == Intent.FAREWELL

    def test_greeting_word_inside_sentence_is_not_greeting(self):
        """ה-regex מעוגן — "שלום, מה המחיר?" אינה ברכה בלבד."""
        assert detect_intent("שלום, כמה עולה תספורת?") == Intent.GENERAL

    def test_empty_is_general(self):
        assert detect_intent("") == Intent.GENERAL


class TestFullRegex:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("מה שעות הפתיחה?", Intent.BUSINESS_HOURS),
            ("אתם פתוחים היום?", Intent.BUSINESS_HOURS),
            ("כמה עולה תספורת?", Intent.PRICING),
            ("מה המחיר של צבע?", Intent.PRICING),
            ("איפה אתם נמצאים?", Intent.LOCATION),
            ("מה הכתובת?", Intent.LOCATION),
            ("אפשר לדבר עם בעל העסק?", Intent.HUMAN_AGENT),
            ("אני רוצה לדבר עם נציג", Intent.HUMAN_AGENT),
            ("אני לא מרוצה מהשירות", Intent.COMPLAINT),
            ("רוצה להתלונן", Intent.COMPLAINT),
            ("יש לכם חניה?", Intent.GENERAL),
        ],
    )
    def test_classification(self, text, expected):
        assert _detect_intent_regex_full(text) == expected


class TestNoBookingIntents:
    def test_booking_intents_removed(self):
        """אין תורים בערוץ הזה — הכוונות האלה לא קיימות יותר."""
        values = {i.value for i in Intent}
        assert "appointment_booking" not in values
        assert "appointment_cancel" not in values
        assert "appointment_reschedule" not in values

    def test_booking_request_falls_through_to_general(self):
        """בקשת תור אינה כוונה נפרדת — ה-LLM יטפל בה (ויעשה handoff)."""
        assert _detect_intent_regex_full("אני רוצה לקבוע תור") == Intent.GENERAL


class TestHybrid:
    def test_llm_disabled_by_default_uses_regex(self, monkeypatch):
        """כברירת מחדל אין קריאת LLM נוספת — תיוג בלבד לא מצדיק אותה."""
        import intent as intent_mod

        def _boom(_):
            raise AssertionError("נקרא ה-LLM למרות ש-LLM_INTENT_ENABLED כבוי")

        monkeypatch.setattr(intent_mod, "_detect_intent_llm", _boom)
        assert detect_intent_with_llm("כמה עולה?") == Intent.PRICING

    def test_llm_used_when_enabled(self, monkeypatch):
        import config
        import intent as intent_mod

        monkeypatch.setattr(config, "LLM_INTENT_ENABLED", True)
        monkeypatch.setattr(intent_mod, "_detect_intent_llm", lambda _: Intent.COMPLAINT)
        assert detect_intent_with_llm("משהו לא ברור") == Intent.COMPLAINT

    def test_greeting_short_circuits_before_llm(self, monkeypatch):
        import config
        import intent as intent_mod

        monkeypatch.setattr(config, "LLM_INTENT_ENABLED", True)
        monkeypatch.setattr(
            intent_mod, "_detect_intent_llm",
            lambda _: pytest.fail("ברכה לא אמורה להגיע ל-LLM"),
        )
        assert detect_intent_with_llm("שלום") == Intent.GREETING
