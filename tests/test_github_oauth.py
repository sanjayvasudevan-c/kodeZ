import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.main import app


def test_github_login_redirects_to_github():
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/auth/github/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("https://github.com/login/oauth/authorize")
    assert "oauth_state" in r.cookies


def test_github_callback_rejects_missing_state_cookie():
    client = TestClient(app, base_url="https://testserver")
    r = client.get("/auth/github/callback?code=abc&state=whatever")
    assert r.status_code == 400


def test_github_callback_rejects_mismatched_state():
    client = TestClient(app, base_url="https://testserver")
    client.cookies.set("oauth_state", "expected-state")
    r = client.get("/auth/github/callback?code=abc&state=wrong-state")
    assert r.status_code == 400


def test_github_user_creation_does_not_duplicate_linked_local_account(monkeypatch):
    from app.api import auth as auth_module

    email = f"link-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct horse battery staple"

    client = TestClient(app, base_url="https://testserver")
    reg = client.post("/auth/register", json={"email": email, "password": password})
    assert reg.status_code == 201
    local_user_id = reg.json()["id"]

    github_id = 999_000_111

    async def fake_exchange(code):
        return "fake-github-access-token"

    async def fake_fetch(token):
        return {
            "github_id": github_id,
            "github_username": "octocat",
            "email": email,
            "full_name": "Test User",
            "avatar_url": "https://example.com/avatar.png",
        }

    monkeypatch.setattr(auth_module, "exchange_code_for_token", fake_exchange)
    monkeypatch.setattr(auth_module, "fetch_github_profile", fake_fetch)

    client.get("/auth/github/login", follow_redirects=False)
    state = client.cookies.get("oauth_state")

    r = client.get(f"/auth/github/callback?code=fake&state={state}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == local_user_id  # linked, not duplicated
    assert "access_token" in client.cookies

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT github_id FROM users WHERE email = :email"), {"email": email}
        ).first()
    assert row[0] == github_id

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})
