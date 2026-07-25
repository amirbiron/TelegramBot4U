"""
טסטים לשכבת ה-tenancy (tenancy.py).

הועתקו מ-`ai-business-bot/tests/test_tenancy.py` עם נרמול imports
(‏`ai_chatbot.config` ⇒ ‏`config`) והסרת הטסט של `tenant_faiss_dir`
(אין RAG בפרויקט הזה).

מכסים: fallback לברירת מחדל, ‏context manager, ולידציית slug, מצב STRICT,
resolve של נתיבים, בידוד אמיתי של get_connection בין שני tenants,
וסמנטיקת ההפצה של contextvars (‏asyncio כן, thread חדש לא).
"""

import asyncio
import threading
from unittest.mock import patch

import pytest

import tenancy
from tenancy import (
    DEFAULT_TENANT,
    InvalidTenantSlug,
    MissingTenantContext,
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
    tenant_context,
    tenant_db_path,
)


class TestContext:
    def test_default_fallback_when_unset(self):
        assert get_current_tenant() == DEFAULT_TENANT

    def test_context_manager_sets_and_restores(self):
        assert get_current_tenant() == DEFAULT_TENANT
        with tenant_context("salon-a"):
            assert get_current_tenant() == "salon-a"
        assert get_current_tenant() == DEFAULT_TENANT

    def test_nested_contexts(self):
        with tenant_context("outer"):
            with tenant_context("inner"):
                assert get_current_tenant() == "inner"
            assert get_current_tenant() == "outer"

    def test_context_restored_on_exception(self):
        with pytest.raises(ValueError):
            with tenant_context("salon-a"):
                raise ValueError("boom")
        assert get_current_tenant() == DEFAULT_TENANT

    def test_set_reset_token(self):
        token = set_current_tenant("salon-a")
        assert get_current_tenant() == "salon-a"
        reset_current_tenant(token)
        assert get_current_tenant() == DEFAULT_TENANT


class TestSlugValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "UPPER",
            "עברית",
            "a b",
            "a/b",
            "../etc",
            "a" * 33,
            "-startdash",
            "dot.dot",
            None,
            123,
        ],
    )
    def test_invalid_slugs_rejected(self, bad):
        with pytest.raises(InvalidTenantSlug):
            tenancy.validate_tenant_id(bad)

    @pytest.mark.parametrize("good", ["default", "salon-a", "a", "x1-y2", "0abc"])
    def test_valid_slugs_accepted(self, good):
        assert tenancy.validate_tenant_id(good) == good

    def test_set_current_tenant_validates(self):
        with pytest.raises(InvalidTenantSlug):
            set_current_tenant("../escape")


class TestStrictMode:
    def test_strict_raises_without_context(self, monkeypatch):
        monkeypatch.setenv("TENANCY_STRICT", "true")
        with pytest.raises(MissingTenantContext):
            get_current_tenant()

    def test_strict_ok_with_context(self, monkeypatch):
        monkeypatch.setenv("TENANCY_STRICT", "true")
        with tenant_context("salon-a"):
            assert get_current_tenant() == "salon-a"


class TestPathResolution:
    def test_default_tenant_maps_to_legacy_db_path(self, tmp_path):
        legacy = tmp_path / "legacy.db"
        with patch("config.DB_PATH", legacy):
            assert tenant_db_path() == legacy
            assert tenant_db_path(DEFAULT_TENANT) == legacy

    def test_other_tenant_maps_to_tenants_dir(self, tmp_path):
        with patch("config.DATA_DIR", tmp_path):
            expected = (tmp_path / "tenants" / "salon-a" / "chatbot.db").resolve()
            assert tenant_db_path("salon-a") == expected
            with tenant_context("salon-a"):
                assert tenant_db_path() == expected

    def test_db_path_rejects_invalid_tenant(self):
        with pytest.raises(InvalidTenantSlug):
            tenant_db_path("../../escape")

    def test_tenant_data_dir_rejects_default(self):
        with pytest.raises(InvalidTenantSlug):
            tenancy.tenant_data_dir(DEFAULT_TENANT)


class TestConnectionIsolation:
    """הטסט המרכזי: get_connection חייב לכבד את ה-tenant context."""

    def test_two_tenants_get_separate_databases(self, tmp_path):
        from database import get_connection, init_db

        with patch("config.DATA_DIR", tmp_path), \
             patch("config.DB_PATH", tmp_path / "default.db"):
            for slug in ("salon-a", "salon-b"):
                (tmp_path / "tenants" / slug).mkdir(parents=True)
                with tenant_context(slug):
                    init_db()

            with tenant_context("salon-a"):
                with get_connection() as conn:
                    conn.execute(
                        "INSERT INTO kb_entries (category, title, content) "
                        "VALUES ('FAQ', 'only-in-a', 'x')"
                    )

            with tenant_context("salon-a"):
                with get_connection() as conn:
                    rows = conn.execute(
                        "SELECT COUNT(*) AS c FROM kb_entries WHERE title='only-in-a'"
                    ).fetchone()
                    assert rows["c"] == 1

            with tenant_context("salon-b"):
                with get_connection() as conn:
                    rows = conn.execute(
                        "SELECT COUNT(*) AS c FROM kb_entries WHERE title='only-in-a'"
                    ).fetchone()
                    assert rows["c"] == 0

            # קבצים פיזיים נפרדים נוצרו בפועל
            assert (tmp_path / "tenants" / "salon-a" / "chatbot.db").exists()
            assert (tmp_path / "tenants" / "salon-b" / "chatbot.db").exists()

    def test_default_context_uses_legacy_path(self, default_tenant_db):
        """בלי context מפורש — get_connection עובד מול ה-DB של ברירת המחדל."""
        from database import get_connection

        with get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM kb_entries").fetchone()
            assert row["c"] >= 0  # הסכימה קיימת ונגישה


class TestPropagationSemantics:
    """מתעד את סמנטיקת ההפצה — עליה נשען הכלל 'להעביר tenant בין threads ידנית'."""

    def test_asyncio_task_inherits_tenant(self):
        async def child():
            return get_current_tenant()

        async def main():
            with tenant_context("salon-a"):
                return await asyncio.create_task(child())

        assert asyncio.run(main()) == "salon-a"

    def test_new_thread_does_not_inherit(self):
        seen = {}

        def worker():
            seen["tenant"] = get_current_tenant()

        with tenant_context("salon-a"):
            t = threading.Thread(target=worker)
            t.start()
            t.join()

        assert seen["tenant"] == DEFAULT_TENANT
