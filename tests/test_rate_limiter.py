"""טסטים ל-rate limiter — חלונות הזזה ובידוד פר-tenant."""

import config
import rate_limiter
from rate_limiter import check_rate_limit, record_message
from tenancy import tenant_context


class TestWindows:
    def test_under_limit_passes(self):
        assert check_rate_limit("1") is None

    def test_minute_window_trips(self, monkeypatch):
        monkeypatch.setattr(rate_limiter, "_WINDOWS", [(60, 3, "minute")])
        for _ in range(3):
            assert check_rate_limit("1") is None
            record_message("1")
        assert check_rate_limit("1") == "minute"

    def test_check_does_not_record(self, monkeypatch):
        """הפרדת check מ-record: בדיקה חוזרת לא צורכת מהמכסה."""
        monkeypatch.setattr(rate_limiter, "_WINDOWS", [(60, 2, "minute")])
        for _ in range(10):
            assert check_rate_limit("1") is None
        record_message("1")
        record_message("1")
        assert check_rate_limit("1") == "minute"

    def test_old_timestamps_pruned(self, monkeypatch):
        import time

        monkeypatch.setattr(rate_limiter, "_WINDOWS", [(60, 2, "minute")])
        key = rate_limiter._bucket_key("1")
        rate_limiter._touch(key)
        # שתי חותמות מלפני שעתיים — מחוץ לכל החלונות
        rate_limiter._user_timestamps[key].extend([time.time() - 7200] * 2)
        assert check_rate_limit("1") is None

    def test_returns_window_name_not_customer_text(self, monkeypatch):
        """חריגה לא מייצרת טקסט ללקוח — רק שם החלון להתראה לבעלים."""
        monkeypatch.setattr(rate_limiter, "_WINDOWS", [(60, 1, "minute")])
        record_message("1")
        result = check_rate_limit("1")
        assert result == "minute"
        assert "המתן" not in result


class TestTenantIsolation:
    def test_limits_are_per_tenant(self, monkeypatch):
        monkeypatch.setattr(rate_limiter, "_WINDOWS", [(60, 2, "minute")])
        with tenant_context("salon-a"):
            record_message("1")
            record_message("1")
            assert check_rate_limit("1") == "minute"
        with tenant_context("salon-b"):
            assert check_rate_limit("1") is None


class TestEviction:
    def test_lru_cap_respected(self, monkeypatch):
        monkeypatch.setattr(rate_limiter, "_MAX_TRACKED_USERS", 5)
        for i in range(20):
            record_message(str(i))
        assert len(rate_limiter._user_timestamps) <= 5

    def test_check_also_evicts(self, monkeypatch):
        """גם check מפנה — אחרת משתמש חסום מגדיל את המילון בלי גבול."""
        monkeypatch.setattr(rate_limiter, "_MAX_TRACKED_USERS", 5)
        for i in range(20):
            check_rate_limit(str(i))
        assert len(rate_limiter._user_timestamps) <= 5


class TestConfiguredDefaults:
    def test_windows_match_config(self):
        limits = {name: limit for _, limit, name in rate_limiter._WINDOWS}
        assert limits["minute"] == config.RATE_LIMIT_PER_MINUTE
        assert limits["hour"] == config.RATE_LIMIT_PER_HOUR
        assert limits["day"] == config.RATE_LIMIT_PER_DAY
