"""טסטים לבוט המנהל, לקליטת בוט-בן, ול-offboarding (שלב 2)."""

import pytest

import control_plane as cp
from bot import manager_bot
from tenancy import tenant_context


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeManagerBot:
    """‏double לבוט המנהל — מתעד קריאות API."""

    def __init__(self, token: str = "tok-123:NEW", fail_token: bool = False):
        self.sent: list[dict] = []
        self.token = token
        self.fail_token = fail_token
        self.access_settings: list[dict] = []
        self.replaced: list[int] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})

    async def get_managed_bot_token(self, user_id):
        if self.fail_token:
            raise RuntimeError("token fetch failed")
        return self.token

    async def set_managed_bot_access_settings(self, user_id, is_access_restricted,
                                              added_user_ids=None):
        self.access_settings.append({
            "user_id": user_id, "is_access_restricted": is_access_restricted,
        })
        return True

    async def replace_managed_bot_token(self, user_id):
        self.replaced.append(user_id)
        return "tok-999:REPLACED"


class FakeUpdate:
    def __init__(self, message=None, managed_bot=None, user=None):
        self.effective_message = message
        self.effective_user = user
        self.managed_bot = managed_bot


class FakeContext:
    def __init__(self, bot, args=None):
        self.bot = bot
        self.args = args or []


class FakeUser:
    def __init__(self, user_id: int, username: str = ""):
        self.id = user_id
        self.username = username


class FakeManagedBotUpdated:
    def __init__(self, creator_id: int, bot_id: int, bot_username: str):
        self.user = FakeUser(creator_id)
        self.bot = FakeUser(bot_id, bot_username)


# ─── שמות מוצעים ודיפ-לינקים ────────────────────────────────────────────


class TestUsernameSuggestion:
    def test_hyphens_become_underscores(self):
        """טלגרם לא מרשה מקפים ב-username — ה-slug של ה-tenant כן."""
        assert manager_bot.suggest_username("salon-dana") == "salon_dana_bot"

    def test_must_end_with_bot(self):
        assert manager_bot.suggest_username("acme").endswith("_bot")

    def test_starts_with_letter(self):
        assert manager_bot.suggest_username("7eleven")[0].isalpha()

    def test_collision_gets_suffix(self):
        first = manager_bot.suggest_username("acme")
        second = manager_bot.suggest_username("acme", taken={first})
        assert second != first
        assert second.endswith("_bot")

    def test_length_within_telegram_limit(self):
        long_slug = "a" * 40
        assert len(manager_bot.suggest_username(long_slug)) <= 32

    def test_matches_telegram_pattern(self):
        for slug in ("salon-dana", "acme", "7eleven", "a-b-c"):
            assert manager_bot._USERNAME_RE.match(manager_bot.suggest_username(slug)), slug


class TestDeepLinks:
    def test_creation_link_format(self):
        link = manager_bot.build_creation_deep_link("MgrBot", "acme_bot", "סלון דנה")
        assert link.startswith("https://t.me/newbot/MgrBot/acme_bot?name=")
        # שם בעברית חייב להיות מקודד
        assert "סלון" not in link

    def test_pairing_link_format(self):
        link = manager_bot.build_pairing_link("MgrBot", "abc123")
        assert link == "https://t.me/MgrBot?start=PAIR-abc123"


# ─── צימוד ───────────────────────────────────────────────────────────────


class TestPairing:
    async def test_valid_code_pairs_and_sends_link(self, tenant, monkeypatch):
        import config

        monkeypatch.setattr(config, "MANAGER_BOT_USERNAME", "MgrBot")
        code = cp.create_pairing_code("acme")
        msg = FakeMessage()
        await manager_bot.on_start(
            FakeUpdate(message=msg, user=FakeUser(42)),
            FakeContext(FakeManagerBot(), args=[f"PAIR-{code}"]),
        )
        assert cp.get_tenant_by_paired_user(42) == "acme"
        assert "t.me/newbot/MgrBot/" in msg.replies[0]

    async def test_start_without_code_is_neutral(self, tenant, monkeypatch):
        msg = FakeMessage()
        await manager_bot.on_start(
            FakeUpdate(message=msg, user=FakeUser(42)),
            FakeContext(FakeManagerBot(), args=[]),
        )
        assert "קישור הצטרפות" in msg.replies[0]
        assert cp.get_tenant_by_paired_user(42) is None

    async def test_expired_code_rejected(self, tenant, monkeypatch):
        import config

        monkeypatch.setattr(config, "MANAGER_BOT_USERNAME", "MgrBot")
        code = cp.create_pairing_code("acme")
        with cp.get_platform_connection() as conn:
            conn.execute(
                "UPDATE pairing_codes SET expires_at = datetime('now', '-1 hour')"
            )
        msg = FakeMessage()
        await manager_bot.on_start(
            FakeUpdate(message=msg, user=FakeUser(42)),
            FakeContext(FakeManagerBot(), args=[f"PAIR-{code}"]),
        )
        assert "לא בתוקף" in msg.replies[0]
        assert cp.get_tenant_by_paired_user(42) is None

    async def test_code_is_single_use(self, tenant, monkeypatch):
        import config

        monkeypatch.setattr(config, "MANAGER_BOT_USERNAME", "MgrBot")
        code = cp.create_pairing_code("acme")
        for user_id in (42, 43):
            msg = FakeMessage()
            await manager_bot.on_start(
                FakeUpdate(message=msg, user=FakeUser(user_id)),
                FakeContext(FakeManagerBot(), args=[f"PAIR-{code}"]),
            )
        assert cp.get_tenant_by_paired_user(42) == "acme"
        assert cp.get_tenant_by_paired_user(43) is None

    async def test_missing_manager_username_fails_gracefully(self, tenant, monkeypatch):
        import config

        monkeypatch.setattr(config, "MANAGER_BOT_USERNAME", "")
        code = cp.create_pairing_code("acme")
        msg = FakeMessage()
        await manager_bot.on_start(
            FakeUpdate(message=msg, user=FakeUser(42)),
            FakeContext(FakeManagerBot(), args=[f"PAIR-{code}"]),
        )
        assert "לא יכול להמשיך" in msg.replies[0]


# ─── קליטת בוט-בן ────────────────────────────────────────────────────────


@pytest.fixture
def paired_tenant(tenant):
    """‏tenant שהבעלים שלו כבר צומד אליו."""
    code = cp.create_pairing_code("acme")
    cp.consume_pairing_code(code, 42)
    return "acme"


@pytest.fixture
def no_network(monkeypatch):
    """‏setup_tenant_webhook מוחלף — אין קריאות רשת."""
    calls = []

    async def _fake_setup(tenant_id, base_url=None):
        calls.append(tenant_id)
        return "acme_bot"

    monkeypatch.setattr("bot.business_bot.setup_tenant_webhook", _fake_setup)
    return calls


class TestManagedBotIntake:
    async def test_full_intake(self, paired_tenant, no_network):
        bot = FakeManagerBot(token="555:CHILD-TOKEN")
        await manager_bot.on_managed_bot(
            FakeUpdate(managed_bot=FakeManagedBotUpdated(42, 777, "acme_bot")),
            FakeContext(bot),
        )
        # הטוקן נשמר מוצפן ורשום ל-tenant הנכון
        assert cp.get_tenant_secret("acme", "telegram_bot_token") == "555:CHILD-TOKEN"
        row = cp.get_managed_bot(777)
        assert row["tenant_id"] == "acme"
        assert row["owner_user_id"] == 42
        assert no_network == ["acme"]
        # הגישה לבוט-הבן הוגבלה לבעלים
        assert bot.access_settings[0]["is_access_restricted"] is True
        # ההוראות הידניות נשלחו (V1 — אין API להדלקת Secretary Mode)
        assert "BotFather" in bot.sent[-1]["text"]
        assert "Chatbots" in bot.sent[-1]["text"]

    async def test_unpaired_creator_creates_no_state(self, tenant, no_network):
        bot = FakeManagerBot()
        await manager_bot.on_managed_bot(
            FakeUpdate(managed_bot=FakeManagedBotUpdated(999, 777, "stranger_bot")),
            FakeContext(bot),
        )
        assert cp.get_managed_bot(777) is None
        assert cp.get_tenant_secret("acme", "telegram_bot_token") is None
        assert no_network == []
        assert "לא מזהה אותך" in bot.sent[0]["text"]

    async def test_token_fetch_failure_creates_no_state(self, paired_tenant, no_network):
        bot = FakeManagerBot(fail_token=True)
        await manager_bot.on_managed_bot(
            FakeUpdate(managed_bot=FakeManagedBotUpdated(42, 777, "acme_bot")),
            FakeContext(bot),
        )
        assert cp.get_managed_bot(777) is None
        assert no_network == []

    async def test_matching_is_by_creator_not_username(self, paired_tenant, no_network):
        """סיכון 6 ב-PLAN §8: המשתמש עשוי לשנות את ה-username המוצע."""
        bot = FakeManagerBot()
        await manager_bot.on_managed_bot(
            FakeUpdate(managed_bot=FakeManagedBotUpdated(42, 777, "totally_different_bot")),
            FakeContext(bot),
        )
        assert cp.get_managed_bot(777)["tenant_id"] == "acme"

    async def test_access_settings_failure_does_not_block(self, paired_tenant, no_network):
        """הגבלת הגישה היא best-effort — כשל בה לא עוצר את ה-onboarding."""

        class _Bot(FakeManagerBot):
            async def set_managed_bot_access_settings(self, *a, **kw):
                raise RuntimeError("nope")

        bot = _Bot()
        await manager_bot.on_managed_bot(
            FakeUpdate(managed_bot=FakeManagedBotUpdated(42, 777, "acme_bot")),
            FakeContext(bot),
        )
        assert cp.get_managed_bot(777) is not None
        assert "BotFather" in bot.sent[-1]["text"]


# ─── ניתוק ───────────────────────────────────────────────────────────────


class TestOffboarding:
    @pytest.fixture
    def connected_tenant(self, tenant, monkeypatch):
        cp.set_tenant_secret("acme", "telegram_bot_token", "555:CHILD")
        cp.set_tenant_secret("acme", "telegram_webhook_secret", "sec")
        cp.register_managed_bot(777, "acme", "acme_bot", 42, status="connected")
        cp.upsert_business_connection("conn-1", "acme", 42, user_chat_id=42,
                                      is_enabled=True, can_reply=True)

        async def _fake_remove(tenant_id):
            return None

        monkeypatch.setattr("bot.business_bot.remove_tenant_webhook", _fake_remove)
        return "acme"

    async def test_full_offboard(self, connected_tenant, monkeypatch):
        from services.offboarding import offboard_tenant

        manager = FakeManagerBot()

        class _App:
            bot = manager

        async def _ensure():
            return _App()

        monkeypatch.setattr("bot.registry.ensure_manager_application", _ensure)

        summary = await offboard_tenant("acme")

        assert summary["webhook_removed"] is True
        assert summary["token_revoked"] is True
        assert summary["secret_deleted"] is True
        assert summary["bot_marked_revoked"] is True
        assert summary["tenant_suspended"] is True
        assert summary["errors"] == []
        assert manager.replaced == [777]
        assert cp.get_tenant_secret("acme", "telegram_bot_token") is None
        assert cp.get_managed_bot(777)["status"] == "revoked"
        assert cp.get_tenant("acme")["status"] == "suspended"
        assert cp.get_business_connection("conn-1")["is_enabled"] == 0

    async def test_secret_deleted_even_if_revoke_fails(self, connected_tenant, monkeypatch):
        """הטוקן שברשותנו הוא הסיכון — מוחקים אותו גם אם הנטרול נכשל."""
        from services.offboarding import offboard_tenant

        async def _ensure():
            return None

        monkeypatch.setattr("bot.registry.ensure_manager_application", _ensure)
        summary = await offboard_tenant("acme")

        assert summary["token_revoked"] is False
        assert summary["secret_deleted"] is True
        assert cp.get_tenant_secret("acme", "telegram_bot_token") is None
        assert summary["errors"]

    async def test_is_idempotent(self, connected_tenant, monkeypatch):
        from services.offboarding import offboard_tenant

        manager = FakeManagerBot()

        class _App:
            bot = manager

        async def _ensure():
            return _App()

        monkeypatch.setattr("bot.registry.ensure_manager_application", _ensure)
        await offboard_tenant("acme")
        # הרצה שנייה על tenant מושעה — לא זורקת ומשלימה את מה שנותר
        second = await offboard_tenant("acme")
        assert second["secret_deleted"] is True

    async def test_unknown_tenant_raises(self, platform_db):
        from services.offboarding import offboard_tenant

        with pytest.raises(cp.UnknownTenantError):
            await offboard_tenant("nope")


# ─── בידוד בין לקוחות ────────────────────────────────────────────────────


class TestTenantIsolation:
    async def test_two_tenants_answer_on_their_own_connection(self, platform_db, monkeypatch):
        """‏T2.6: שני לקוחות, הודעות משולבות — כל תשובה על החיבור הנכון,
        וכל שיחה ב-DB הנכון."""
        import json
        import pathlib

        import database as db
        from bot import business_handlers as bh
        from core import message_processor as mp
        from tests.test_business_handlers import FakeBot
        from tests.test_business_handlers import FakeContext as BizContext

        monkeypatch.setattr(
            mp, "generate_answer",
            lambda **kw: {"answer": "תשובה", "kb_empty": False, "kb_tokens": 1,
                          "llm_failed": False},
        )
        monkeypatch.setattr(bh, "_schedule_summary", lambda user_id: None)

        raw = json.loads(
            (pathlib.Path(__file__).parent / "fixtures"
             / "business_message_customer.json").read_text(encoding="utf-8")
        )
        from telegram import Update

        for slug, conn_id, owner in (("alpha", "conn-alpha", 1001), ("beta", "conn-beta", 1002)):
            cp.create_tenant(slug, slug)
            cp.upsert_business_connection(conn_id, slug, owner, user_chat_id=owner,
                                          is_enabled=True, can_reply=True)

        bot = FakeBot()
        for slug, conn_id, customer in (("alpha", "conn-alpha", 111),
                                        ("beta", "conn-beta", 222),
                                        ("alpha", "conn-alpha", 333)):
            payload = json.loads(json.dumps(raw))
            payload["business_message"]["business_connection_id"] = conn_id
            payload["business_message"]["from"]["id"] = customer
            payload["business_message"]["chat"]["id"] = customer
            with tenant_context(slug):
                await bh.on_business_message(Update.de_json(payload, None), BizContext(bot))

        # כל תשובה יצאה על החיבור של ה-tenant שלה
        by_chat = {m["chat_id"]: m["business_connection_id"] for m in bot.customer_messages}
        assert by_chat == {111: "conn-alpha", 222: "conn-beta", 333: "conn-alpha"}

        # וכל שיחה נשמרה ב-DB הנכון
        with tenant_context("alpha"):
            assert {u["user_id"] for u in db.get_unique_users()} == {"111", "333"}
        with tenant_context("beta"):
            assert {u["user_id"] for u in db.get_unique_users()} == {"222"}
