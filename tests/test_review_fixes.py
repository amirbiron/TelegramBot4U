"""טסטים לתיקוני הסקירה של PR #1.

כל מחלקה כאן מכסה ממצא אחד, ומטרתה למנוע רגרסיה — לא לתעד את הממצא.
הכותרת של כל מחלקה אומרת מה נשבר אם הטסט ייכשל.
"""

from unittest.mock import MagicMock, patch

import pytest


# ─── PII sanitizer — קידומות טלפון ישראליות ────────────────────────────
class TestPhonePatterns:
    """מספר טלפון של לקוח שמגיע למפתח בלי redaction = דליפת PII."""

    @pytest.mark.parametrize(
        "phone",
        [
            # סלולר
            "0501234567", "050-123-4567", "052 123 4567",
            # VoIP / וירטואלי — קידומת בת שלוש ספרות
            "0731234567", "073-1234567", "077 123 4567", "072-000-1111",
            "0791234567", "074-1234567", "076-1234567",
            # קווי — קידומת בת שתי ספרות
            "03-1234567", "021234567", "04 111 2222", "08-1234567",
            "09 8887777",
            # בינלאומי
            "+972501234567", "+972-50-123-4567", "00972501234567",
        ],
    )
    def test_phone_is_redacted(self, phone):
        from utils.pii_sanitizer import PHONE_REDACTION, sanitize_pii

        result = sanitize_pii(f"תתקשר {phone} בבקשה")
        assert result.phones_redacted >= 1, f"לא זוהה כטלפון: {phone}"
        assert PHONE_REDACTION in result.text
        assert phone not in result.text

    @pytest.mark.parametrize(
        "text",
        ["השנה 2024 הייתה טובה", "מחיר 1234 שקל", "יש 15 פריטים במלאי"],
    )
    def test_non_phone_is_kept(self, text):
        from utils.pii_sanitizer import sanitize_pii

        result = sanitize_pii(text)
        assert result.phones_redacted == 0
        assert result.text == text

    def test_has_pii_indicators_covers_voip(self):
        from utils.pii_sanitizer import has_pii_indicators

        assert has_pii_indicators("החוג הוא 073-1234567")
        assert not has_pii_indicators("סתם טקסט בלי מספרים")


# ─── intent — סדר הדפוסים ──────────────────────────────────────────────
class TestIntentPriority:
    """בקשה לאדם היא היחידה שמייצרת פעולה; דפוס תיוג שגונב אותה =
    לקוח שביקש בן אדם ומקבל תשובה על מחירון."""

    def test_human_agent_precedes_labeling_intents(self):
        import intent as intent_mod

        order = [i.name for i, _ in intent_mod._FALLBACK_PATTERNS]
        assert order.index("HUMAN_AGENT") < order.index("BUSINESS_HOURS")
        assert order.index("HUMAN_AGENT") < order.index("PRICING")

    @pytest.mark.parametrize(
        "text",
        [
            "כמה יעלה לדבר עם בעל העסק?",
            "מה שעות הפעילות? אני רוצה לדבר עם נציג",
            "מחיר לשירות, ואפשר לדבר עם מישהו?",
        ],
    )
    def test_mixed_message_resolves_to_human_agent(self, text, monkeypatch):
        import config as _cfg
        from intent import Intent, detect_intent_with_llm

        # ה-regex המלא הוא המסלול בפרודקשן (LLM_INTENT_ENABLED כבוי)
        monkeypatch.setattr(_cfg, "LLM_INTENT_ENABLED", False, raising=False)
        assert detect_intent_with_llm(text) == Intent.HUMAN_AGENT


# ─── llm_client — מפתח Anthropic פר-tenant ─────────────────────────────
class TestPerTenantAnthropicKey:
    """‏tenant עם מפתח משלו חויב על חשבון מפתח הפלטפורמה, או נכשל
    לגמרי כשלפלטפורמה אין מפתח בכלל."""

    def test_tenant_secret_selects_claude_with_its_own_key(self, tenant, monkeypatch):
        import control_plane as cp
        import llm_client
        from tenancy import tenant_context

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cp.set_tenant_secret(tenant, "anthropic_api_key", "sk-ant-tenant")

        with tenant_context(tenant):
            provider, _model, api_key = llm_client.get_llm_provider_config()

        assert provider == "claude"
        assert api_key == "sk-ant-tenant"

    def test_no_tenant_secret_falls_back_to_platform_env(self, tenant, monkeypatch):
        import llm_client
        from tenancy import tenant_context

        monkeypatch.setenv("LLM_PROVIDER", "")
        with tenant_context(tenant):
            provider, _model, api_key = llm_client.get_llm_provider_config()

        assert provider == ""
        assert api_key == ""

    def test_client_cache_is_keyed_by_key_not_singleton(self, monkeypatch):
        import llm_client

        llm_client.reset_clients()
        created: list[str] = []

        class _FakeAnthropic:
            def __init__(self, api_key, timeout=None):
                created.append(api_key)

        monkeypatch.setattr(
            llm_client, "anthropic", MagicMock(Anthropic=_FakeAnthropic)
        )
        llm_client.get_anthropic_client("key-a")
        llm_client.get_anthropic_client("key-b")
        llm_client.get_anthropic_client("key-a")  # cache hit

        assert created == ["key-a", "key-b"]
        llm_client.reset_clients()

    def test_missing_key_raises_clear_error(self, monkeypatch):
        import llm_client

        llm_client.reset_clients()
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(llm_client, "anthropic", MagicMock())
        with pytest.raises(RuntimeError, match="Anthropic"):
            llm_client.get_anthropic_client("")

    def test_timeouts_are_configured(self):
        import llm_client

        assert llm_client.LLM_TIMEOUT_SECONDS > 0
        assert llm_client.INTENT_TIMEOUT_SECONDS > 0
        # סיווג כוונה לא אמור להחזיק את הלקוח כמו תשובה מלאה
        assert llm_client.INTENT_TIMEOUT_SECONDS < llm_client.LLM_TIMEOUT_SECONDS


# ─── השוואת credentials על bytes ───────────────────────────────────────
class TestNonAsciiCredentials:
    """‏hmac.compare_digest על `str` מרים TypeError על תו לא-ASCII —
    כלומר 500 שאפשר להפעיל מבחוץ, במקום דחייה."""

    def test_hebrew_username_is_rejected_not_crashing(self, monkeypatch):
        import admin.app as admin_app
        import config as _cfg

        monkeypatch.setattr(_cfg, "ADMIN_USERNAME", "admin", raising=False)
        monkeypatch.setattr(_cfg, "ADMIN_PASSWORD", "test-password", raising=False)
        monkeypatch.setattr(_cfg, "ADMIN_PASSWORD_HASH", "", raising=False)

        assert admin_app._verify_admin_credentials("מנהל", "סיסמה") is False
        assert admin_app._verify_admin_credentials("admin", "סיסמה") is False
        assert admin_app._verify_admin_credentials("admin", "test-password") is True

    def test_webhook_secret_comparison_accepts_non_ascii(self, monkeypatch):
        import bot.webhook as webhook_mod

        headers = {"X-Telegram-Bot-Api-Secret-Token": "סוד-בעברית"}
        fake_request = MagicMock(headers=headers)
        monkeypatch.setattr(webhook_mod, "request", fake_request)
        with patch("bot.registry.resolve_webhook_secret", return_value="סוד-בעברית"):
            assert webhook_mod._verify_secret("acme") is True
        with patch("bot.registry.resolve_webhook_secret", return_value="אחר"):
            assert webhook_mod._verify_secret("acme") is False

    def test_missing_secret_fails_closed(self, monkeypatch):
        import bot.webhook as webhook_mod

        fake_request = MagicMock(headers={})
        monkeypatch.setattr(webhook_mod, "request", fake_request)
        with patch("bot.registry.resolve_webhook_secret", return_value=""):
            assert webhook_mod._verify_secret("acme") is False


# ─── control plane — קודי צימוד מגובבים ────────────────────────────────
class TestPairingCodeHashing:
    """קוד צימוד בטקסט גלוי ב-platform.db = מי שקורא את הקובץ יכול
    לצמוד את עצמו לכל לקוח עם קוד שטרם נוצל."""

    def test_plaintext_code_is_not_stored(self, tenant, platform_db):
        code = platform_db.create_pairing_code(tenant)
        with platform_db.get_platform_connection() as conn:
            stored = conn.execute("SELECT code FROM pairing_codes").fetchone()["code"]
        assert stored != code
        assert len(stored) == 64  # SHA-256 hex

    def test_lookup_and_consume_still_work(self, tenant, platform_db):
        code = platform_db.create_pairing_code(tenant)
        assert platform_db.get_pairing_code(code) is not None
        assert platform_db.consume_pairing_code(code, 12345) == tenant
        # חד-פעמי
        assert platform_db.consume_pairing_code(code, 12345) is None

    def test_unknown_code_returns_none(self, tenant, platform_db):
        platform_db.create_pairing_code(tenant)
        assert platform_db.get_pairing_code("לא-קיים") is None
        assert platform_db.consume_pairing_code("לא-קיים", 1) is None


# ─── control plane — rollback ביצירת tenant ────────────────────────────
class TestCreateTenantRollback:
    """רישום tenant בלי data plane = tenant 'פעיל' שכל גישה לנתוניו
    נכשלת, ו-create_tenant חוזר נופל על TenantExistsError."""

    def test_data_plane_failure_removes_the_registration(self, platform_db):
        with patch("database.init_db", side_effect=OSError("דיסק מלא")):
            with pytest.raises(OSError):
                platform_db.create_tenant("broken", "עסק שנכשל")

        assert platform_db.get_tenant("broken") is None
        # ניסיון חוזר אינו נחסם
        platform_db.create_tenant("broken", "עסק שנכשל")
        assert platform_db.get_tenant("broken") is not None


# ─── retention — ניקוז תור ה-ledger ────────────────────────────────────
class TestLedgerRetryDrain:
    """בלי קורא, `ledger_write_retry` מצטבר לנצח — עם user_id גלוי
    בתוך ה-payload, בניגוד למטריצת הפרטיות."""

    def test_purge_drains_the_retry_queue(self, default_tenant_db):
        db = default_tenant_db
        with patch(
            "utils.consent_ledger.process_ledger_retry_queue",
            return_value={"succeeded": 2, "failed": 0, "exhausted": 1,
                          "total_processed": 3},
        ):
            result = db.purge_old_data()

        assert result["ledger_retry_succeeded"] == 2
        assert result["ledger_retry_exhausted"] == 1
        assert result["ledger_retry_total_processed"] == 3

    def test_drain_failure_does_not_break_purge(self, default_tenant_db):
        db = default_tenant_db
        with patch(
            "utils.consent_ledger.process_ledger_retry_queue",
            side_effect=RuntimeError("DB נעול"),
        ):
            result = db.purge_old_data()

        assert result["ledger_retry_failed"] == -1
        assert result["conversations"] >= 0  # שאר המחיקות רצו


# ─── llm — גדר ה-KB גם בכשל טעינה ──────────────────────────────────────
class TestKnowledgeBaseFence:
    """בלי הגדר, המודל מקבל פרסונה בלבד ועונה מהידע הכללי שלו על
    מחירים ושעות — כלומר ממציא."""

    def _kb_section(self, messages):
        from llm import ANCHOR_KB

        return next(
            (m["content"] for m in messages if ANCHOR_KB in m.get("content", "")), "",
        )

    def test_empty_kb_injects_the_fence(self, default_tenant_db):
        import llm

        messages = llm._build_messages("מה המחיר?", [], None)
        assert "אין לך כרגע שום מידע עסקי" in self._kb_section(messages)

    def test_kb_load_failure_injects_the_same_fence(self, default_tenant_db):
        import llm

        with patch("llm.get_kb_context", side_effect=RuntimeError("DB נעול")):
            messages = llm._build_messages("מה המחיר?", [], None)
        assert "אין לך כרגע שום מידע עסקי" in self._kb_section(messages)

    def test_generate_answer_survives_kb_meta_failure(self, default_tenant_db):
        import llm

        fake = MagicMock(
            text="שלום", finish_reason="stop", model="test",
            prompt_tokens=1, completion_tokens=1,
        )
        with patch("llm.get_kb_context", side_effect=RuntimeError("DB נעול")), \
                patch("llm.chat_complete", return_value=fake):
            result = llm.generate_answer("מה המחיר?", [])

        assert result["llm_failed"] is False
        assert result["kb_empty"] is True
        assert result["kb_tokens"] == 0
