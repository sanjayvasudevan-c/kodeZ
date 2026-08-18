import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.main import app

PASSWORD = "correct horse battery staple"


def make_user(email: str) -> tuple[TestClient, str]:
    client = TestClient(app, base_url="https://testserver")
    r = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return client, r.json()["id"]


def next_github_repo_id() -> int:
    return uuid.uuid4().int % (2**48)


def insert_repository(github_repo_id: int, full_name: str = "octocat/hello-world") -> uuid.UUID:
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


def insert_group(repository_id, owner_user_id, name: str = "Test Group") -> str:
    group_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO groups (id, name, repository_id, owner_id, created_at, updated_at) "
                "VALUES (:id, :name, :rid, :uid, now(), now())"
            ),
            {"id": group_id, "name": name, "rid": str(repository_id), "uid": str(owner_user_id)},
        )
        conn.execute(
            text(
                "INSERT INTO group_members (id, group_id, user_id, role, created_at, updated_at) "
                "VALUES (:id, :gid, :uid, 'owner', now(), now())"
            ),
            {"id": str(uuid.uuid4()), "gid": group_id, "uid": str(owner_user_id)},
        )
    return group_id


def insert_membership(group_id, user_id, role: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO group_members (id, group_id, user_id, role, created_at, updated_at) "
                "VALUES (:id, :gid, :uid, :role, now(), now())"
            ),
            {"id": str(uuid.uuid4()), "gid": str(group_id), "uid": str(user_id), "role": role},
        )


def cleanup_repo_and_group(repo_id) -> None:
    with engine.begin() as conn:
        # group_invitations and group_members cascade from groups.
        conn.execute(text("DELETE FROM groups WHERE repository_id = :rid"), {"rid": str(repo_id)})
        conn.execute(text("DELETE FROM repositories WHERE id = :rid"), {"rid": str(repo_id)})


def delete_user_and_owned_groups(email: str) -> None:
    # Fixture teardown order isn't guaranteed relative to other fixtures' cleanup,
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


@pytest.fixture
def owner():
    email = f"member-owner-{uuid.uuid4().hex[:10]}@example.com"
    client, user_id = make_user(email)
    yield {"client": client, "user_id": user_id, "email": email}
    delete_user_and_owned_groups(email)


@pytest.fixture
def other_user():
    email = f"member-other-{uuid.uuid4().hex[:10]}@example.com"
    client, user_id = make_user(email)
    yield {"client": client, "user_id": user_id, "email": email}
    delete_user_and_owned_groups(email)


@pytest.fixture
def third_user():
    email = f"member-third-{uuid.uuid4().hex[:10]}@example.com"
    client, user_id = make_user(email)
    yield {"client": client, "user_id": user_id, "email": email}
    delete_user_and_owned_groups(email)


@pytest.fixture
def repository():
    github_repo_id = next_github_repo_id()
    repo_id = insert_repository(github_repo_id)
    yield {"id": repo_id, "github_repo_id": github_repo_id}
    cleanup_repo_and_group(repo_id)


@pytest.fixture
def group(owner, repository):
    group_id = insert_group(repository["id"], owner["user_id"])
    yield {"id": group_id, "repository_id": repository["id"], "owner_id": owner["user_id"]}
