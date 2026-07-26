"""טסטים לתיקוני הסקירה של PR #1.

כל מחלקה כאן מכסה ממצא אחד, ומטרתה למנוע רגרסיה — לא לתעד את הממצא.
הכותרת של כל מחלקה אומרת מה נשבר אם הטסט ייכשל.
"""

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from telegram.error import BadRequest

_IL = ZoneInfo("Asia/Jerusalem")


def _il(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 7, day, hour, 30, tzinfo=_IL)


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


# ─── crypto — מפתח הצפנה נדחה במקום להיגזר ─────────────────────────────
class TestEncryptionKeyValidation:
    """גזירת מפתח מסיסמה עם SHA-256 חשוף (בלי salt, בלי iterations) —
    סיסמה קצרה נשברת ב-brute force וכל סודות ה-tenants נפתחים איתה."""

    def _reset(self):
        import utils.crypto as crypto

        crypto._fernet_cache.clear()

    def test_valid_fernet_key_is_accepted(self, monkeypatch):
        import utils.crypto as crypto

        self._reset()
        key = crypto.generate_new_key()
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
        assert crypto.decrypt_field(crypto.encrypt_field("סוד")) == "סוד"
        self._reset()

    @pytest.mark.parametrize(
        "bad_key",
        ["my-password", "1234", "לא-אסקי", "dG9vLXNob3J0", "x" * 44],
    )
    def test_non_fernet_key_is_rejected(self, monkeypatch, bad_key):
        import utils.crypto as crypto

        self._reset()
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", bad_key)
        with pytest.raises(crypto.EncryptionConfigError):
            crypto.validate_key()
        self._reset()

    def test_startup_validation_reports_a_bad_key(self, monkeypatch):
        import config as _cfg
        import utils.crypto as crypto

        self._reset()
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "not-a-fernet-key")
        errors = _cfg.validate_config()
        assert any("Fernet" in e for e in errors)
        self._reset()

    def test_missing_key_still_reported_separately(self, monkeypatch):
        import config as _cfg

        self._reset()
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        errors = _cfg.validate_config()
        assert any("לא מוגדר" in e for e in errors)
        self._reset()


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


# ─── סבב שני של הסקירה ─────────────────────────────────────────────────
class TestPartialSendIsRecorded:
    """שליחה חלקית שנרשמת כ'לא נשלח' = ההיסטוריה טוענת שלא ענינו,
    והמודל חוזר בפנייה הבאה על תוכן שהלקוח כבר קרא."""

    async def test_delivered_chunks_are_returned(self, default_tenant_db, monkeypatch):
        import database as db
        from bot import dispatch
        from tests.doubles import FakeBot

        monkeypatch.setattr(dispatch, "TELEGRAM_MAX_MESSAGE_LENGTH", 60)

        class FailsOnThird(FakeBot):
            def __init__(self):
                super().__init__()
                self.n = 0

            async def send_message(self, chat_id, text, business_connection_id=None, **kw):
                self.n += 1
                if self.n == 3:
                    raise BadRequest("Bad Request: BUSINESS_PEER_INVALID")
                return await super().send_message(
                    chat_id, text, business_connection_id, **kw
                )

        db.upsert_user("1", "דנה", inbound=True)
        text = ("משפט ארוך לצורך הבדיקה. " * 20).strip()
        bot = FailsOnThird()
        sent = await dispatch.send_to_customer(bot, 10, "conn-1", text, "1", "דנה")

        assert sent, "מה שנמסר בפועל לא אמור לחזור ריק"
        assert sent != text, "השליחה הייתה חלקית — לא אמור לחזור הטקסט המלא"
        assert len(bot.customer_messages) == 2
        for m in bot.customer_messages:
            assert m["text"] in sent

    async def test_full_send_returns_the_whole_text(self, default_tenant_db):
        import database as db
        from bot import dispatch
        from tests.doubles import FakeBot

        db.upsert_user("1", "דנה", inbound=True)
        sent = await dispatch.send_to_customer(
            FakeBot(), 10, "conn-1", "שלום", "1", "דנה",
        )
        assert sent == "שלום"


class TestWebhookSecretsAreSeparate:
    """סוד משותף לשני ה-routes = דליפה באחד פותחת גם את השני."""

    def test_default_tenant_does_not_use_the_manager_secret(self, monkeypatch):
        import config as _cfg
        from bot.registry import resolve_webhook_secret
        from tenancy import DEFAULT_TENANT

        monkeypatch.setattr(_cfg, "MANAGER_WEBHOOK_SECRET", "manager-secret", raising=False)
        monkeypatch.setattr(_cfg, "TELEGRAM_WEBHOOK_SECRET", "bot-secret", raising=False)
        assert resolve_webhook_secret(DEFAULT_TENANT) == "bot-secret"

    def test_missing_secret_is_empty_not_the_manager_one(self, monkeypatch):
        import config as _cfg
        from bot.registry import resolve_webhook_secret
        from tenancy import DEFAULT_TENANT

        monkeypatch.setattr(_cfg, "MANAGER_WEBHOOK_SECRET", "manager-secret", raising=False)
        monkeypatch.setattr(_cfg, "TELEGRAM_WEBHOOK_SECRET", "", raising=False)
        # ריק ⇒ ‏_verify_secret דוחה (fail closed), ולא נופל לסוד המנהל
        assert resolve_webhook_secret(DEFAULT_TENANT) == ""


class TestOwnerNotificationDedup:
    """‏kind ריק עוקף את הדה-דופ — כשל שליחה חוזר היה מציף את הבעלים."""

    async def test_repeated_send_failure_is_suppressed(self, default_tenant_db):
        from services import owner_channel
        from tests.doubles import FakeBot

        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}
        assert await owner_channel.notify_send_failed(bot, conn, "דנה", "other") is True
        assert await owner_channel.notify_send_failed(bot, conn, "דנה", "other") is False
        assert len(bot.messages) == 1

    async def test_different_customer_still_notifies(self, default_tenant_db):
        from services import owner_channel
        from tests.doubles import FakeBot

        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}
        await owner_channel.notify_send_failed(bot, conn, "דנה", "other")
        assert await owner_channel.notify_send_failed(bot, conn, "יוסי", "other") is True
        assert len(bot.messages) == 2


class TestLlmModelIsHonored:
    """‏LLM_MODEL שנקרא ואז נזרק = המשתמש בטוח שהחליף מודל ולא החליף."""

    def test_openai_branch_uses_llm_model(self, monkeypatch):
        import llm_client

        captured = {}

        class _FakeCompletions:
            def create(self, **kw):
                captured.update(kw)
                raise RuntimeError("עוצרים אחרי לכידת הפרמטרים")

        fake_client = MagicMock(chat=MagicMock(completions=_FakeCompletions()))
        monkeypatch.setattr(llm_client, "get_openai_client", lambda: fake_client)
        monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini-custom")
        monkeypatch.delenv("LLM_PROVIDER", raising=False)

        with pytest.raises(RuntimeError):
            llm_client.chat_complete([], temperature=0.5, max_tokens=100)
        assert captured["model"] == "gpt-4o-mini-custom"

    def test_falls_back_to_openai_model(self, monkeypatch):
        import config as _cfg
        import llm_client

        captured = {}

        class _FakeCompletions:
            def create(self, **kw):
                captured.update(kw)
                raise RuntimeError("stop")

        fake_client = MagicMock(chat=MagicMock(completions=_FakeCompletions()))
        monkeypatch.setattr(llm_client, "get_openai_client", lambda: fake_client)
        monkeypatch.setattr(_cfg, "OPENAI_MODEL", "gpt-4.1-mini", raising=False)
        monkeypatch.setenv("LLM_MODEL", "")

        with pytest.raises(RuntimeError):
            llm_client.chat_complete([], temperature=0.5, max_tokens=100)
        assert captured["model"] == "gpt-4.1-mini"


class TestSuggestedUsernameIsValid:
    """הצעה שלא עומדת בכללי טלגרם נדחית במסך היצירה, בלי שנדע למה."""

    @pytest.mark.parametrize(
        "slug", ["ac", "a", "acme", "acme-cafe", "123shop", "x" * 40, "tov"],
    )
    def test_suggestion_matches_telegram_rules(self, slug):
        from bot.manager_bot import _USERNAME_RE, suggest_username

        name = suggest_username(slug)
        assert _USERNAME_RE.match(name), f"username לא תקין: {name}"
        assert 5 <= len(name) <= 32

    def test_taken_names_are_skipped(self):
        from bot.manager_bot import suggest_username

        assert suggest_username("acme", {"acme_bot"}) != "acme_bot"


class TestTenantHasPaired:
    """הפאנל שואל את ה-control plane ולא את ה-SQL של platform.db."""

    def test_false_before_pairing(self, tenant, platform_db):
        platform_db.create_pairing_code(tenant)
        assert platform_db.tenant_has_paired(tenant) is False

    def test_true_after_pairing(self, tenant, platform_db):
        code = platform_db.create_pairing_code(tenant)
        platform_db.consume_pairing_code(code, 4242)
        assert platform_db.tenant_has_paired(tenant) is True


# ─── סבב שלישי — ממצאים על שלב 3 ───────────────────────────────────────
class TestNonTelegramSendFailure:
    """כשל שאינו של טלגרם נבלע בשקט: הלקוח לא קיבל תשובה, ה-DB לא סומן,
    והבעלים לא ידע."""

    async def test_generic_exception_marks_and_notifies(self, default_tenant_db):
        import database as db
        from bot import dispatch
        from services import owner_channel
        from tests.doubles import FakeBot

        owner_channel.reset_dedup()
        # ‏TimeoutError אינו TelegramError — קודם הוא נפל לענף שרק לוגג
        bot = FakeBot(fail_send=TimeoutError("החיבור נתקע"))
        db.upsert_user("1", "דנה", inbound=True)
        conn = {"connection_id": "conn-1", "user_chat_id": 999}

        sent = await dispatch.send_to_customer(
            bot, 10, "conn-1", "שלום", "1", "דנה", conn,
        )

        assert sent == ""
        assert db.get_user("1")["send_failure_reason"] == dispatch.FAILURE_OTHER
        assert bot.customer_messages == []
        assert len(bot.owner_messages) == 1

    async def test_without_conn_it_still_marks_the_db(self, default_tenant_db):
        import database as db
        from bot import dispatch
        from tests.doubles import FakeBot

        bot = FakeBot(fail_send=OSError("שקע נסגר"))
        db.upsert_user("1", "דנה", inbound=True)
        await dispatch.send_to_customer(bot, 10, "conn-1", "שלום", "1", "דנה")
        assert db.get_user("1")["send_failure_reason"] == dispatch.FAILURE_OTHER


class TestDedupKeyIsTheUserId:
    """שם תצוגה אינו ייחודי — שתי 'דנה' היו חולקות מפתח דה-דופ,
    וההתראה על השנייה הייתה נבלעת."""

    async def test_same_name_different_customers_both_notify(self, default_tenant_db):
        from services import owner_channel
        from tests.doubles import FakeBot

        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}

        first = await owner_channel.notify_send_failed(
            bot, conn, "דנה", "other", user_id="111",
        )
        second = await owner_channel.notify_send_failed(
            bot, conn, "דנה", "other", user_id="222",
        )
        assert first is True
        assert second is True, "לקוח שני עם אותו שם — ההתראה עליו נבלעה"

    async def test_same_customer_is_still_deduped(self, default_tenant_db):
        from services import owner_channel
        from tests.doubles import FakeBot

        owner_channel.reset_dedup()
        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 999}

        await owner_channel.notify_send_failed(bot, conn, "דנה", "other", user_id="111")
        again = await owner_channel.notify_send_failed(
            bot, conn, "דנה", "other", user_id="111",
        )
        assert again is False

    def test_subject_falls_back_to_the_name(self):
        """בלי user_id עדיף דה-דופ גס על היעדר דה-דופ."""
        from services.owner_channel import _subject

        assert _subject("", "דנה") == "דנה"
        assert _subject("  ", "דנה") == "דנה"
        assert _subject("111", "דנה") == "111"

    @pytest.mark.parametrize(
        "fn_name", ["notify_rate_limited", "notify_window_closed",
                    "notify_send_failed", "notify_media"],
    )
    def test_every_per_customer_notification_takes_a_user_id(self, fn_name):
        """התראה חדשה פר-לקוח שתשכח את הפרמטר תיפול כאן."""
        import inspect

        from services import owner_channel

        params = inspect.signature(getattr(owner_channel, fn_name)).parameters
        assert "user_id" in params


class TestAlertTargetIsChatScoped:
    """‏message_id של טלגרם ייחודי פר-צ'אט. מפתח בלעדיו = `/pause`
    בתגובה להתראה אחת משתיק את הלקוח של התראה אחרת."""

    def test_same_message_id_in_two_chats_stays_separate(self, default_tenant_db):
        import database as db

        db.record_owner_alert_target(500, "user-a", "chat-a", owner_chat_id="owner-1")
        db.record_owner_alert_target(500, "user-b", "chat-b", owner_chat_id="owner-2")

        first = db.get_owner_alert_target(500, owner_chat_id="owner-1")
        second = db.get_owner_alert_target(500, owner_chat_id="owner-2")
        assert first["user_id"] == "user-a"
        assert second["user_id"] == "user-b"

    def test_wrong_chat_returns_nothing(self, default_tenant_db):
        import database as db

        db.record_owner_alert_target(500, "user-a", "chat-a", owner_chat_id="owner-1")
        assert db.get_owner_alert_target(500, owner_chat_id="owner-9") is None

    async def test_send_records_the_chat_it_actually_sent_to(self, default_tenant_db):
        import database as db
        from services import owner_channel
        from tests.doubles import FakeBot

        bot = FakeBot()
        conn = {"connection_id": "conn-1", "user_chat_id": 777}
        await owner_channel.notify_handoff(
            bot, conn, "דנה", "שאלה", target=("u1", "c1"),
        )
        message_id = bot.messages[0]["message_id"]
        assert db.get_owner_alert_target(message_id, owner_chat_id="777") is not None


class TestOwnerRepliesHaveNoMarkdown:
    """אין `parse_mode` בריפו — סימני Markdown היו מוצגים ככוכביות."""

    def _texts(self) -> list[str]:
        """המחרוזות שנשלחות בפועל לבעלים — בלי docstrings.

        ה-docstrings הם תיעוד למפתח ולא הודעה, ולכן הדגשות בהם מותרות
        (וגם רצויות). בלי ההבחנה הזו הטסט היה נכשל על תיעוד תקין.
        """
        import ast
        import pathlib

        source = pathlib.Path("bot/owner_commands.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        out = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name.startswith("_cmd_")):
                continue
            docstrings = {
                ast.get_docstring(fn, clean=False)
                for fn in ast.walk(node)
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    if sub.value not in docstrings:
                        out.append(sub.value)
        return out

    def test_no_bold_markers_in_replies(self):
        offenders = [t for t in self._texts() if "**" in t]
        assert not offenders, f"סימני Markdown בהודעה לבעלים: {offenders}"

    @pytest.mark.parametrize("marker", ["__", "`"])
    def test_no_other_markdown_markers(self, marker):
        offenders = [t for t in self._texts() if marker in t]
        assert not offenders, f"סימני Markdown ({marker}) בהודעה לבעלים: {offenders}"


# ─── סבב רביעי — ממצאים על שלב 4 ───────────────────────────────────────
class TestDailyJobStorm:
    """‏`is_due` נשען על ה-DB בלבד, ו-`mark_ran` רק לגג בכשל. התוצאה:
    ‏platform.db נעול לרגע ⇒ גיבוי מלא של כל ה-tenants **כל דקה**."""

    def test_failed_persist_still_stops_the_rerun(self, platform_db, monkeypatch):
        import control_plane as cp
        from services import daily_job

        daily_job.reset_process_marks()
        monkeypatch.setattr(
            cp, "set_platform_meta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB נעול")),
        )
        now = _il(5)
        assert daily_job.is_due("job-x", 3, now) is True
        daily_job.mark_ran("job-x", now)
        # ה-DB לא נכתב — ובלי הסימון בזיכרון זה היה חוזר בכל tick
        assert daily_job.is_due("job-x", 3, now) is False

    def test_the_in_process_mark_is_scoped_to_the_day(self, platform_db, monkeypatch):
        import control_plane as cp
        from services import daily_job

        daily_job.reset_process_marks()
        monkeypatch.setattr(
            cp, "set_platform_meta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB נעול")),
        )
        daily_job.mark_ran("job-x", _il(5, day=15))
        assert daily_job.is_due("job-x", 3, _il(5, day=16)) is True

    def test_a_restart_falls_back_to_the_db(self, platform_db, monkeypatch):
        """הסימון בזיכרון הוא תוספת ולא תחליף — restart מריץ שוב."""
        import control_plane as cp
        from services import daily_job

        daily_job.reset_process_marks()
        monkeypatch.setattr(
            cp, "set_platform_meta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB נעול")),
        )
        daily_job.mark_ran("job-x", _il(5))
        daily_job.reset_process_marks()          # מדמה עליית תהליך
        assert daily_job.is_due("job-x", 3, _il(5)) is True

    def test_every_daily_job_shares_the_guard(self, platform_db, monkeypatch):
        """שלושת ה-jobs היו שלושה עותקים של אותה לוגיקה."""
        import control_plane as cp
        from services import backup_job, daily_job, digest_service, retention_service

        daily_job.reset_process_marks()
        monkeypatch.setattr(
            cp, "set_platform_meta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB נעול")),
        )
        for mark, due, hour in (
            (backup_job.mark_backup_ran, backup_job.is_backup_due, 3),
            (retention_service.mark_retention_ran,
             retention_service.is_retention_due, 4),
            (digest_service.mark_digest_ran, digest_service.is_digest_due, 20),
        ):
            now = _il(hour + 1)
            mark(now)
            assert due(now) is False, f"{due.__name__} חוזר על עצמו אחרי כשל כתיבה"

    def test_read_failure_is_fail_closed(self, platform_db, monkeypatch):
        import control_plane as cp
        from services import daily_job

        daily_job.reset_process_marks()
        monkeypatch.setattr(
            cp, "get_platform_meta",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB נעול")),
        )
        assert daily_job.is_due("job-x", 3, _il(5)) is False


class TestOwnerAlertTargetsMigration:
    """‏`CREATE TABLE IF NOT EXISTS` לא משנה טבלה קיימת — ‏DB שנוצר עם
    המפתח היחיד היה נשאר בלי `owner_chat_id` וכל שאילתה חדשה נכשלת."""

    def _legacy_table(self, conn):
        conn.execute("DROP TABLE IF EXISTS owner_alert_targets")
        conn.executescript("""
            CREATE TABLE owner_alert_targets (
                owner_message_id INTEGER PRIMARY KEY,
                user_id          TEXT NOT NULL,
                chat_id          TEXT NOT NULL,
                created_at       TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO owner_alert_targets (owner_message_id, user_id, chat_id)
            VALUES (4242, 'u1', 'c1');
        """)

    def test_upgrade_adds_the_composite_key(self, default_tenant_db):
        import database as db
        from migrations import run_migrations

        with db.get_connection() as conn:
            self._legacy_table(conn)
            run_migrations(conn)
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(owner_alert_targets)")
            }
            pk = [
                r["name"] for r in conn.execute("PRAGMA table_info(owner_alert_targets)")
                if r["pk"]
            ]
        assert "owner_chat_id" in cols
        assert set(pk) == {"owner_chat_id", "owner_message_id"}

    def test_existing_rows_survive(self, default_tenant_db):
        import database as db
        from migrations import run_migrations

        with db.get_connection() as conn:
            self._legacy_table(conn)
            run_migrations(conn)
            row = conn.execute(
                "SELECT user_id, chat_id, owner_chat_id FROM owner_alert_targets"
            ).fetchone()
        assert row["user_id"] == "u1"
        assert row["chat_id"] == "c1"
        assert row["owner_chat_id"] == ""

    def test_new_writes_work_after_the_upgrade(self, default_tenant_db):
        import database as db
        from migrations import run_migrations

        with db.get_connection() as conn:
            self._legacy_table(conn)
            run_migrations(conn)
        db.record_owner_alert_target(500, "u2", "c2", owner_chat_id="owner-1")
        assert db.get_owner_alert_target(500, owner_chat_id="owner-1") is not None

    def test_migration_is_idempotent(self, default_tenant_db):
        import database as db
        from migrations import run_migrations

        with db.get_connection() as conn:
            self._legacy_table(conn)
            run_migrations(conn)
            run_migrations(conn)
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM owner_alert_targets"
            ).fetchone()["c"]
        assert count == 1

    def test_a_failure_mid_rebuild_leaves_the_table_intact(self, default_tenant_db):
        """‏rebuild שנכשל באמצע חייב להשאיר את הטבלה הישנה.

        ‏`executescript` מבצע COMMIT משתמע לכל טרנזקציה פתוחה לפני
        שהוא מתחיל, ולכן טרנזקציה חיצונית לא הגנה כאן על כלום: כשל
        בין ה-DROP ל-RENAME היה משאיר את ה-tenant **בלי הטבלה בכלל**,
        וכל התראה לבעלים מפסיקה להיות ניתנת לשיוך.
        """
        import database as db
        import migrations

        with db.get_connection() as conn:
            self._legacy_table(conn)

            # ‏sqlite3.Connection הוא C extension ולא ניתן ל-patch
            # ישירות; ‏proxy דק שמעביר הכול חוץ מ-executescript.
            class _FailingConn:
                def __init__(self, inner):
                    self._inner = inner

                def __getattr__(self, name):
                    return getattr(self._inner, name)

                def executescript(self, script):
                    # מריצים עד ה-DROP (כולל) ואז מפילים — בדיוק החלון
                    # שבו הטבלה המקורית כבר לא קיימת.
                    self._inner.executescript(script.split("ALTER TABLE")[0])
                    raise sqlite3.OperationalError("disk I/O error")

            with pytest.raises(sqlite3.OperationalError):
                migrations._rebuild_owner_alert_targets(_FailingConn(conn))

            rows = conn.execute(
                "SELECT user_id FROM owner_alert_targets"
            ).fetchall()

        # הטבלה קיימת, עם השורה שהייתה בה
        assert [r["user_id"] for r in rows] == ["u1"]


class TestFatalConfigStopsTheBoot:
    """שירות שעולה עם מפתח הצפנה שגוי עובר health check ונראה תקין,
    בעוד כל כתיבת סוד בו נכשלת בשקט."""

    @pytest.fixture(autouse=True)
    def _clean_fernet_cache(self):
        """ה-cache מוצפן לפי מפתח, וטסט שמחליף מפתח חייב לרוקן אותו.

        ב-fixture ולא בגוף הטסט: ניקוי בסוף הפונקציה **אינו** רץ
        כשהטענה נכשלת, ואז מפתח שבור דולף לטסטים הבאים והכשל השני
        מסתיר את הראשון.
        """
        import utils.crypto as crypto

        crypto._fernet_cache.clear()
        try:
            yield
        finally:
            crypto._fernet_cache.clear()

    def test_broken_key_is_fatal(self, monkeypatch):
        import config as _cfg

        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "not-a-fernet-key")
        assert _cfg.fatal_config_errors()

    def test_missing_key_is_not_fatal(self, monkeypatch):
        """חסר הוא לגיטימי — פיתוח מקומי, `--seed`, ‏tenant יחיד."""
        import config as _cfg

        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        assert _cfg.fatal_config_errors() == []

    def test_valid_key_is_not_fatal(self, monkeypatch):
        import config as _cfg
        import utils.crypto as crypto

        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", crypto.generate_new_key())
        assert _cfg.fatal_config_errors() == []

    def test_missing_webhook_url_is_only_a_warning(self, monkeypatch):
        """‏`--admin` בלי WEBHOOK_BASE_URL הוא מצב עבודה תקין."""
        import config as _cfg
        import utils.crypto as crypto

        monkeypatch.setattr(_cfg, "WEBHOOK_BASE_URL", "", raising=False)
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", crypto.generate_new_key())
        assert any("WEBHOOK_BASE_URL" in e for e in _cfg.validate_config(require_bot=True))
        assert _cfg.fatal_config_errors() == []

    def test_boot_raises_on_fatal_config(self, monkeypatch):
        import main

        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "not-a-fernet-key")
        with pytest.raises(RuntimeError, match="תצורה פגומה"):
            main.create_wsgi_app(with_bots=False)
