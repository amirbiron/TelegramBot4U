"""טסטים ל-kb_service — התפר שהחליף את צינור ה-RAG."""

import control_plane as cp
import database as db
import kb_service
from tenancy import tenant_context


class TestContext:
    def test_empty_kb(self, default_tenant_db):
        ctx = kb_service.get_kb_context()
        assert ctx.is_empty is True
        assert ctx.text == ""
        assert ctx.token_estimate == 0

    def test_includes_all_active_entries(self, default_tenant_db):
        db.add_kb_entry("מחירון", "תספורות", "תספורת נשים 99")
        db.add_kb_entry("מדיניות", "ביטולים", "ביטול עד 24 שעות")
        ctx = kb_service.get_kb_context()
        assert ctx.entry_count == 2
        assert "תספורת נשים 99" in ctx.text
        assert "ביטול עד 24 שעות" in ctx.text
        # שתי הרשומות — לא top-k. זו כל הנקודה של הביטול של ה-RAG.
        assert "מחירון" in ctx.text and "מדיניות" in ctx.text

    def test_inactive_entries_excluded(self, default_tenant_db):
        entry_id = db.add_kb_entry("מחירון", "ישן", "מחיר ישן")
        with db.get_connection() as conn:
            conn.execute("UPDATE kb_entries SET is_active=0 WHERE id=?", (entry_id,))
        assert kb_service.get_kb_context().is_empty is True

    def test_entry_without_content_skipped(self, default_tenant_db):
        db.add_kb_entry("כללי", "ריק", "   ")
        db.add_kb_entry("כללי", "מלא", "תוכן")
        ctx = kb_service.get_kb_context()
        assert "מלא" in ctx.text
        assert ctx.text.count("---") == 2  # רשומה אחת בלבד (פתיחה+סגירה)


class TestCache:
    def test_edit_refreshes_cache(self, default_tenant_db):
        entry_id = db.add_kb_entry("מחירון", "תספורות", "99 ש\"ח")
        assert "99" in kb_service.get_kb_context().text
        db.update_kb_entry(entry_id, "מחירון", "תספורות", "120 ש\"ח")
        # בלי אינבלידציה ידנית — מפתח ה-cache הוא גרסת ה-KB
        assert "120" in kb_service.get_kb_context().text

    def test_delete_refreshes_cache(self, default_tenant_db):
        entry_id = db.add_kb_entry("מחירון", "תספורות", "99 ש\"ח")
        kb_service.get_kb_context()
        db.delete_kb_entry(entry_id)
        assert kb_service.get_kb_context().is_empty is True

    def test_cache_hit_avoids_requery(self, default_tenant_db, monkeypatch):
        db.add_kb_entry("מחירון", "תספורות", "99")
        kb_service.get_kb_context()

        def _boom(*args, **kwargs):
            raise AssertionError("get_all_kb_entries נקראה למרות cache תקף")

        monkeypatch.setattr(db, "get_all_kb_entries", _boom)
        assert "99" in kb_service.get_kb_context().text

    def test_tenants_do_not_share_cache(self, tenant):
        cp.create_tenant("beta", "עסק שני")
        with tenant_context("acme"):
            db.add_kb_entry("א", "רק-acme", "תוכן של acme")
            assert "acme" in kb_service.get_kb_context().text
        with tenant_context("beta"):
            ctx = kb_service.get_kb_context()
            assert ctx.is_empty is True
            assert "acme" not in ctx.text


class TestTokenEstimate:
    def test_hebrew_estimate_is_reasonable(self, default_tenant_db):
        # ~600 תווים עברית — אמור לצאת בערך 100–350 טוקנים בכל שיטת אומדן
        db.add_kb_entry("כללי", "טקסט", "שלום לכולם, זהו טקסט לבדיקה. " * 20)
        ctx = kb_service.get_kb_context()
        assert 50 < ctx.token_estimate < 900

    def test_threshold_flag(self, default_tenant_db, monkeypatch):
        import config

        monkeypatch.setattr(config, "KB_TOKEN_WARN_THRESHOLD", 10)
        db.add_kb_entry("כללי", "ארוך", "מילה " * 200)
        ctx = kb_service.get_kb_context()
        assert ctx.is_over_threshold is True

    def test_under_threshold_by_default(self, default_tenant_db):
        db.add_kb_entry("כללי", "קצר", "תוכן קצר")
        assert kb_service.get_kb_context().is_over_threshold is False


class TestContract:
    def test_top_hint_accepted_but_unused(self, default_tenant_db):
        """‏top_hint הוא חלק מהחוזה לעתיד — כרגע לא משנה את הפלט."""
        db.add_kb_entry("מחירון", "תספורות", "99")
        db.add_kb_entry("מדיניות", "ביטולים", "24 שעות")
        with_hint = kb_service.get_kb_context(top_hint="כמה עולה תספורת?")
        without = kb_service.get_kb_context()
        assert with_hint.text == without.text
