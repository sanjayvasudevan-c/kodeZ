import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api import groups as groups_module
from app.core.crypto import encrypt_secret
from app.core.github_api import GitHubRepoNotFoundError, GitHubTokenInvalidError
from app.database import engine
from app.main import app

PASSWORD = "correct horse battery staple"


def _make_user(email: str) -> tuple[TestClient, str]:
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return client, r.json()["id"]


def _attach_github_token(email: str, token: str = "fake-github-token") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET github_access_token = :tok WHERE email = :email"),
            {"tok": encrypt_secret(token), "email": email},
        )


def _insert_repository(github_repo_id: int, full_name: str = "octocat/hello-world") -> uuid.UUID:
    repo_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO repositories "
                "(id, github_repo_id, owner_login, name, full_name, html_url, default_branch, private, created_at) "
                "VALUES (:id, :grid, 'octocat', 'hello-world', :full_name, "
                "'https://github.com/octocat/hello-world', 'main', false, now())"
            ),
            {"id": str(repo_id), "grid": github_repo_id, "full_name": full_name},
        )
    return repo_id


def _insert_membership(group_id, user_id, role: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO group_members (id, group_id, user_id, role, created_at, updated_at) "
                "VALUES (:id, :gid, :uid, :role, now(), now())"
            ),
            {"id": str(uuid.uuid4()), "gid": str(group_id), "uid": str(user_id), "role": role},
        )


def _cleanup_repo_and_group(repo_id) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM groups WHERE repository_id = :rid"), {"rid": str(repo_id)})
        conn.execute(text("DELETE FROM repositories WHERE id = :rid"), {"rid": str(repo_id)})


def _cleanup_user(email: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})


def _next_github_repo_id() -> int:
    return uuid.uuid4().int % (2**48)


@pytest.fixture
def owner():
    email = f"grp-owner-{uuid.uuid4().hex[:10]}@example.com"
    client, user_id = _make_user(email)
    _attach_github_token(email)
    yield {"client": client, "user_id": user_id, "email": email}
    _cleanup_user(email)


@pytest.fixture
def other_user():
    email = f"grp-other-{uuid.uuid4().hex[:10]}@example.com"
    client, user_id = _make_user(email)
    _attach_github_token(email)
    yield {"client": client, "user_id": user_id, "email": email}
    _cleanup_user(email)


@pytest.fixture
def repository():
    github_repo_id = _next_github_repo_id()
    repo_id = _insert_repository(github_repo_id)
    yield {"id": repo_id, "github_repo_id": github_repo_id}
    _cleanup_repo_and_group(repo_id)


@pytest.fixture(autouse=True)
def mock_github_access(monkeypatch):
    async def fake_get_repo_by_id(token, github_repo_id):
        return {"github_repo_id": github_repo_id}

    monkeypatch.setattr(groups_module, "get_repo_by_id", fake_get_repo_by_id)


# 1, 2, 3: create group succeeds, owner auto-membership, role="owner"
def test_create_group_success(owner, repository):
    r = owner["client"].post("/groups", json={"repository_id": str(repository["id"])})
    assert r.status_code == 201
    body = r.json()
    assert body["repository_id"] == str(repository["id"])
    assert body["owner_id"] == owner["user_id"]
    assert body["my_role"] == "owner"

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT role FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": body["id"], "uid": owner["user_id"]},
        ).first()
    assert row is not None
    assert row[0] == "owner"


def test_create_group_uses_repo_full_name_when_name_omitted(owner, repository):
    r = owner["client"].post("/groups", json={"repository_id": str(repository["id"])})
    assert r.json()["name"] == "octocat/hello-world"


# 4: repository cannot belong to two groups
def test_repository_cannot_have_two_groups(owner, other_user, repository):
    r1 = owner["client"].post("/groups", json={"repository_id": str(repository["id"])})
    assert r1.status_code == 201

    r2 = other_user["client"].post("/groups", json={"repository_id": str(repository["id"])})
    assert r2.status_code == 409


# 5: duplicate concurrent-style creation handled safely
def test_concurrent_group_creation_is_safe(owner, other_user, repository):
    def create(client):
        return client.post("/groups", json={"repository_id": str(repository["id"])})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(create, owner["client"])
        f2 = pool.submit(create, other_user["client"])
        r1, r2 = f1.result(), f2.result()

    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [201, 409]

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM groups WHERE repository_id = :rid"),
            {"rid": str(repository["id"])},
        ).scalar()
    assert count == 1


# 6: cannot create a group around a repo the user can't access via GitHub
def test_create_group_denied_without_github_access(owner, repository, monkeypatch):
    async def fake_get_repo_by_id(token, github_repo_id):
        raise GitHubRepoNotFoundError()

    monkeypatch.setattr(groups_module, "get_repo_by_id", fake_get_repo_by_id)

    r = owner["client"].post("/groups", json={"repository_id": str(repository["id"])})
    assert r.status_code == 403

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM groups WHERE repository_id = :rid"),
            {"rid": str(repository["id"])},
        ).scalar()
    assert count == 0


def test_create_group_revoked_github_token(owner, repository, monkeypatch):
    async def fake_get_repo_by_id(token, github_repo_id):
        raise GitHubTokenInvalidError()

    monkeypatch.setattr(groups_module, "get_repo_by_id", fake_get_repo_by_id)

    r = owner["client"].post("/groups", json={"repository_id": str(repository["id"])})
    assert r.status_code == 401


def test_create_group_repository_not_found(owner):
    r = owner["client"].post("/groups", json={"repository_id": str(uuid.uuid4())})
    assert r.status_code == 404


# 7, 8: membership-gated group detail
def test_non_member_cannot_access_group(owner, other_user, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = other_user["client"].get(f"/groups/{group_id}")
    assert r.status_code == 404


def test_member_can_access_group(owner, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = owner["client"].get(f"/groups/{group_id}")
    assert r.status_code == 200
    assert r.json()["id"] == group_id


# 9: member can list members
def test_member_can_list_members(owner, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = owner["client"].get(f"/groups/{group_id}/members")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["user_id"] == owner["user_id"]
    assert body[0]["role"] == "owner"


def test_non_member_cannot_list_members(owner, other_user, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = other_user["client"].get(f"/groups/{group_id}/members")
    assert r.status_code == 404


# 10, 11: update permissions
def test_plain_member_cannot_update_group(owner, other_user, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]
    _insert_membership(group_id, other_user["user_id"], "member")

    r = other_user["client"].patch(f"/groups/{group_id}", json={"name": "New Name"})
    assert r.status_code == 403


def test_admin_can_update_group(owner, other_user, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]
    _insert_membership(group_id, other_user["user_id"], "admin")

    r = other_user["client"].patch(f"/groups/{group_id}", json={"name": "New Name"})
    assert r.status_code == 200
    assert r.json()["name"] == "New Name"


def test_owner_can_update_group(owner, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = owner["client"].patch(f"/groups/{group_id}", json={"description": "desc"})
    assert r.status_code == 200
    assert r.json()["description"] == "desc"


# 12, 13: delete permissions
def test_member_cannot_delete_group(owner, other_user, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]
    _insert_membership(group_id, other_user["user_id"], "member")

    r = other_user["client"].delete(f"/groups/{group_id}")
    assert r.status_code == 403


def test_owner_can_delete_group(owner, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = owner["client"].delete(f"/groups/{group_id}")
    assert r.status_code == 204

    with engine.begin() as conn:
        group_count = conn.execute(
            text("SELECT count(*) FROM groups WHERE id = :gid"), {"gid": group_id}
        ).scalar()
        member_count = conn.execute(
            text("SELECT count(*) FROM group_members WHERE group_id = :gid"), {"gid": group_id}
        ).scalar()
    assert group_count == 0
    assert member_count == 0  # cascade-deleted


# 14, 15: leave
def test_member_can_leave(owner, other_user, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]
    _insert_membership(group_id, other_user["user_id"], "member")

    r = other_user["client"].post(f"/groups/{group_id}/leave")
    assert r.status_code == 204

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group_id, "uid": other_user["user_id"]},
        ).scalar()
    assert count == 0


def test_owner_cannot_leave(owner, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    r = owner["client"].post(f"/groups/{group_id}/leave")
    assert r.status_code == 409

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM group_members WHERE group_id = :gid"), {"gid": group_id}
        ).scalar()
    assert count == 1


# 16: listing only returns the caller's groups
def test_list_groups_only_returns_own_groups(owner, other_user):
    repo_a = _insert_repository(_next_github_repo_id(), "octocat/repo-a")
    repo_b = _insert_repository(_next_github_repo_id(), "octocat/repo-b")
    try:
        group_a = owner["client"].post("/groups", json={"repository_id": str(repo_a)}).json()
        group_b = other_user["client"].post("/groups", json={"repository_id": str(repo_b)}).json()

        r = owner["client"].get("/groups")
        assert r.status_code == 200
        ids = {g["id"] for g in r.json()}
        assert group_a["id"] in ids
        assert group_b["id"] not in ids
    finally:
        _cleanup_repo_and_group(repo_a)
        _cleanup_repo_and_group(repo_b)


def test_list_groups_requires_auth():
    anon = TestClient(app, base_url="https://testserver")
    r = anon.get("/groups")
    assert r.status_code == 401


# 17: duplicate membership impossible
def test_duplicate_membership_impossible(owner, repository):
    group_id = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()["id"]

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO group_members (id, group_id, user_id, role, created_at, updated_at) "
                    "VALUES (:id, :gid, :uid, 'member', now(), now())"
                ),
                {"id": str(uuid.uuid4()), "gid": group_id, "uid": owner["user_id"]},
            )


# 18: ownership invariant preserved
def test_ownership_invariant(owner, other_user, repository):
    body = owner["client"].post(
        "/groups", json={"repository_id": str(repository["id"])}
    ).json()

    with engine.begin() as conn:
        group_row = conn.execute(
            text("SELECT owner_id FROM groups WHERE id = :gid"), {"gid": body["id"]}
        ).first()
        owner_rows = conn.execute(
            text("SELECT user_id FROM group_members WHERE group_id = :gid AND role = 'owner'"),
            {"gid": body["id"]},
        ).fetchall()

    assert str(group_row[0]) == owner["user_id"]
    assert len(owner_rows) == 1
    assert str(owner_rows[0][0]) == owner["user_id"]

    # A second owner-role row for the same group must be rejected by the partial unique index.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO group_members (id, group_id, user_id, role, created_at, updated_at) "
                    "VALUES (:id, :gid, :uid, 'owner', now(), now())"
                ),
                {"id": str(uuid.uuid4()), "gid": body["id"], "uid": other_user["user_id"]},
            )
