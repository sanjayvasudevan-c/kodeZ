import uuid

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.main import app
from tests.conftest import insert_membership, make_user


def _expire_invitation(invitation_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE group_invitations SET expires_at = now() - interval '1 day' WHERE id = :id"),
            {"id": invitation_id},
        )


# --- who can invite ---------------------------------------------------------


def test_owner_can_invite_existing_user_by_id(owner, other_user, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["invited_user_id"] == other_user["user_id"]
    assert body["role"] == "member"
    assert body["status"] == "pending"
    assert "token" in body and len(body["token"]) > 20
    assert "token_hash" not in body


def test_admin_can_invite_member_but_not_admin(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")

    r = other_user["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": third_user["user_id"], "role": "member"}
    )
    assert r.status_code == 201

    r2 = other_user["client"].post(
        f"/groups/{group['id']}/invitations",
        json={"user_id": third_user["user_id"], "role": "admin"},
    )
    assert r2.status_code == 403


def test_plain_member_cannot_invite(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = other_user["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": third_user["user_id"]}
    )
    assert r.status_code == 403


def test_non_member_cannot_invite(owner, other_user, third_user, group):
    r = other_user["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": third_user["user_id"]}
    )
    assert r.status_code == 404


# --- target resolution -------------------------------------------------------


def test_invite_unknown_email_creates_invitation_without_user_id(owner, group):
    email = f"stranger-{uuid.uuid4().hex[:10]}@example.com"
    r = owner["client"].post(f"/groups/{group['id']}/invitations", json={"email": email})
    assert r.status_code == 201
    body = r.json()
    assert body["invited_user_id"] is None
    assert body["invited_email"] == email.lower()


def test_invite_existing_user_by_email_resolves_user_id(owner, other_user, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"email": other_user["email"].upper()}
    )
    assert r.status_code == 201
    assert r.json()["invited_user_id"] == other_user["user_id"]


def test_cannot_invite_existing_member(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    assert r.status_code == 409


def test_request_requires_exactly_one_target(owner, other_user, group):
    r = owner["client"].post(f"/groups/{group['id']}/invitations", json={})
    assert r.status_code == 422

    r2 = owner["client"].post(
        f"/groups/{group['id']}/invitations",
        json={"user_id": other_user["user_id"], "email": "x@example.com"},
    )
    assert r2.status_code == 422


# --- duplicate / expiry -------------------------------------------------------


def test_duplicate_pending_invitation_rejected(owner, other_user, group):
    r1 = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    assert r1.status_code == 201

    r2 = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    assert r2.status_code == 409


def test_expired_pending_invitation_can_be_reinvited(owner, other_user, group):
    r1 = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    _expire_invitation(r1.json()["id"])

    r2 = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    assert r2.status_code == 201
    assert r2.json()["id"] != r1.json()["id"]

    with engine.begin() as conn:
        cancelled = conn.execute(
            text("SELECT status FROM group_invitations WHERE id = :id"), {"id": r1.json()["id"]}
        ).scalar()
    assert cancelled == "cancelled"


# --- accept / reject -----------------------------------------------------------


def test_accept_invitation_creates_membership(owner, other_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()

    r = other_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == other_user["user_id"]
    assert body["role"] == "member"

    r2 = other_user["client"].get(f"/groups/{group['id']}")
    assert r2.status_code == 200
    assert r2.json()["my_role"] == "member"


def test_accept_requires_correct_recipient(owner, other_user, third_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()

    r = third_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r.status_code == 404

    with engine.begin() as conn:
        member_count = conn.execute(
            text("SELECT count(*) FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group["id"], "uid": third_user["user_id"]},
        ).scalar()
    assert member_count == 0


def test_accept_invitation_is_single_use(owner, other_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()

    r1 = other_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r1.status_code == 200

    r2 = other_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r2.status_code == 404


def test_accept_expired_invitation_rejected(owner, other_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()
    _expire_invitation(invite["id"])

    r = other_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r.status_code == 404


def test_accept_unknown_token_rejected(owner, other_user, group):
    r = other_user["client"].post("/invitations/accept", json={"token": "not-a-real-token"})
    assert r.status_code == 404


def test_reject_invitation(owner, other_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()

    r = other_user["client"].post("/invitations/reject", json={"token": invite["token"]})
    assert r.status_code == 204

    with engine.begin() as conn:
        status_val = conn.execute(
            text("SELECT status FROM group_invitations WHERE id = :id"), {"id": invite["id"]}
        ).scalar()
    assert status_val == "rejected"

    # a rejected token cannot later be accepted
    r2 = other_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r2.status_code == 404


# --- cancellation -------------------------------------------------------------


def test_owner_can_cancel_invitation(owner, other_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()

    r = owner["client"].delete(f"/groups/{group['id']}/invitations/{invite['id']}")
    assert r.status_code == 204

    r2 = other_user["client"].post("/invitations/accept", json={"token": invite["token"]})
    assert r2.status_code == 404


def test_cancel_already_resolved_invitation_is_404(owner, other_user, group):
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    ).json()
    other_user["client"].post("/invitations/reject", json={"token": invite["token"]})

    r = owner["client"].delete(f"/groups/{group['id']}/invitations/{invite['id']}")
    assert r.status_code == 404


def test_member_cannot_cancel_invitation(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": third_user["user_id"]}
    ).json()

    r = other_user["client"].delete(f"/groups/{group['id']}/invitations/{invite['id']}")
    assert r.status_code == 403


# --- listing -------------------------------------------------------------


def test_list_group_invitations_requires_admin_or_owner(owner, other_user, group):
    owner["client"].post(f"/groups/{group['id']}/invitations", json={"email": "a@example.com"})
    insert_membership(group["id"], other_user["user_id"], "member")

    r = other_user["client"].get(f"/groups/{group['id']}/invitations")
    assert r.status_code == 403

    r2 = owner["client"].get(f"/groups/{group['id']}/invitations")
    assert r2.status_code == 200
    assert len(r2.json()) == 1


def test_list_my_invitations_by_user_id_and_email(owner, other_user, group):
    owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )

    r = other_user["client"].get("/invitations")
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["invited_user_id"] == other_user["user_id"]


def test_list_my_invitations_requires_auth():
    anon = TestClient(app, base_url="https://testserver")
    r = anon.get("/invitations")
    assert r.status_code == 401


# --- new-user (email-only) flow -----------------------------------------------


def test_new_user_invitation_then_register_then_accept(owner, group):
    email = f"newcomer-{uuid.uuid4().hex[:10]}@example.com"
    invite = owner["client"].post(f"/groups/{group['id']}/invitations", json={"email": email}).json()
    assert invite["invited_user_id"] is None

    # the invitee doesn't have an account yet at invite time
    client, user_id = make_user(email)

    # now visible via GET /invitations by email match
    pending = client.get("/invitations").json()
    assert any(i["id"] == invite["id"] for i in pending)

    r = client.post("/invitations/accept", json={"token": invite["token"]})
    assert r.status_code == 200
    assert r.json()["user_id"] == user_id

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE email = :email"), {"email": email})


# --- GitHub independence -------------------------------------------------------


def test_invite_and_accept_do_not_require_github_account(owner, other_user, group):
    # neither owner nor other_user has a github_access_token set anywhere in this test
    invite = owner["client"].post(
        f"/groups/{group['id']}/invitations", json={"user_id": other_user["user_id"]}
    )
    assert invite.status_code == 201

    r = other_user["client"].post("/invitations/accept", json={"token": invite.json()["token"]})
    assert r.status_code == 200
