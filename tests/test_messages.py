import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.main import app
from tests.conftest import insert_membership


def _cmid() -> str:
    return str(uuid.uuid4())


# --- authorization -------------------------------------------------------


def test_member_can_send(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "hello", "client_message_id": _cmid()}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["body"] == "hello"
    assert body["sender_id"] == owner["user_id"]
    assert body["seq"] == 1


def test_non_member_cannot_send(owner, other_user, group):
    r = other_user["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "hi", "client_message_id": _cmid()}
    )
    assert r.status_code == 404


def test_member_can_read(owner, group):
    owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "hello", "client_message_id": _cmid()}
    )
    r = owner["client"].get(f"/groups/{group['id']}/messages")
    assert r.status_code == 200
    assert len(r.json()["messages"]) == 1


def test_non_member_cannot_read(owner, other_user, group):
    r = other_user["client"].get(f"/groups/{group['id']}/messages")
    assert r.status_code == 404


def test_unauthenticated_access_rejected(group):
    anon = TestClient(app, base_url="https://testserver")
    r = anon.post(
        f"/groups/{group['id']}/messages", json={"body": "hi", "client_message_id": _cmid()}
    )
    assert r.status_code == 401

    r2 = anon.get(f"/groups/{group['id']}/messages")
    assert r2.status_code == 401


# --- sender identity -------------------------------------------------------


def test_sender_comes_from_authenticated_user(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = other_user["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "hi", "client_message_id": _cmid()}
    )
    assert r.status_code == 201
    assert r.json()["sender_id"] == other_user["user_id"]


def test_sender_spoofing_is_impossible(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = other_user["client"].post(
        f"/groups/{group['id']}/messages",
        json={"body": "hi", "client_message_id": _cmid(), "sender_id": owner["user_id"]},
    )
    assert r.status_code == 201
    # extra field is ignored; sender is always the authenticated caller
    assert r.json()["sender_id"] == other_user["user_id"]


# --- body validation -------------------------------------------------------


def test_body_cannot_be_empty(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "", "client_message_id": _cmid()}
    )
    assert r.status_code == 422


def test_body_cannot_exceed_10000_chars(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/messages",
        json={"body": "x" * 10001, "client_message_id": _cmid()},
    )
    assert r.status_code == 422

    r2 = owner["client"].post(
        f"/groups/{group['id']}/messages",
        json={"body": "x" * 10000, "client_message_id": _cmid()},
    )
    assert r2.status_code == 201


# --- sequencing -------------------------------------------------------


def test_message_receives_correct_sequence(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "first", "client_message_id": _cmid()}
    )
    assert r.json()["seq"] == 1


def test_sequence_increments(owner, group):
    seqs = []
    for i in range(4):
        r = owner["client"].post(
            f"/groups/{group['id']}/messages",
            json={"body": f"msg {i}", "client_message_id": _cmid()},
        )
        seqs.append(r.json()["seq"])
    assert seqs == [1, 2, 3, 4]


def test_different_groups_have_independent_sequences(owner, group):
    from tests.conftest import insert_group, next_github_repo_id, insert_repository

    repo_id_2 = insert_repository(next_github_repo_id())
    group_id_2 = insert_group(repo_id_2, owner["user_id"], name="Second group")
    try:
        r1 = owner["client"].post(
            f"/groups/{group['id']}/messages", json={"body": "a", "client_message_id": _cmid()}
        )
        r2 = owner["client"].post(
            f"/groups/{group_id_2}/messages", json={"body": "b", "client_message_id": _cmid()}
        )
        assert r1.json()["seq"] == 1
        assert r2.json()["seq"] == 1
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM groups WHERE id = :id"), {"id": group_id_2})
            conn.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repo_id_2)})


def test_client_created_timestamp_does_not_determine_ordering(owner, group):
    # A message claiming a far-future/past client timestamp still gets the next
    # sequential seq, in send order — ordering is seq-based, not timestamp-based.
    r1 = owner["client"].post(
        f"/groups/{group['id']}/messages",
        json={
            "body": "sent first, claims to be from the future",
            "client_message_id": _cmid(),
            "client_created_at": "2099-01-01T00:00:00Z",
        },
    )
    r2 = owner["client"].post(
        f"/groups/{group['id']}/messages",
        json={
            "body": "sent second, claims to be from the past",
            "client_message_id": _cmid(),
            "client_created_at": "2000-01-01T00:00:00Z",
        },
    )
    assert r1.json()["seq"] == 1
    assert r2.json()["seq"] == 2


def test_concurrent_message_creation_preserves_sequence_ordering(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    def send(client, label):
        return client.post(
            f"/groups/{group['id']}/messages",
            json={"body": label, "client_message_id": _cmid()},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(send, owner["client"], f"owner-{i}") for i in range(5)
        ] + [pool.submit(send, other_user["client"], f"other-{i}") for i in range(5)]
        results = [f.result() for f in futures]

    seqs = sorted(r.json()["seq"] for r in results)
    assert seqs == list(range(1, 11))  # gap-free, no duplicates

    with engine.begin() as conn:
        stored_seqs = [
            row[0]
            for row in conn.execute(
                text("SELECT seq FROM messages WHERE group_id = :gid ORDER BY seq"),
                {"gid": group["id"]},
            )
        ]
    assert stored_seqs == list(range(1, 11))


# --- idempotency -------------------------------------------------------


def test_duplicate_client_message_id_is_idempotent(owner, group):
    cmid = _cmid()
    r1 = owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "hello", "client_message_id": cmid}
    )
    r2 = owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "hello (retry)", "client_message_id": cmid}
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["seq"] == r2.json()["seq"]

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM messages WHERE group_id = :gid"), {"gid": group["id"]}
        ).scalar()
    assert count == 1


def test_same_client_message_id_from_different_users_does_not_collide(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    cmid = _cmid()

    r1 = owner["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "from owner", "client_message_id": cmid}
    )
    r2 = other_user["client"].post(
        f"/groups/{group['id']}/messages", json={"body": "from other", "client_message_id": cmid}
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
    assert {r1.json()["seq"], r2.json()["seq"]} == {1, 2}


# --- pagination -------------------------------------------------------


def test_before_seq_pagination(owner, group):
    for i in range(5):
        owner["client"].post(
            f"/groups/{group['id']}/messages",
            json={"body": f"msg {i}", "client_message_id": _cmid()},
        )

    r = owner["client"].get(f"/groups/{group['id']}/messages?limit=2")
    assert r.status_code == 200
    page1 = r.json()
    assert len(page1["messages"]) == 2
    assert [m["seq"] for m in page1["messages"]] == [4, 5]
    assert page1["has_more"] is True
    assert page1["next_before_seq"] == 4

    r2 = owner["client"].get(
        f"/groups/{group['id']}/messages?before_seq={page1['next_before_seq']}&limit=2"
    )
    page2 = r2.json()
    assert [m["seq"] for m in page2["messages"]] == [2, 3]
    assert page2["has_more"] is True
    assert page2["next_before_seq"] == 2

    r3 = owner["client"].get(
        f"/groups/{group['id']}/messages?before_seq={page2['next_before_seq']}&limit=2"
    )
    page3 = r3.json()
    assert [m["seq"] for m in page3["messages"]] == [1]
    assert page3["has_more"] is False
    assert page3["next_before_seq"] is None


def test_pagination_limit_validation(owner, group):
    r = owner["client"].get(f"/groups/{group['id']}/messages?limit=0")
    assert r.status_code == 422

    r2 = owner["client"].get(f"/groups/{group['id']}/messages?limit=201")
    assert r2.status_code == 422

    r3 = owner["client"].get(f"/groups/{group['id']}/messages?before_seq=0")
    assert r3.status_code == 422


def test_empty_message_history(owner, group):
    r = owner["client"].get(f"/groups/{group['id']}/messages")
    assert r.status_code == 200
    assert r.json() == {"messages": [], "has_more": False, "next_before_seq": None}

    r2 = owner["client"].post(f"/groups/{group['id']}/sync", json={"since_seq": 0})
    assert r2.status_code == 200
    body = r2.json()
    assert body["messages"] == []
    assert body["has_more"] is False
    assert body["next_seq"] == 0
    assert body["server_last_seq"] == 0
    assert body["floor_seq"] == 0


# --- group isolation -------------------------------------------------------


def test_group_isolation(owner, group):
    from tests.conftest import insert_group, next_github_repo_id, insert_repository

    repo_id_2 = insert_repository(next_github_repo_id())
    group_id_2 = insert_group(repo_id_2, owner["user_id"], name="Other group")
    try:
        owner["client"].post(
            f"/groups/{group['id']}/messages", json={"body": "in group 1", "client_message_id": _cmid()}
        )
        owner["client"].post(
            f"/groups/{group_id_2}/messages", json={"body": "in group 2", "client_message_id": _cmid()}
        )

        r = owner["client"].get(f"/groups/{group['id']}/messages")
        bodies = [m["body"] for m in r.json()["messages"]]
        assert bodies == ["in group 1"]
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM groups WHERE id = :id"), {"id": group_id_2})
            conn.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": str(repo_id_2)})


# --- sync -------------------------------------------------------


def test_sync_forward_from_cursor(owner, group):
    seqs = []
    for i in range(3):
        r = owner["client"].post(
            f"/groups/{group['id']}/messages",
            json={"body": f"m{i}", "client_message_id": _cmid()},
        )
        seqs.append(r.json()["seq"])

    r = owner["client"].post(f"/groups/{group['id']}/sync", json={"since_seq": 1})
    body = r.json()
    assert [m["seq"] for m in body["messages"]] == [2, 3]
    assert body["next_seq"] == 3
    assert body["has_more"] is False
    assert body["server_last_seq"] == 3
    assert body["floor_seq"] == 1


def test_sync_respects_limit_and_has_more(owner, group):
    for i in range(5):
        owner["client"].post(
            f"/groups/{group['id']}/messages",
            json={"body": f"m{i}", "client_message_id": _cmid()},
        )

    r = owner["client"].post(f"/groups/{group['id']}/sync", json={"since_seq": 0, "limit": 2})
    body = r.json()
    assert [m["seq"] for m in body["messages"]] == [1, 2]
    assert body["has_more"] is True
    assert body["next_seq"] == 2


def test_sync_non_member_rejected(owner, other_user, group):
    r = other_user["client"].post(f"/groups/{group['id']}/sync", json={"since_seq": 0})
    assert r.status_code == 404
