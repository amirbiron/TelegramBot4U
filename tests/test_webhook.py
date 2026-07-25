"""טסטים ל-route של ה-webhook — ראוטינג, אימות, ו-allowed_updates."""

import asyncio
import json
import pathlib
import threading

import pytest

import control_plane as cp

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def webhook_app(default_tenant_db, tenant):
    """אפליקציית Flask עם ה-route של ה-webhook ולולאת בוטים אמיתית."""
    from admin.app import create_admin_app
    from bot.webhook import register_webhook_routes

    cp.set_route("telegram_webhook_key", "route-key-abc", "acme")
    cp.set_tenant_secret("acme", "telegram_webhook_secret", "s3cr3t")

    app = create_admin_app()
    app.config["TESTING"] = True

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    app.config["_bot_loop"] = loop

    register_webhook_routes(app)
    yield app
    # ניקוז לפני עצירה: ה-route מוסר את העיבוד ללולאה ומחזיר 200 מיד,
    # כך שבסוף הטסט יש קורוטינה שטרם רצה. עצירה בלעדיו משאירה אותה
    # לאיסוף זבל ומייצרת "coroutine was never awaited" בטסט אקראי.
    try:
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=5)
    except Exception:
        pass  # noqa: S110 — ניקוז best-effort בסוף טסט, לא נתיב מוצר
    # עצירה, המתנה לסיום ה-thread, ורק אז סגירה. בלי ה-join הלולאה
    # עלולה עוד לרוץ כשהטסט הבא נפתח, ו-`close()` על לולאה שרצה זורק —
    # מה שהופך threads דולפים לכשל בטסט אקראי אחר.
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()


@pytest.fixture
def client(webhook_app):
    return webhook_app.test_client()


def _payload(name: str = "business_message_customer.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestRouting:
    def test_unknown_route_key_is_404(self, client):
        resp = client.post("/telegram/webhook/t/nope", json=_payload())
        assert resp.status_code == 404

    def test_missing_secret_rejected(self, client):
        resp = client.post("/telegram/webhook/t/route-key-abc", json=_payload())
        assert resp.status_code == 403

    def test_wrong_secret_rejected(self, client):
        resp = client.post(
            "/telegram/webhook/t/route-key-abc", json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )
        assert resp.status_code == 403

    def test_correct_secret_accepted(self, client):
        resp = client.post(
            "/telegram/webhook/t/route-key-abc", json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
        )
        # 200 מיידי — בלי להמתין לעיבוד (אחרת טלגרם תשלח שוב)
        assert resp.status_code == 200

    def test_invalid_body_rejected(self, client):
        resp = client.post(
            "/telegram/webhook/t/route-key-abc", data="not json",
            content_type="application/json",
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
        )
        assert resp.status_code == 400

    def test_missing_loop_returns_503(self, webhook_app):
        webhook_app.config["_bot_loop"] = None
        resp = webhook_app.test_client().post(
            "/telegram/webhook/t/route-key-abc", json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
        )
        assert resp.status_code == 503

    def test_route_is_csrf_exempt(self, client):
        """ה-route הוא שרת-לשרת — אימות דרך הסוד, לא דרך CSRF."""
        client.application.config["WTF_CSRF_ENABLED"] = True
        resp = client.post(
            "/telegram/webhook/t/route-key-abc", json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": "s3cr3t"},
        )
        assert resp.status_code == 200

    def test_tenant_without_secret_is_rejected(self, client):
        """‏fail closed: סוד ריק אינו "בלי אימות" אלא תצורה חסרה."""
        cp.set_tenant_secret("acme", "telegram_webhook_secret", "")
        resp = client.post(
            "/telegram/webhook/t/route-key-abc", json=_payload(),
            headers={"X-Telegram-Bot-Api-Secret-Token": ""},
        )
        assert resp.status_code == 403


class TestAllowedUpdates:
    def test_all_business_update_types_included(self):
        """שכחת סוג עדכון = דממה שקטה בלי שגיאה."""
        import config

        assert set(config.BUSINESS_ALLOWED_UPDATES) >= {
            "business_connection",
            "business_message",
            "edited_business_message",
            "deleted_business_messages",
            "message",
        }

    def test_manager_listens_to_managed_bot(self):
        import config

        assert "managed_bot" in config.MANAGER_ALLOWED_UPDATES

    async def test_setup_registers_webhook_with_allowed_updates(self, tenant, monkeypatch):
        """‏set_webhook נקרא עם allowed_updates המלא ועם סוד."""
        import config
        from bot import business_bot

        captured = {}

        class FakeBot:
            def __init__(self, token=None):
                captured["token"] = token

            async def initialize(self):
                return None

            async def shutdown(self):
                return None

            async def set_webhook(self, url, secret_token=None, allowed_updates=None,
                                  drop_pending_updates=None):
                captured.update({
                    "url": url, "secret": secret_token,
                    "allowed_updates": allowed_updates,
                })
                return True

            async def get_me(self):
                class _Me:
                    username = "acme_secretary_bot"

                return _Me()

        monkeypatch.setattr(config, "WEBHOOK_BASE_URL", "https://example.test")
        monkeypatch.setattr("telegram.Bot", FakeBot)
        cp.set_tenant_secret("acme", "telegram_bot_token", "123:ABC")

        username = await business_bot.setup_tenant_webhook("acme")

        assert username == "acme_secretary_bot"
        assert captured["url"].startswith("https://example.test/telegram/webhook/t/")
        assert set(captured["allowed_updates"]) == set(config.BUSINESS_ALLOWED_UPDATES)
        assert captured["secret"]
        # הסוד נשמר לפני הרישום (fail closed)
        assert cp.get_tenant_secret("acme", "telegram_webhook_secret") == captured["secret"]
        assert cp.get_tenant_secret("acme", "telegram_bot_username") == "acme_secretary_bot"

    async def test_setup_without_token_fails_loudly(self, tenant, monkeypatch):
        import config
        from bot import business_bot

        monkeypatch.setattr(config, "WEBHOOK_BASE_URL", "https://example.test")
        with pytest.raises(RuntimeError, match="טוקן"):
            await business_bot.setup_tenant_webhook("acme")

    async def test_setup_without_base_url_fails_loudly(self, tenant, monkeypatch):
        import config
        from bot import business_bot

        monkeypatch.setattr(config, "WEBHOOK_BASE_URL", "")
        with pytest.raises(RuntimeError, match="WEBHOOK_BASE_URL"):
            await business_bot.setup_tenant_webhook("acme")


class TestRegistry:
    def test_never_falls_back_to_another_tenant_token(self, tenant, monkeypatch):
        """‏tenant בלי טוקן מחזיר ריק — לא את הטוקן של ה-env."""
        import config
        from bot import registry

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "env-token")
        assert registry.resolve_telegram_token("acme") == ""
        assert registry.resolve_telegram_token("default") == "env-token"

    def test_tenant_token_is_used(self, tenant):
        from bot import registry

        cp.set_tenant_secret("acme", "telegram_bot_token", "123:ACME")
        assert registry.resolve_telegram_token("acme") == "123:ACME"

    async def test_dispatch_without_token_drops_update(self, tenant):
        from bot import registry

        registry.reset_registry()
        await registry.dispatch_update("acme", _payload())
        assert registry._apps == {}

    def test_handlers_registered_on_application(self):
        from telegram.ext import (
            BusinessConnectionHandler,
            BusinessMessagesDeletedHandler,
            MessageHandler,
        )

        from bot.business_bot import create_business_application

        app = create_business_application("123:FAKE")
        handlers = app.handlers[0]
        types = [type(h) for h in handlers]
        assert BusinessConnectionHandler in types
        assert BusinessMessagesDeletedHandler in types
        assert types.count(MessageHandler) == 2

    async def test_invalid_token_is_dropped_not_raised(self, tenant, monkeypatch):
        """טוקן שנשלל = תצורה שגויה: שורת לוג ברורה, בלי traceback בכל הודעה."""
        from telegram.error import InvalidToken

        from bot import registry

        registry.reset_registry()
        cp.set_tenant_secret("acme", "telegram_bot_token", "123:REVOKED")

        class _App:
            async def initialize(self):
                raise InvalidToken("rejected")

        monkeypatch.setattr(
            "bot.business_bot.create_business_application", lambda token: _App(),
        )
        assert await registry.ensure_application("acme") is None
        assert registry._apps == {}
