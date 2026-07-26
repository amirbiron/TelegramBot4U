"""טסטים לגיבוי הלילי ולשחזור ממנו (‏T4.4).

הטסט המרכזי כאן הוא **שחזור**: גיבוי שלא ניסו לשחזר ממנו אינו גיבוי,
והוא הדבר היחיד שמוכיח שהקובץ שנוצר שמיש.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import backup_service
import database as db
from services import backup_job
from tenancy import tenant_context

IL = ZoneInfo("Asia/Jerusalem")
STAMP = "2026-07-15"


def _at(hour: int, day: int = 15) -> datetime:
    return datetime(2026, 7, day, hour, 30, tzinfo=IL)


class TestBackupAndRestore:
    def test_tenant_db_is_backed_up(self, tenant, tmp_path):
        with tenant_context(tenant):
            db.add_kb_entry("שעות", "שעות פתיחה", "פתוחים 9–17")

        assert backup_service.backup_tenant(tenant, STAMP) is True
        backup_file = tmp_path / "backups" / STAMP / tenant / "chatbot.db"
        assert backup_file.exists()
        assert backup_file.stat().st_size > 0

    def test_restore_brings_the_data_back(self, tenant):
        """הבדיקה האמיתית: מוחקים נתונים ומשחזרים מהגיבוי."""
        with tenant_context(tenant):
            db.add_kb_entry("שעות", "שעות פתיחה", "פתוחים 9–17")
            before = db.count_kb_entries()

        backup_service.backup_tenant(tenant, STAMP)

        with tenant_context(tenant):
            for row in db.get_all_kb_entries():
                db.delete_kb_entry(row["id"])
            assert db.count_kb_entries() == 0

        assert backup_service.restore_tenant(tenant, STAMP) is True
        with tenant_context(tenant):
            assert db.count_kb_entries() == before

    def test_restore_without_a_backup_fails_cleanly(self, tenant):
        assert backup_service.restore_tenant(tenant, "1999-01-01") is False

    def test_missing_db_file_is_reported(self, platform_db):
        """‏tenant רשום בלי קובץ — כשל מדווח, לא חריגה."""
        assert backup_service.backup_tenant("no-such-tenant", STAMP) is False

    def test_platform_db_is_backed_up(self, platform_db, tmp_path):
        assert backup_service.backup_platform_db(STAMP) is True
        assert (tmp_path / "backups" / STAMP / "_platform" / "platform.db").exists()

    def test_backup_is_consistent_under_an_open_connection(self, tenant, tmp_path):
        """‏WAL פתוח באמצע כתיבה — ‏cp היה מייצר DB פגום."""
        with tenant_context(tenant):
            db.add_kb_entry("כללי", "א", "1")
            with db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO kb_entries (category, title, content) VALUES (?, ?, ?)",
                    ("כללי", "ב", "2"),
                )
                assert backup_service.backup_tenant(tenant, STAMP) is True

        import sqlite3

        restored = sqlite3.connect(
            str(tmp_path / "backups" / STAMP / tenant / "chatbot.db")
        )
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()


class TestRunBackup:
    def test_covers_every_tenant_and_the_platform(self, platform_db):
        platform_db.create_tenant("a", "עסק א")
        platform_db.create_tenant("b", "עסק ב")
        result = backup_service.run_backup(STAMP, _at(3).timestamp())
        assert result["tenants_ok"] == 2
        assert result["tenants_failed"] == 0
        assert result["platform_ok"] is True

    def test_one_failing_tenant_does_not_stop_the_rest(self, platform_db):
        platform_db.create_tenant("a", "עסק א")
        platform_db.create_tenant("b", "עסק ב")
        calls = {"n": 0}
        real = backup_service.backup_tenant

        def _flaky(tenant_id, stamp):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("דיסק מלא")
            return real(tenant_id, stamp)

        with patch.object(backup_service, "backup_tenant", _flaky):
            result = backup_service.run_backup(STAMP, _at(3).timestamp())

        assert result["tenants_failed"] == 1
        assert result["tenants_ok"] == 1


class TestPrune:
    def _make(self, tmp_path, name: str):
        folder = tmp_path / "backups" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "marker").write_text("x", encoding="utf-8")
        return folder

    def test_old_folders_are_removed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKUP_RETENTION_DAYS", "14")
        old = self._make(tmp_path, "2026-06-01")
        fresh = self._make(tmp_path, "2026-07-15")

        removed = backup_service._prune_old_backups(_at(3).timestamp())

        assert removed == 1
        assert not old.exists()
        assert fresh.exists()

    def test_non_date_folders_are_left_alone(self, tmp_path, monkeypatch):
        """תיקייה ששמה אינו תאריך — אין לנו דרך לדעת מה היא."""
        monkeypatch.setenv("BACKUP_RETENTION_DAYS", "1")
        keep = self._make(tmp_path, "manual-before-migration")
        backup_service._prune_old_backups(_at(3).timestamp())
        assert keep.exists()

    def test_prune_on_a_missing_root_is_a_noop(self, tmp_path):
        assert backup_service._prune_old_backups(_at(3).timestamp()) == 0

    @pytest.mark.parametrize("bad", ["", "לא מספר", "0", "-5"])
    def test_invalid_retention_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("BACKUP_RETENTION_DAYS", bad)
        assert backup_service._retention_days() >= 1


class TestUploadHook:
    def test_hook_is_called_per_artifact(self, tenant):
        calls = []
        backup_service.set_upload_hook(lambda path, key: calls.append(key))
        try:
            backup_service.backup_tenant(tenant, STAMP)
            backup_service.backup_platform_db(STAMP)
        finally:
            backup_service.set_upload_hook(None)

        assert f"{STAMP}/{tenant}/chatbot.db" in calls
        assert f"{STAMP}/_platform/platform.db" in calls

    def test_hook_failure_does_not_fail_the_backup(self, tenant, tmp_path):
        """הגיבוי המקומי כבר על הדיסק — כשל בהעלאה לא מבטל אותו."""
        def _boom(path, key):
            raise RuntimeError("‏S3 לא זמין")

        backup_service.set_upload_hook(_boom)
        try:
            assert backup_service.backup_tenant(tenant, STAMP) is True
        finally:
            backup_service.set_upload_hook(None)
        assert (tmp_path / "backups" / STAMP / tenant / "chatbot.db").exists()


class TestScheduling:
    def test_not_due_before_the_hour(self, platform_db):
        assert backup_job.is_backup_due(_at(2)) is False

    def test_due_after_the_hour(self, platform_db):
        assert backup_job.is_backup_due(_at(3)) is True

    def test_not_due_twice_the_same_day(self, platform_db):
        backup_job.mark_backup_ran(_at(3))
        assert backup_job.is_backup_due(_at(9)) is False

    def test_due_again_the_next_day(self, platform_db):
        backup_job.mark_backup_ran(_at(3, day=15))
        assert backup_job.is_backup_due(_at(3, day=16)) is True

    def test_backup_runs_before_retention(self):
        """‏purge לפני גיבוי היה מוציא מהגיבוי את מה שנמחק."""
        from services import retention_service

        assert backup_job.BACKUP_HOUR_LOCAL < retention_service.RETENTION_HOUR_LOCAL

    def test_all_tenants_share_one_stamp(self, platform_db):
        """ריצה שחוצה חצות לא מפזרת tenants בין שתי תיקיות."""
        stamps = []
        with patch.object(
            backup_service, "run_backup",
            lambda stamp, now_epoch: stamps.append(stamp) or {},
        ):
            backup_job.run_backup_now(_at(3))
        assert stamps == ["2026-07-15"]
