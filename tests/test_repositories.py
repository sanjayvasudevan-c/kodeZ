import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import repositories as repositories_module
from app.core.github_api import GitHubRepoNotFoundError, GitHubTokenInvalidError
from app.database import engine
from app.main import app


@pytest.fixture
def github_user(monkeypatch):
    """Register + log in a user, then attach a fake (encrypted) GitHub token directly in the DB."""
    from app.core.crypto import encrypt_secret

    email = f"repo-test-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct horse battery staple"

    client = TestClient(app, base_url="https://testserver")
    client.post("/auth/register", json={"email": email, "password": password})
    client.post("/auth/login", json={"email": email, "password": password})

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET github_access_token = :tok WHERE email = :email"),
            {"tok": encrypt_secret("fake-github-token"), "email": email},
        )

    yield client

    _delete_user_and_owned_groups(email)


@pytest.fixture
def other_user():
    email = f"repo-other-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct horse battery staple"
    client = TestClient(app, base_url="https://testserver")
    client.post("/auth/register", json={"email": email, "password": password})
    client.post("/auth/login", json={"email": email, "password": password})
    yield client
    _delete_user_and_owned_groups(email)


def _delete_user_and_owned_groups(email: str) -> None:
    # Fixture teardown order isn't guaranteed relative to the autouse repo cleanup,
    # so each user fixture must clear its own owner_id FK (ON DELETE RESTRICT) itself.
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM groups WHERE owner_id = "
                "(SELECT id FROM users WHERE email = :email)"
            ),
            {"email": email},
        )
        conn.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})


def _create_group_for_repo(repository_id: str, owner_user_id: str) -> str:
    group_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO groups (id, name, repository_id, owner_id, created_at, updated_at) "
                "VALUES (:id, 'Test Group', :rid, :uid, now(), now())"
            ),
            {"id": group_id, "rid": repository_id, "uid": owner_user_id},
        )
        conn.execute(
            text(
                "INSERT INTO group_members (id, group_id, user_id, role, created_at, updated_at) "
                "VALUES (:id, :gid, :uid, 'owner', now(), now())"
            ),
            {"id": str(uuid.uuid4()), "gid": group_id, "uid": owner_user_id},
        )
    return group_id


@pytest.fixture(autouse=True)
def cleanup_repo():
    yield
    with engine.begin() as conn:
        # groups reference repositories with ON DELETE RESTRICT, so any group
        # created against the test repo must be removed first (cascades to members).
        conn.execute(
            text(
                "DELETE FROM groups WHERE repository_id IN "
                "(SELECT id FROM repositories WHERE github_repo_id = 555000111)"
            )
        )
        conn.execute(text("DELETE FROM repositories WHERE github_repo_id = 555000111"))


FAKE_REPO = {
    "github_repo_id": 555000111,
    "owner_login": "octocat",
    "name": "hello-world",
    "full_name": "octocat/hello-world",
    "html_url": "https://github.com/octocat/hello-world",
    "default_branch": "main",
    "private": False,
}


def test_list_repositories_requires_auth():
    anon = TestClient(app, base_url="https://testserver")
    r = anon.get("/repositories/github")
    assert r.status_code == 401


def test_import_requires_auth():
    anon = TestClient(app, base_url="https://testserver")
    r = anon.post("/repositories", json={"github_repo_id": 1})
    assert r.status_code == 401


def test_list_repositories(github_user, monkeypatch):
    async def fake_list(token):
        assert token == "fake-github-token"
        return [FAKE_REPO]

    monkeypatch.setattr(repositories_module, "list_user_repos", fake_list)

    r = github_user.get("/repositories/github")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["full_name"] == "octocat/hello-world"


def test_list_repositories_no_github_account_linked():
    email = f"nogh-{uuid.uuid4().hex[:12]}@example.com"
    password = "correct horse battery staple"
    client = TestClient(app, base_url="https://testserver")
    client.post("/auth/register", json={"email": email, "password": password})
    client.post("/auth/login", json={"email": email, "password": password})

    r = client.get("/repositories/github")
    assert r.status_code == 400

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})


def test_list_repositories_revoked_token(github_user, monkeypatch):
    async def fake_list(token):
        raise GitHubTokenInvalidError()

    monkeypatch.setattr(repositories_module, "list_user_repos", fake_list)

    r = github_user.get("/repositories/github")
    assert r.status_code == 401


def test_import_repository(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        assert token == "fake-github-token"
        assert repo_id == FAKE_REPO["github_repo_id"]
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    r = github_user.post("/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]})
    assert r.status_code == 201
    body = r.json()
    assert body["github_repo_id"] == FAKE_REPO["github_repo_id"]
    assert body["full_name"] == "octocat/hello-world"
    assert "id" in body


def test_import_repository_prevents_duplicates(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    r1 = github_user.post("/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]})
    r2 = github_user.post("/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM repositories WHERE github_repo_id = :id"),
            {"id": FAKE_REPO["github_repo_id"]},
        ).scalar()
    assert count == 1


def test_import_repository_not_found_or_no_access(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        raise GitHubRepoNotFoundError()

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    r = github_user.post("/repositories", json={"github_repo_id": 999999999})
    assert r.status_code == 404


def test_import_repository_revoked_token(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        raise GitHubTokenInvalidError()

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    r = github_user.post("/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]})
    assert r.status_code == 401


def test_get_imported_repository(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    imported = github_user.post(
        "/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]}
    ).json()

    r = github_user.get(f"/repositories/{imported['id']}")
    assert r.status_code == 200
    assert r.json()["full_name"] == "octocat/hello-world"


def test_get_repository_requires_auth(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)
    imported = github_user.post(
        "/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]}
    ).json()

    anon = TestClient(app, base_url="https://testserver")
    r = anon.get(f"/repositories/{imported['id']}")
    assert r.status_code == 401


def test_get_repository_not_found(github_user):
    r = github_user.get(f"/repositories/{uuid.uuid4()}")
    assert r.status_code == 404


def test_group_member_can_access_grouped_repository(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    imported = github_user.post(
        "/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]}
    ).json()
    me = github_user.get("/users/me").json()
    _create_group_for_repo(imported["id"], me["id"])

    r = github_user.get(f"/repositories/{imported['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == imported["id"]


def test_non_member_cannot_access_grouped_repository(github_user, other_user, monkeypatch):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    imported = github_user.post(
        "/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]}
    ).json()
    me = github_user.get("/users/me").json()
    _create_group_for_repo(imported["id"], me["id"])

    r = other_user.get(f"/repositories/{imported['id']}")
    assert r.status_code == 404


def test_ungrouped_repository_accessible_to_any_authenticated_user(
    github_user, other_user, monkeypatch
):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    imported = github_user.post(
        "/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]}
    ).json()

    # No group has been created for this repository yet — existing behavior preserved.
    r = other_user.get(f"/repositories/{imported['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == imported["id"]


def test_get_grouped_repository_requires_auth(github_user, monkeypatch):
    async def fake_get(token, repo_id):
        return FAKE_REPO

    monkeypatch.setattr(repositories_module, "get_repo_by_id", fake_get)

    imported = github_user.post(
        "/repositories", json={"github_repo_id": FAKE_REPO["github_repo_id"]}
    ).json()
    me = github_user.get("/users/me").json()
    _create_group_for_repo(imported["id"], me["id"])

    anon = TestClient(app, base_url="https://testserver")
    r = anon.get(f"/repositories/{imported['id']}")
    assert r.status_code == 401
