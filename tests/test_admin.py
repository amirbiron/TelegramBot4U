"""טסטים לפאנל האדמין — אימות, ‏CSRF, ‏CRUD של בסיס הידע ופערי ידע."""

import pytest

import database as db


@pytest.fixture
def app(default_tenant_db, monkeypatch):
    import admin.app as admin_app
    from admin.app import create_admin_app

    # ‏rate limit ההתחברות הוא state ברמת מודול — בלי איפוס, טסט שממצה
    # את המכסה חוסם את כל הטסטים שאחריו.
    admin_app._login_attempts.clear()

    application = create_admin_app()
    application.config["TESTING"] = True
    # CSRF נבדק בטסט ייעודי; שאר הטסטים עובדים בלעדיו כדי לא לגרד טוקנים
    application.config["WTF_CSRF_ENABLED"] = False
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_client(client):
    client.post("/login", data={"username": "admin", "password": "test-password"})
    return client


class TestAuth:
    def test_health_is_public(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json["status"] == "ok"

    def test_protected_routes_redirect(self, client):
        for path in ("/", "/kb", "/knowledge-gaps", "/conversations", "/my-bot"):
            resp = client.get(path)
            assert resp.status_code == 302, path
            assert "/login" in resp.headers["Location"], path

    def test_login_success(self, client):
        resp = client.post(
            "/login", data={"username": "admin", "password": "test-password"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["Location"] in ("/", "http://localhost/")

    def test_login_failure(self, client):
        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert resp.status_code == 200
        assert "שגויים" in resp.get_data(as_text=True)

    def test_login_rate_limited(self, client):
        for _ in range(5):
            client.post("/login", data={"username": "admin", "password": "wrong"})
        resp = client.post("/login", data={"username": "admin", "password": "wrong"})
        assert "יותר מדי ניסיונות" in resp.get_data(as_text=True)

    def test_logout_clears_session(self, auth_client):
        auth_client.get("/logout")
        assert auth_client.get("/kb").status_code == 302

    def test_htmx_unauthenticated_gets_redirect_header(self, client):
        resp = client.post("/kb/delete/1", headers={"HX-Request": "true"})
        assert resp.status_code == 401
        assert resp.headers["HX-Redirect"].endswith("/login")

    def test_platform_route_hidden_from_non_admin(self, auth_client):
        """לא platform admin ⇒ 404, בלי לחשוף שהאזור קיים."""
        assert auth_client.get("/platform").status_code == 404


class TestCsrf:
    def test_post_without_token_rejected(self, app, default_tenant_db):
        app.config["WTF_CSRF_ENABLED"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        resp = client.post("/kb/add", data={"category": "א", "title": "ב", "content": "ג"})
        assert resp.status_code in (302, 400)
        assert db.count_kb_entries(active_only=False) == 0

    def test_htmx_csrf_error_does_not_swap(self, app, default_tenant_db):
        app.config["WTF_CSRF_ENABLED"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
        resp = client.post("/kb/delete/1", headers={"HX-Request": "true"})
        assert resp.status_code == 403
        assert resp.headers["HX-Reswap"] == "none"


class TestKnowledgeBase:
    def test_add_entry(self, auth_client):
        resp = auth_client.post(
            "/kb/add",
            data={"category": "מחירון", "title": "תספורות", "content": "99 ש\"ח"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        entries = db.get_all_kb_entries()
        assert len(entries) == 1
        assert entries[0]["title"] == "תספורות"

    def test_add_entry_validates_required_fields(self, auth_client):
        auth_client.post("/kb/add", data={"category": "", "title": "", "content": ""})
        assert db.count_kb_entries(active_only=False) == 0

    def test_edit_entry(self, auth_client):
        entry_id = db.add_kb_entry("מחירון", "תספורות", "99")
        auth_client.post(
            f"/kb/edit/{entry_id}",
            data={"category": "מחירון", "title": "תספורות", "content": "120"},
        )
        assert "120" in db.get_kb_entry(entry_id)["content"]

    def test_edit_missing_entry_redirects(self, auth_client):
        resp = auth_client.get("/kb/edit/999")
        assert resp.status_code == 302

    def test_delete_entry_via_htmx(self, auth_client):
        entry_id = db.add_kb_entry("מחירון", "תספורות", "99")
        resp = auth_client.post(
            f"/kb/delete/{entry_id}", headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert db.get_kb_entry(entry_id) is None

    def test_delete_last_entry_swaps_empty_state(self, auth_client):
        """‏HTMX — DOM consistency: כשהטבלה מתרוקנת מחליפים את כל הקונטיינר."""
        entry_id = db.add_kb_entry("מחירון", "תספורות", "99")
        resp = auth_client.post(
            f"/kb/delete/{entry_id}", headers={"HX-Request": "true"},
        )
        assert resp.headers["HX-Retarget"] == "#kb-table-wrapper"
        assert "אין עדיין רשומות" in resp.get_data(as_text=True)

    def test_list_shows_entries(self, auth_client):
        db.add_kb_entry("מחירון", "תספורת נשים", "99")
        body = auth_client.get("/kb").get_data(as_text=True)
        assert "תספורת נשים" in body

    def test_no_rebuild_route(self, auth_client):
        """אין RAG — ולכן אין /kb/rebuild."""
        assert auth_client.post("/kb/rebuild").status_code == 404

    def test_search_escapes_wildcards(self, auth_client):
        db.add_kb_entry("כללי", "חניה", "יש חניה")
        body = auth_client.get(
            "/kb/search?q=%25", headers={"HX-Request": "true"},
        ).get_data(as_text=True)
        assert "לא נמצאו תוצאות" in body

    def test_search_finds_entry(self, auth_client):
        db.add_kb_entry("כללי", "חניה", "יש חניה בחינם")
        body = auth_client.get(
            "/kb/search?q=חניה", headers={"HX-Request": "true"},
        ).get_data(as_text=True)
        assert "חניה" in body
        assert "לא נמצאו" not in body

    def test_empty_search_returns_nothing(self, auth_client):
        resp = auth_client.get("/kb/search?q=", headers={"HX-Request": "true"})
        assert resp.get_data(as_text=True) == ""


class TestKnowledgeGaps:
    def test_list_and_resolve(self, auth_client):
        gap_id = db.save_unanswered_question("1", "דנה", "יש מכשיר X?")
        body = auth_client.get("/knowledge-gaps").get_data(as_text=True)
        assert "יש מכשיר X?" in body

        resp = auth_client.post(
            f"/knowledge-gaps/{gap_id}/resolve",
            data={"status": "resolved"}, headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert db.get_unanswered_question(gap_id)["status"] == "resolved"

    def test_invalid_status_rejected(self, auth_client):
        gap_id = db.save_unanswered_question("1", "דנה", "שאלה")
        resp = auth_client.post(
            f"/knowledge-gaps/{gap_id}/resolve",
            data={"status": "; DROP TABLE"}, headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422
        assert db.get_unanswered_question(gap_id)["status"] == "open"

    def test_adding_kb_entry_closes_the_gap(self, auth_client):
        gap_id = db.save_unanswered_question("1", "דנה", "יש מכשיר X?")
        auth_client.post(
            "/kb/add",
            data={
                "category": "מלאי", "title": "מכשיר X", "content": "יש במלאי",
                "gap_id": str(gap_id),
            },
        )
        assert db.get_unanswered_question(gap_id)["status"] == "resolved"


class TestOtherPages:
    def test_dashboard_renders(self, auth_client):
        db.add_kb_entry("מחירון", "תספורות", "99")
        body = auth_client.get("/").get_data(as_text=True)
        assert "לוח בקרה" in body

    def test_conversations_window_state(self, auth_client):
        db.upsert_user("1", "דנה", chat_id="10", inbound=True)
        body = auth_client.get("/conversations").get_data(as_text=True)
        assert "דנה" in body
        assert "פתוח" in body

    def test_conversations_rejects_bad_user_id(self, auth_client):
        """פרמטר לא תקין מה-URL לא מגיע לשאילתה."""
        resp = auth_client.get("/conversations?user_id=' OR 1=1--")
        assert resp.status_code == 200

    def test_my_bot_without_bot(self, auth_client):
        body = auth_client.get("/my-bot").get_data(as_text=True)
        assert "הבוט שלי" in body


class TestTenantBinding:
    def test_session_tenant_is_bound(self, app, tenant):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["tenant_id"] = "acme"
        from tenancy import tenant_context

        with tenant_context("acme"):
            db.add_kb_entry("א", "רק-acme", "x")
        body = client.get("/kb").get_data(as_text=True)
        assert "רק-acme" in body

    def test_suspended_tenant_logs_out(self, app, tenant):
        import control_plane as cp

        client = app.test_client()
        with client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["tenant_id"] = "acme"
        cp.set_tenant_status("acme", "suspended")
        resp = client.get("/kb")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]
