"""טסטים ל-control plane — כולל הסכימות החדשות של מודל B (‏PLAN §5.1)."""

import pytest

import control_plane as cp
from tenancy import TenantSuspendedError, tenant_context


class TestTenantLifecycle:
    def test_create_and_get(self, platform_db):
        cp.create_tenant("acme", "עסק לדוגמה")
        row = cp.get_tenant("acme")
        assert row["display_name"] == "עסק לדוגמה"
        assert row["status"] == "active"

    def test_create_duplicate_rejected(self, tenant):
        with pytest.raises(cp.TenantExistsError):
            cp.create_tenant("acme", "שוב")

    def test_default_slug_reserved(self, platform_db):
        from tenancy import InvalidTenantSlug

        with pytest.raises(InvalidTenantSlug):
            cp.create_tenant("default", "לא חוקי")

    def test_create_tenant_builds_data_plane(self, tenant):
        """‏create_tenant מריץ init_db על ה-DB של ה-tenant."""
        from tenancy import tenant_db_path

        assert tenant_db_path("acme").exists()
        import database as db

        with tenant_context("acme"):
            assert db.count_kb_entries(active_only=False) == 0

    def test_suspended_tenant_blocked(self, tenant):
        cp.set_tenant_status("acme", "suspended")
        from tenancy import tenant_db_path

        with pytest.raises(TenantSuspendedError):
            tenant_db_path("acme")

    def test_migrate_all_tenants_runs_on_active_only(self, tenant):
        cp.create_tenant("beta", "עסק שני")
        cp.set_tenant_status("beta", "suspended")
        result = cp.migrate_all_tenants()
        assert result == {"migrated": 1, "errors": 0}

    def test_delete_tenant_cascades(self, tenant):
        cp.set_route("telegram_webhook_key", "k1", "acme")
        cp.set_tenant_secret("acme", "telegram_bot_token", "123:abc")
        cp.register_managed_bot(555, "acme", "acme_bot", 42)
        cp.upsert_business_connection("conn-1", "acme", 42)
        summary = cp.delete_tenant("acme", backup=False)
        assert cp.get_tenant("acme") is None
        assert summary["cascade"]["routes"] == 1
        assert summary["cascade"]["secrets"] == 1
        assert summary["cascade"]["managed_bots"] == 1
        assert summary["cascade"]["business_connections"] == 1
        assert summary["files_removed"] is True


class TestRoutes:
    def test_set_and_resolve(self, tenant):
        cp.set_route("telegram_webhook_key", "abc123", "acme")
        assert cp.resolve_route("telegram_webhook_key", "abc123") == "acme"
        assert cp.get_tenant_route_key("acme", "telegram_webhook_key") == "abc123"

    def test_unknown_route_type_rejected(self, tenant):
        with pytest.raises(ValueError):
            cp.set_route("twilio_number", "+972", "acme")

    def test_resolve_unknown_returns_none(self, platform_db):
        assert cp.resolve_route("telegram_webhook_key", "nope") is None


class TestSecrets:
    def test_roundtrip_encrypted(self, tenant):
        cp.set_tenant_secret("acme", "telegram_bot_token", "123:SECRET")
        assert cp.get_tenant_secret("acme", "telegram_bot_token") == "123:SECRET"
        # הערך ב-DB מוצפן, לא בטקסט גלוי
        with cp.get_platform_connection() as conn:
            row = conn.execute(
                "SELECT value_enc FROM tenant_secrets WHERE tenant_id='acme'"
            ).fetchone()
        assert "SECRET" not in row["value_enc"]
        assert row["value_enc"].startswith("v1:")

    def test_empty_value_deletes(self, tenant):
        cp.set_tenant_secret("acme", "telegram_bot_token", "x")
        cp.set_tenant_secret("acme", "telegram_bot_token", "")
        assert cp.get_tenant_secret("acme", "telegram_bot_token") is None

    def test_fail_closed_without_key(self, tenant, monkeypatch):
        """בלי SECRETS_ENCRYPTION_KEY — כתיבת סוד נחסמת (fail-closed)."""
        from utils import crypto

        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY", raising=False)
        monkeypatch.setattr(crypto, "_fernet_cache", {})
        with pytest.raises(crypto.EncryptionConfigError):
            cp.set_tenant_secret("acme", "telegram_bot_token", "x")


class TestManagedBots:
    def test_register_and_lookup(self, tenant):
        cp.register_managed_bot(777, "acme", "acme_secretary_bot", 42)
        row = cp.get_managed_bot(777)
        assert row["tenant_id"] == "acme"
        assert row["status"] == "created"
        assert [r["bot_id"] for r in cp.get_managed_bots_by_owner(42)] == [777]
        assert cp.get_managed_bot_for_tenant("acme")["bot_id"] == 777

    def test_status_transitions(self, tenant):
        cp.register_managed_bot(777, "acme", "b", 42)
        cp.set_managed_bot_status(777, "connected")
        assert cp.get_managed_bot(777)["status"] == "connected"
        with pytest.raises(ValueError):
            cp.set_managed_bot_status(777, "nonsense")

    def test_revoked_not_returned_as_active(self, tenant):
        cp.register_managed_bot(777, "acme", "b", 42)
        cp.set_managed_bot_status(777, "revoked")
        assert cp.get_managed_bot_for_tenant("acme") is None

    def test_reregister_preserves_created_at(self, tenant):
        cp.register_managed_bot(777, "acme", "b", 42)
        created = cp.get_managed_bot(777)["created_at"]
        cp.register_managed_bot(777, "acme", "b2", 42, status="connected")
        row = cp.get_managed_bot(777)
        assert row["created_at"] == created
        assert row["bot_username"] == "b2"


class TestBusinessConnections:
    def test_upsert_and_get(self, tenant):
        cp.upsert_business_connection(
            "conn-1", "acme", owner_user_id=42, user_chat_id=99,
            can_reply=True, rights_json='{"can_reply": true}',
        )
        row = cp.get_business_connection("conn-1")
        assert row["tenant_id"] == "acme"
        assert row["can_reply"] == 1
        assert row["user_chat_id"] == 99

    def test_cache_invalidated_on_write(self, tenant):
        """ה-cache חייב להתעדכן בכתיבה — אחרת חיבור מנותק ימשיך לענות."""
        cp.upsert_business_connection("conn-1", "acme", 42, can_reply=True)
        assert cp.get_business_connection("conn-1")["can_reply"] == 1
        cp.upsert_business_connection("conn-1", "acme", 42, can_reply=False)
        assert cp.get_business_connection("conn-1")["can_reply"] == 0

    def test_disable_invalidates_cache(self, tenant):
        cp.upsert_business_connection("conn-1", "acme", 42, can_reply=True)
        cp.get_business_connection("conn-1")  # ממלא cache
        assert cp.disable_business_connection("conn-1") is True
        row = cp.get_business_connection("conn-1")
        assert row["is_enabled"] == 0
        assert row["can_reply"] == 0

    def test_unknown_connection_is_none(self, platform_db):
        assert cp.get_business_connection("nope") is None

    def test_connected_at_preserved_on_reconnect(self, tenant):
        cp.upsert_business_connection("conn-1", "acme", 42)
        first = cp.get_business_connection("conn-1")["connected_at"]
        cp.upsert_business_connection("conn-1", "acme", 42, can_reply=True)
        assert cp.get_business_connection("conn-1")["connected_at"] == first


class TestPairingCodes:
    def test_create_and_consume(self, tenant):
        code = cp.create_pairing_code("acme")
        assert cp.consume_pairing_code(code, 42) == "acme"
        row = cp.get_pairing_code(code)
        assert row["used_by_user_id"] == 42

    def test_single_use(self, tenant):
        code = cp.create_pairing_code("acme")
        assert cp.consume_pairing_code(code, 42) == "acme"
        assert cp.consume_pairing_code(code, 43) is None

    def test_expired_code_rejected(self, tenant):
        # ‏TTL שלילי מייצר קוד שכבר פג — בלי להמתין ובלי לגעת ב-DB
        # (ב-DB נשמר ה-hash, לא הקוד, אז UPDATE לפי הקוד לא היה תופס).
        code = cp.create_pairing_code("acme", ttl_minutes=-1)
        assert cp.consume_pairing_code(code, 42) is None

    def test_unknown_code_rejected(self, tenant):
        assert cp.consume_pairing_code("does-not-exist", 42) is None

    def test_purge_expired(self, tenant):
        code = cp.create_pairing_code("acme", ttl_minutes=-60)
        assert cp.purge_expired_pairing_codes() == 1
        assert cp.get_pairing_code(code) is None

    def test_code_is_stored_hashed(self, tenant):
        """הקוד הוא credential — ‏platform.db לא מחזיק אותו בטקסט גלוי."""
        code = cp.create_pairing_code("acme")
        with cp.get_platform_connection() as conn:
            stored = conn.execute("SELECT code FROM pairing_codes").fetchone()["code"]
        assert stored != code
        assert cp.get_pairing_code(code) is not None


class TestAdminUsers:
    def test_owner_requires_tenant(self, platform_db):
        with pytest.raises(cp.UnknownTenantError):
            cp.create_admin_user("a@b.com", "password1", role="owner", tenant_id="nope")

    def test_login_roundtrip(self, tenant):
        cp.create_admin_user("a@b.com", "password1", role="owner", tenant_id="acme")
        user = cp.verify_admin_login("A@B.com", "password1")
        assert user["tenant_id"] == "acme"
        # לעולם לא מחזירים hash החוצה (דפוס #6)
        assert "password_hash" not in user

    def test_wrong_password_returns_none(self, tenant):
        cp.create_admin_user("a@b.com", "password1", role="owner", tenant_id="acme")
        assert cp.verify_admin_login("a@b.com", "wrong") is None

    def test_disabled_user_cannot_login(self, tenant):
        cp.create_admin_user("a@b.com", "password1", role="owner", tenant_id="acme")
        cp.set_admin_user_status("a@b.com", "disabled")
        assert cp.verify_admin_login("a@b.com", "password1") is None
