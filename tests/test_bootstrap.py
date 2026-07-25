"""טסטים לעליית התהליך, ל-seed ול-CLI של הפלטפורמה."""

import control_plane as cp
import database as db
import main
import platform_cli
from tenancy import DEFAULT_TENANT, tenant_context


class TestBootstrap:
    def test_creates_schemas(self, tmp_path):
        main.bootstrap()
        assert cp.platform_db_path().exists()
        with tenant_context(DEFAULT_TENANT):
            assert db.count_kb_entries(active_only=False) == 0

    def test_is_idempotent(self):
        main.bootstrap()
        main.bootstrap()

    def test_migrates_existing_tenants(self, tenant):
        """כל tenant פעיל עובר init_db בעליית התהליך."""
        with tenant_context("acme"):
            db.add_kb_entry("א", "t", "c")
        main.bootstrap()
        with tenant_context("acme"):
            assert db.count_kb_entries() == 1

    def test_purges_expired_pairing_codes(self, tenant):
        code = cp.create_pairing_code("acme")
        with cp.get_platform_connection() as conn:
            conn.execute(
                "UPDATE pairing_codes SET expires_at = datetime('now', '-1 hour') "
                "WHERE code = ?",
                (code,),
            )
        main.bootstrap()
        assert cp.get_pairing_code(code) is None


class TestSeed:
    def test_seed_creates_demo_tenant_with_kb(self):
        import seed_data

        main.bootstrap()
        tenant_id = seed_data.seed_demo_tenant()
        assert cp.get_tenant(tenant_id)["display_name"] == seed_data.DEMO_TENANT_NAME
        with tenant_context(tenant_id):
            assert db.count_kb_entries() == len(seed_data.DEMO_KB_ENTRIES)
            assert db.get_bot_settings()["handoff_bridge_message"]

    def test_seed_is_idempotent(self):
        import seed_data

        main.bootstrap()
        seed_data.seed_demo_tenant()
        seed_data.seed_demo_tenant()
        with tenant_context(seed_data.DEMO_TENANT_ID):
            assert db.count_kb_entries() == len(seed_data.DEMO_KB_ENTRIES)

    def test_seeded_kb_reaches_the_prompt(self):
        """בדיקת קצה-לקצה קטנה: מה שנזרע מגיע לבסיס הידע שנשלח ל-LLM."""
        import kb_service
        import seed_data

        main.bootstrap()
        seed_data.seed_demo_tenant()
        with tenant_context(seed_data.DEMO_TENANT_ID):
            ctx = kb_service.get_kb_context()
        assert "ביטול עד 24 שעות" in ctx.text
        assert ctx.entry_count == len(seed_data.DEMO_KB_ENTRIES)


class TestPlatformCli:
    def test_create_tenant_registers_route(self, capsys):
        assert platform_cli.main(["create-tenant", "--id", "salon", "--name", "סלון"]) == 0
        assert cp.get_tenant("salon") is not None
        route_key = cp.get_tenant_route_key("salon", "telegram_webhook_key")
        assert route_key
        assert cp.resolve_route("telegram_webhook_key", route_key) == "salon"
        assert route_key in capsys.readouterr().out

    def test_duplicate_tenant_returns_error_code(self):
        platform_cli.main(["create-tenant", "--id", "salon", "--name", "סלון"])
        assert platform_cli.main(["create-tenant", "--id", "salon", "--name", "שוב"]) == 1

    def test_set_secret_does_not_print_value(self, capsys):
        platform_cli.main(["create-tenant", "--id", "salon", "--name", "סלון"])
        capsys.readouterr()
        platform_cli.main([
            "set-secret", "--tenant", "salon",
            "--name", "telegram_bot_token", "--value", "123:SUPERSECRET",
        ])
        assert "SUPERSECRET" not in capsys.readouterr().out
        assert cp.get_tenant_secret("salon", "telegram_bot_token") == "123:SUPERSECRET"

    def test_list_secrets_shows_names_only(self, capsys):
        platform_cli.main(["create-tenant", "--id", "salon", "--name", "סלון"])
        platform_cli.main([
            "set-secret", "--tenant", "salon",
            "--name", "telegram_bot_token", "--value", "123:SUPERSECRET",
        ])
        capsys.readouterr()
        platform_cli.main(["list-secrets", "--tenant", "salon"])
        out = capsys.readouterr().out
        assert "telegram_bot_token" in out
        assert "SUPERSECRET" not in out

    def test_set_status(self):
        platform_cli.main(["create-tenant", "--id", "salon", "--name", "סלון"])
        assert platform_cli.main(
            ["set-status", "--tenant", "salon", "--status", "suspended"]
        ) == 0
        assert cp.get_tenant("salon")["status"] == "suspended"

    def test_gen_key_outputs_valid_fernet(self, capsys):
        platform_cli.main(["gen-key"])
        key = capsys.readouterr().out.strip()
        from cryptography.fernet import Fernet

        Fernet(key.encode())  # לא זורק ⇒ מפתח תקין
