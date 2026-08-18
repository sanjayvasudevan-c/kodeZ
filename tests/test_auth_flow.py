import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.main import app

client = TestClient(app, base_url="https://testserver")


@pytest.fixture
def credentials():
    email = f"test-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct horse battery staple"
    yield {"email": email, "password": password}
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})


def test_register_returns_no_password_hash(credentials):
    r = client.post("/auth/register", json=credentials)
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == credentials["email"]
    assert "password_hash" not in body
    assert "password" not in body


def test_register_duplicate_email_rejected(credentials):
    r1 = client.post("/auth/register", json=credentials)
    assert r1.status_code == 201
    r2 = client.post("/auth/register", json=credentials)
    assert r2.status_code == 400


def test_password_is_stored_as_argon2id_hash_only(credentials):
    client.post("/auth/register", json=credentials)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT password_hash FROM users WHERE email = :email"),
            {"email": credentials["email"]},
        ).first()
    assert row is not None
    assert row[0] != credentials["password"]
    assert row[0].startswith("$argon2id$")


def test_login_sets_cookies_and_no_tokens_in_body(credentials):
    client.post("/auth/register", json=credentials)
    r = client.post("/auth/login", json=credentials)
    assert r.status_code == 200
    assert "access_token" in r.cookies
    assert "refresh_token" in r.cookies
    body = r.json()
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "password_hash" not in body


def test_login_wrong_password_rejected(credentials):
    client.post("/auth/register", json=credentials)
    r = client.post("/auth/login", json={"email": credentials["email"], "password": "wrong"})
    assert r.status_code == 401


def test_protected_route_requires_auth():
    anon = TestClient(app, base_url="https://testserver")
    r = anon.get("/users/me")
    assert r.status_code == 401


def test_protected_route_works_after_login(credentials):
    session = TestClient(app, base_url="https://testserver")
    session.post("/auth/register", json=credentials)
    session.post("/auth/login", json=credentials)
    r = session.get("/users/me")
    assert r.status_code == 200
    assert r.json()["email"] == credentials["email"]


def test_refresh_token_stored_hashed_and_rotates(credentials):
    session = TestClient(app, base_url="https://testserver")
    session.post("/auth/register", json=credentials)
    session.post("/auth/login", json=credentials)

    old_raw_refresh = session.cookies.get("refresh_token")

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT token_hash FROM refresh_tokens")).fetchall()
    assert all(row[0] != old_raw_refresh for row in rows), "raw refresh token must never be stored"

    r = session.post("/auth/refresh")
    assert r.status_code == 204
    new_raw_refresh = session.cookies.get("refresh_token")
    assert new_raw_refresh is not None
    assert new_raw_refresh != old_raw_refresh

    # old token must now be revoked and unusable
    replay_client = TestClient(app, base_url="https://testserver")
    replay_client.cookies.set("refresh_token", old_raw_refresh)
    r2 = replay_client.post("/auth/refresh")
    assert r2.status_code == 401

    # new token still works
    r3 = session.get("/users/me")
    assert r3.status_code == 200


def test_refresh_reuse_revokes_entire_family(credentials):
    session = TestClient(app, base_url="https://testserver")
    session.post("/auth/register", json=credentials)
    session.post("/auth/login", json=credentials)

    first_refresh = session.cookies.get("refresh_token")
    session.post("/auth/refresh")
    second_refresh = session.cookies.get("refresh_token")

    # replay the already-rotated first token -> reuse detected, family revoked
    replay_client = TestClient(app, base_url="https://testserver")
    replay_client.cookies.set("refresh_token", first_refresh)
    r = replay_client.post("/auth/refresh")
    assert r.status_code == 401

    # the legitimately-rotated second token should now be revoked too
    victim_client = TestClient(app, base_url="https://testserver")
    victim_client.cookies.set("refresh_token", second_refresh)
    r2 = victim_client.post("/auth/refresh")
    assert r2.status_code == 401


def test_logout_revokes_refresh_token(credentials):
    session = TestClient(app, base_url="https://testserver")
    session.post("/auth/register", json=credentials)
    session.post("/auth/login", json=credentials)

    r = session.post("/auth/logout")
    assert r.status_code == 204

    r2 = session.post("/auth/refresh")
    assert r2.status_code == 401


def test_logout_all_revokes_every_session(credentials):
    session_a = TestClient(app, base_url="https://testserver")
    session_a.post("/auth/register", json=credentials)
    session_a.post("/auth/login", json=credentials)

    session_b = TestClient(app, base_url="https://testserver")
    session_b.post("/auth/login", json=credentials)

    r = session_a.post("/auth/logout-all")
    assert r.status_code == 204

    r_a = session_a.post("/auth/refresh")
    assert r_a.status_code == 401
    r_b = session_b.post("/auth/refresh")
    assert r_b.status_code == 401
