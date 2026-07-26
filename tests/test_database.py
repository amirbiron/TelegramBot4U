"""טסטים לשכבת ה-DB פר-tenant."""

import pytest

import database as db
from tenancy import tenant_context


class TestSchema:
    def test_no_kb_chunks_table(self, default_tenant_db):
        """אין RAG — ולכן אין kb_chunks. הכלל הזה נאכף בטסט כדי שלא
        יוחזר בטעות בהעתקה עתידית מהריפו המקור."""
        with db.get_connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        names = {r["name"] for r in rows}
        assert "kb_chunks" not in names
        assert {"kb_entries", "conversations", "users", "live_chats"} <= names

    def test_init_db_is_idempotent(self, default_tenant_db):
        db.init_db()
        db.init_db()
        assert db.count_kb_entries(active_only=False) == 0


class TestKnowledgeBase:
    def test_crud(self, default_tenant_db):
        entry_id = db.add_kb_entry("מחירון", "תספורות", "תספורת נשים 99 ש\"ח")
        assert db.get_kb_entry(entry_id)["title"] == "תספורות"
        db.update_kb_entry(entry_id, "מחירון", "תספורות", "תספורת נשים 120 ש\"ח")
        assert "120" in db.get_kb_entry(entry_id)["content"]
        assert db.get_kb_categories() == ["מחירון"]
        db.delete_kb_entry(entry_id)
        assert db.get_kb_entry(entry_id) is None

    def test_count_and_get_share_filter(self, default_tenant_db):
        db.add_kb_entry("א", "t1", "c1")
        db.add_kb_entry("ב", "t2", "c2")
        assert db.count_kb_entries(category="א") == len(db.get_all_kb_entries(category="א"))

    def test_search_escapes_like_wildcards(self, default_tenant_db):
        """דפוס קריטי #8 — `%` מהמשתמש לא יכול לסרוק את כל הטבלה."""
        db.add_kb_entry("כללי", "מדיניות", "ביטול עד 24 שעות")
        db.add_kb_entry("כללי", "חניה", "יש חניה בחינם")
        assert len(db.search_kb_entries("%")) == 0
        assert len(db.search_kb_entries("_")) == 0
        assert len(db.search_kb_entries("חניה")) == 1

    def test_search_finds_in_content(self, default_tenant_db):
        db.add_kb_entry("כללי", "מדיניות", "ביטול עד 24 שעות")
        assert len(db.search_kb_entries("ביטול")) == 1

    def test_kb_version_changes_on_every_write(self, default_tenant_db):
        """כל כתיבה מזיזה את הגרסה — **גם באותה שנייה**.

        רגרסיה: כשהחתימה הייתה MAX(updated_at) ברזולוציית שנייה, יצירה
        ועריכה באותה שנייה נראו זהות וה-cache של kb_service הגיש תוכן ישן.
        """
        v0 = db.get_kb_version()
        entry_id = db.add_kb_entry("א", "t", "c")
        v1 = db.get_kb_version()
        assert v1 != v0
        db.update_kb_entry(entry_id, "א", "t", "c2")
        v2 = db.get_kb_version()
        assert v2 != v1
        db.delete_kb_entry(entry_id)
        assert db.get_kb_version() != v2

    def test_kb_version_tracks_direct_sql_writes(self, default_tenant_db):
        """גם כתיבה ישירה (בלי לעבור בפונקציות) מזיזה את הגרסה — triggers."""
        entry_id = db.add_kb_entry("א", "t", "c")
        before = db.get_kb_version()
        with db.get_connection() as conn:
            conn.execute("UPDATE kb_entries SET is_active=0 WHERE id=?", (entry_id,))
        assert db.get_kb_version() != before


class TestConversations:
    def test_save_and_history_order(self, default_tenant_db):
        db.save_message("1", "דנה", "user", "שאלה", authored_by="customer")
        db.save_message("1", "דנה", "assistant", "תשובה")
        history = db.get_conversation_history("1")
        assert [m["role"] for m in history] == ["user", "assistant"]

    def test_edit_by_tg_id(self, default_tenant_db):
        db.save_message("1", "דנה", "user", "לפני", tg_chat_id=10, tg_message_id=55)
        assert db.update_message_by_tg_id(10, 55, "אחרי") == 1
        assert db.get_conversation_history("1")[0]["message"] == "אחרי"

    def test_delete_by_tg_ids(self, default_tenant_db):
        db.save_message("1", "דנה", "user", "א", tg_chat_id=10, tg_message_id=1)
        db.save_message("1", "דנה", "user", "ב", tg_chat_id=10, tg_message_id=2)
        db.save_message("1", "דנה", "user", "ג", tg_chat_id=10, tg_message_id=3)
        assert db.delete_messages_by_tg_ids(10, [1, 3])["conversations"] == 2
        assert [m["message"] for m in db.get_conversation_history("1")] == ["ב"]

    def test_delete_by_tg_ids_empty_list(self, default_tenant_db):
        assert db.delete_messages_by_tg_ids(10, [])["conversations"] == 0

    def test_authored_by_recorded(self, default_tenant_db):
        db.save_message("1", "דנה", "assistant", "עניתי בעצמי", authored_by="owner")
        assert db.get_conversation_history("1")[0]["authored_by"] == "owner"


class TestUsers:
    def test_upsert_inbound_updates_window(self, default_tenant_db):
        db.upsert_user("1", "דנה", chat_id="10", inbound=True)
        row = db.get_user("1")
        assert row["last_inbound_at"] is not None
        assert db.is_within_reply_window("1") is True

    def test_outbound_upsert_does_not_open_window(self, default_tenant_db):
        db.upsert_user("1", "דנה", chat_id="10", inbound=False)
        assert db.get_user("1")["last_inbound_at"] is None
        assert db.is_within_reply_window("1") is False

    def test_window_expires(self, default_tenant_db):
        db.upsert_user("1", "דנה", inbound=True)
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE users SET last_inbound_at = datetime('now', '-25 hours')"
            )
        assert db.is_within_reply_window("1") is False

    def test_inbound_clears_send_failure(self, default_tenant_db):
        db.upsert_user("1", "דנה", inbound=True)
        db.mark_send_failure("1", "window_closed")
        assert db.get_user("1")["send_failure_reason"] == "window_closed"
        db.upsert_user("1", "דנה", inbound=True)
        assert db.get_user("1")["send_failure_reason"] == ""

    def test_message_count_increments(self, default_tenant_db):
        db.upsert_user("1", "דנה", inbound=True)
        db.upsert_user("1", "דנה", inbound=True)
        assert db.get_user("1")["message_count"] == 2

    def test_fallback_counter(self, default_tenant_db):
        db.upsert_user("1", "דנה")
        db.set_consecutive_fallbacks("1", 2)
        assert db.get_consecutive_fallbacks("1") == 2
        assert db.get_consecutive_fallbacks("missing") == 0


class TestTakeover:
    def test_start_is_idempotent(self, default_tenant_db):
        first = db.start_live_chat("10", "1", "דנה")
        second = db.start_live_chat("10", "1", "דנה")
        assert first == second
        assert db.count_active_live_chats() == 1

    def test_end_and_restart(self, default_tenant_db):
        db.start_live_chat("10")
        db.end_live_chat("10")
        assert db.is_live_chat_active("10") is False
        db.start_live_chat("10")
        assert db.is_live_chat_active("10") is True

    def test_expire_by_timeout(self, default_tenant_db):
        db.start_live_chat("10")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE live_chats SET updated_at = datetime('now', '-121 minutes')"
            )
        assert db.end_expired_live_chats(120) == 1
        assert db.is_live_chat_active("10") is False

    def test_touch_refreshes(self, default_tenant_db):
        db.start_live_chat("10")
        with db.get_connection() as conn:
            conn.execute(
                "UPDATE live_chats SET updated_at = datetime('now', '-121 minutes')"
            )
        db.touch_live_chat("10")
        assert db.end_expired_live_chats(120) == 0

    def test_started_by_recorded(self, default_tenant_db):
        db.start_live_chat("10", started_by="handoff")
        assert db.get_active_live_chat("10")["started_by"] == "handoff"

    def test_cleanup_stale_on_boot(self, default_tenant_db):
        db.start_live_chat("10")
        db.start_live_chat("11")
        assert db.cleanup_stale_live_chats() == 2
        assert db.count_active_live_chats() == 0


class TestSummaries:
    def test_unsummarized_count_and_save(self, default_tenant_db):
        for i in range(3):
            db.save_message("1", "דנה", "user", f"הודעה {i}")
        assert db.get_unsummarized_message_count("1") == 3
        msgs = db.get_messages_for_summarization("1", 3)
        db.save_conversation_summary("1", "סיכום", 3, last_summarized_message_id=msgs[-1]["id"])
        assert db.get_unsummarized_message_count("1") == 0
        assert db.get_latest_summary("1")["summary_text"] == "סיכום"

    def test_summary_replaces_previous(self, default_tenant_db):
        db.save_conversation_summary("1", "ראשון", 1, 1)
        db.save_conversation_summary("1", "שני", 2, 2)
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM conversation_summaries WHERE user_id='1'"
            ).fetchone()
        assert row["c"] == 1
        assert db.get_latest_summary("1")["summary_text"] == "שני"


class TestSettings:
    def test_defaults_and_update(self, default_tenant_db):
        settings = db.get_bot_settings()
        assert settings["tone"] == "friendly"
        assert settings["autopilot_enabled"] == 1
        db.update_bot_settings(tone="formal", custom_prompt="תמיד להציע קפה")
        settings = db.get_bot_settings()
        assert settings["tone"] == "formal"
        assert settings["custom_prompt"] == "תמיד להציע קפה"

    def test_unknown_column_ignored(self, default_tenant_db):
        """‏whitelist — עמודה לא מוכרת לא מגיעה ל-SQL."""
        db.update_bot_settings(evil="DROP TABLE users")
        assert db.get_bot_settings()["tone"] == "friendly"

    def test_autopilot_toggle(self, default_tenant_db):
        assert db.is_autopilot_enabled() is True
        db.update_bot_settings(autopilot_enabled=0)
        assert db.is_autopilot_enabled() is False


class TestIsolation:
    def test_writes_do_not_leak_between_tenants(self, tenant):
        import control_plane as cp

        cp.create_tenant("beta", "עסק שני")
        with tenant_context("acme"):
            db.add_kb_entry("א", "רק-אצל-acme", "x")
        with tenant_context("beta"):
            assert db.count_kb_entries() == 0
        with tenant_context("acme"):
            assert db.count_kb_entries() == 1
