"""טסטים לזכות המחיקה ול-retention.

הטסט המרכזי כאן הוא לולאתי על **סכימת ה-DB בפועל**: כל טבלה עם עמודת
`user_id` חייבת להיות מכוסה ע"י `delete_user_data`, או להופיע ברשימת
החריגים המתועדת. כך הוספת טבלה חדשה בלי עדכון המחיקה נכשלת אוטומטית
(‏CLAUDE.md — פרטיות).
"""

import database as db


def _tables_with_user_id() -> set[str]:
    with db.get_connection() as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        found = set()
        for table in tables:
            cols = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}
            if "user_id" in cols:
                found.add(table)
        return found


class TestDeletionCoverage:
    def test_every_user_id_table_is_covered(self, default_tenant_db):
        covered = set(db._USER_DATA_TABLES) | set(db._USER_DATA_TABLES_RETAINED) | {"users"}
        missing = _tables_with_user_id() - covered
        assert not missing, (
            f"טבלאות עם user_id שאינן מכוסות ב-delete_user_data: {sorted(missing)}. "
            "יש להוסיף ל-_USER_DATA_TABLES (או ל-_USER_DATA_TABLES_RETAINED עם "
            "הנמקה) ולעדכן את docs/privacy_data_matrix.md באותו commit."
        )

    def test_delete_removes_everything(self, default_tenant_db):
        db.upsert_user("1", "דנה", inbound=True)
        db.save_message("1", "דנה", "user", "שאלה")
        db.save_conversation_summary("1", "סיכום", 1, 1)
        db.save_unanswered_question("1", "דנה", "שאלה קשה")
        db.start_live_chat("10", "1", "דנה")
        db.add_customer_fact("1", "preference", "אלרגית ללטקס", 0.9)

        result = db.delete_user_data("1")

        assert db.get_user("1") is None
        assert db.get_conversation_history("1") == []
        assert db.get_latest_summary("1") is None
        assert db.get_unanswered_questions() == []
        assert db.get_customer_facts("1") == []
        assert "__failed_tables__" not in result

    def test_blocked_users_survives_deletion(self, default_tenant_db):
        """‏hold צר לאכיפה — החסימה נשארת גם אחרי מחיקת הנתונים."""
        db.upsert_user("1", "דנה", inbound=True)
        db.block_user("1", "דנה", block_category="abuse")
        db.delete_user_data("1")
        assert db.is_user_blocked("1") is True

    def test_deletion_is_idempotent_under_concurrency(self, default_tenant_db, monkeypatch):
        """בקשה שנייה בזמן שהראשונה בעיבוד — לא מבצעת מחיקה כפולה."""
        db.upsert_user("1", "דנה", inbound=True)
        seen = {}

        original = db._delete_user_data_impl

        def _impl(user_id):
            # בתוך העיבוד, בקשה נוספת חייבת לחזור מיד
            seen["nested"] = db.delete_user_data(user_id)
            return original(user_id)

        monkeypatch.setattr(db, "_delete_user_data_impl", _impl)
        db.delete_user_data("1")
        assert seen["nested"] == {"already_in_progress": True}

    def test_ledger_records_deletion(self, default_tenant_db):
        from utils.consent_ledger import EVENT_DELETION_COMPLETED, get_events_for_subject

        db.upsert_user("1", "דנה", inbound=True)
        db.delete_user_data("1")
        events = get_events_for_subject("1", db.CHANNEL)
        types = [e["event_type"] for e in events]
        assert "deletion_requested" in types
        assert EVENT_DELETION_COMPLETED in types


class TestRetention:
    def test_purge_removes_old_conversations(self, default_tenant_db):
        db.save_message("1", "דנה", "user", "ישן")
        with db.get_connection() as conn:
            conn.execute("UPDATE conversations SET created_at = datetime('now', '-400 days')")
        db.save_message("1", "דנה", "user", "חדש")
        result = db.purge_old_data(conversation_days=365)
        assert result["conversations"] == 1
        assert [m["message"] for m in db.get_conversation_history("1")] == ["חדש"]

    def test_purge_respects_ledger_categories(self, default_tenant_db):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO consent_ledger (subject_hash, channel, category, event_type, "
                "event_at) VALUES ('h', 'telegram_business', 'audit', 'deletion_completed', "
                "datetime('now', '-30 months'))"
            )
            conn.execute(
                "INSERT INTO consent_ledger (subject_hash, channel, category, event_type, "
                "event_at) VALUES ('h', 'telegram_business', 'consent', 'consent_given', "
                "datetime('now', '-30 months'))"
            )
        result = db.purge_old_data(audit_months=24, consent_ledger_years=5)
        assert result["consent_ledger_audit"] == 1
        assert result["consent_ledger_consent"] == 0
