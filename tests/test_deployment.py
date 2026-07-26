"""טסטים לתצורת הפריסה (‏T4.6).

‏`render.yaml` הוא קובץ שאיש לא מריץ בפיתוח, ולכן הוא נוטה לסטות
מהקוד בשקט — עד ה-deploy. הטסטים כאן אוכפים את מה ששבירתו מפילה
פרודקשן: ‏worker יחיד, דיסק מתמיד, ושמות משתני סביבה שהקוד באמת קורא.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
RENDER = REPO / "render.yaml"


@pytest.fixture(scope="module")
def blueprint() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(RENDER.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def service(blueprint) -> dict:
    return blueprint["services"][0]


class TestSingleProcessTopology:
    """‏worker שני = שני digests, שני גיבויים, ושתי אפליקציות על אותו
    קובץ SQLite. זו ההנחה שכל הטופולוגיה נשענת עליה."""

    def test_one_instance(self, service):
        assert service["numInstances"] == 1

    def test_one_worker(self, service):
        match = re.search(r"--workers\s+(\d+)", service["startCommand"])
        assert match, "‏startCommand חייב לציין --workers במפורש"
        assert match.group(1) == "1"

    def test_concurrency_comes_from_threads(self, service):
        assert re.search(r"--threads\s+\d+", service["startCommand"])

    def test_no_preload(self, service):
        """‏preload מרים את לולאת הבוטים ב-master; ה-thread לא שורד fork."""
        assert "--preload" not in service["startCommand"]

    def test_entrypoint_is_the_wsgi_module(self, service):
        assert "wsgi:app" in service["startCommand"]
        assert (REPO / "wsgi.py").exists()

    def test_gunicorn_is_a_declared_dependency(self, service):
        assert service["startCommand"].startswith("gunicorn")
        requirements = (REPO / "requirements.txt").read_text(encoding="utf-8")
        assert "gunicorn" in requirements


class TestPersistence:
    """בלי דיסק מתמיד, כל deploy מוחק את כל הלקוחות."""

    def test_disk_is_mounted(self, service):
        assert service["disk"]["mountPath"]
        assert service["disk"]["sizeGB"] >= 1

    def test_data_dir_points_at_the_disk(self, service):
        env = {v["key"]: v.get("value") for v in service["envVars"]}
        assert env["DATA_DIR"] == service["disk"]["mountPath"]

    def test_backups_live_on_the_disk_too(self, service):
        env = {v["key"]: v.get("value") for v in service["envVars"]}
        assert env["BACKUP_DIR"].startswith(service["disk"]["mountPath"])


class TestEnvVars:
    def _keys(self, service) -> set:
        return {v["key"] for v in service["envVars"]}

    @pytest.mark.parametrize(
        "key",
        [
            "SECRETS_ENCRYPTION_KEY", "LEDGER_PEPPER_V1", "ADMIN_SECRET_KEY",
            "DATA_DIR", "TENANCY_STRICT", "WEBHOOK_BASE_URL",
        ],
    )
    def test_required_keys_are_present(self, service, key):
        assert key in self._keys(service)

    def test_strict_tenancy_in_production(self, service):
        env = {v["key"]: v.get("value") for v in service["envVars"]}
        assert env["TENANCY_STRICT"] == "true"

    def test_secure_cookie_in_production(self, service):
        env = {v["key"]: v.get("value") for v in service["envVars"]}
        assert env["ADMIN_COOKIE_SECURE"] == "true"

    @pytest.mark.parametrize(
        "key",
        [
            "SECRETS_ENCRYPTION_KEY", "LEDGER_PEPPER_V1", "OPENAI_API_KEY",
            "MANAGER_BOT_TOKEN", "TELEGRAM_BOT_TOKEN",
            "MANAGER_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET",
        ],
    )
    def test_secrets_are_never_committed(self, service, key):
        """‏`sync: false` = מוזן בדשבורד. ערך בקובץ = סוד ב-git."""
        entry = next(v for v in service["envVars"] if v["key"] == key)
        assert entry.get("sync") is False
        assert "value" not in entry, f"{key} לא אמור להופיע עם ערך ב-render.yaml"

    def test_every_key_exists_in_env_example(self, service):
        """‏render.yaml שמגדיר משתנה שאינו מתועד = תצורה נסתרת."""
        documented = {
            line.split("=", 1)[0].strip()
            for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        # ‏PORT ו-PYTHON_VERSION הם של Render עצמו, לא של האפליקציה
        platform_keys = {"PYTHON_VERSION", "PORT"}
        missing = self._keys(service) - documented - platform_keys
        assert not missing, f"חסרים ב-.env.example: {sorted(missing)}"

    def test_health_check_path_exists(self, service):
        assert service["healthCheckPath"] == "/health"
        source = (REPO / "admin" / "app.py").read_text(encoding="utf-8")
        assert '@app.route("/health")' in source


class TestReadme:
    def test_linked_documents_exist(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?:)([^)#]+)\)", readme):
            assert (REPO / target).exists(), f"קישור שבור ב-README: {target}"
