from sqlalchemy import text

from app.database import engine
from tests.conftest import insert_membership


# --- removal -------------------------------------------------------------


def test_admin_can_remove_member(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")
    insert_membership(group["id"], third_user["user_id"], "member")

    r = other_user["client"].delete(f"/groups/{group['id']}/members/{third_user['user_id']}")
    assert r.status_code == 204

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group["id"], "uid": third_user["user_id"]},
        ).scalar()
    assert count == 0


def test_admin_cannot_remove_another_admin(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")
    insert_membership(group["id"], third_user["user_id"], "admin")

    r = other_user["client"].delete(f"/groups/{group['id']}/members/{third_user['user_id']}")
    assert r.status_code == 403


def test_member_cannot_remove_anyone(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    insert_membership(group["id"], third_user["user_id"], "member")

    r = other_user["client"].delete(f"/groups/{group['id']}/members/{third_user['user_id']}")
    assert r.status_code == 403


def test_owner_can_remove_admin(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")

    r = owner["client"].delete(f"/groups/{group['id']}/members/{other_user['user_id']}")
    assert r.status_code == 204


def test_owner_cannot_be_removed(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")

    r = other_user["client"].delete(f"/groups/{group['id']}/members/{owner['user_id']}")
    assert r.status_code == 403

    # even the owner acting on themself is blocked (rank is not strictly greater)
    r2 = owner["client"].delete(f"/groups/{group['id']}/members/{owner['user_id']}")
    assert r2.status_code == 403


def test_remove_nonmember_is_404(owner, other_user, group):
    r = owner["client"].delete(f"/groups/{group['id']}/members/{other_user['user_id']}")
    assert r.status_code == 404


# --- role changes -------------------------------------------------------------


def test_owner_can_promote_member_to_admin(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = owner["client"].patch(
        f"/groups/{group['id']}/members/{other_user['user_id']}/role", json={"role": "admin"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_owner_can_demote_admin_to_member(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")

    r = owner["client"].patch(
        f"/groups/{group['id']}/members/{other_user['user_id']}/role", json={"role": "member"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "member"


def test_admin_cannot_change_roles(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")
    insert_membership(group["id"], third_user["user_id"], "member")

    r = other_user["client"].patch(
        f"/groups/{group['id']}/members/{third_user['user_id']}/role", json={"role": "admin"}
    )
    assert r.status_code == 403


def test_member_cannot_change_roles(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = other_user["client"].patch(
        f"/groups/{group['id']}/members/{other_user['user_id']}/role", json={"role": "admin"}
    )
    assert r.status_code == 403


def test_cannot_change_own_owner_role_via_role_endpoint(owner, group):
    r = owner["client"].patch(
        f"/groups/{group['id']}/members/{owner['user_id']}/role", json={"role": "admin"}
    )
    assert r.status_code == 400


def test_role_change_rejects_owner_value(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = owner["client"].patch(
        f"/groups/{group['id']}/members/{other_user['user_id']}/role", json={"role": "owner"}
    )
    assert r.status_code == 422


def test_role_change_nonmember_is_404(owner, other_user, group):
    r = owner["client"].patch(
        f"/groups/{group['id']}/members/{other_user['user_id']}/role", json={"role": "admin"}
    )
    assert r.status_code == 404


# --- leave (Phase 4 behavior, sanity check it's untouched) --------------------


def test_member_can_still_leave(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    r = other_user["client"].post(f"/groups/{group['id']}/leave")
    assert r.status_code == 204


def test_owner_still_cannot_leave(owner, group):
    r = owner["client"].post(f"/groups/{group['id']}/leave")
    assert r.status_code == 409


# --- ownership transfer -------------------------------------------------------


def test_owner_can_transfer_ownership(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership",
        json={"new_owner_id": other_user["user_id"], "demote_to": "admin"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["owner_id"] == other_user["user_id"]
    assert body["my_role"] == "admin"  # caller (old owner) is now admin

    with engine.begin() as conn:
        old_owner_role = conn.execute(
            text("SELECT role FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group["id"], "uid": owner["user_id"]},
        ).scalar()
        new_owner_role = conn.execute(
            text("SELECT role FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group["id"], "uid": other_user["user_id"]},
        ).scalar()
        group_owner_id = conn.execute(
            text("SELECT owner_id FROM groups WHERE id = :gid"), {"gid": group["id"]}
        ).scalar()

    assert old_owner_role == "admin"
    assert new_owner_role == "owner"
    assert str(group_owner_id) == other_user["user_id"]


def test_transfer_ownership_default_demotion_is_admin(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership", json={"new_owner_id": other_user["user_id"]}
    )
    assert r.status_code == 200

    with engine.begin() as conn:
        old_owner_role = conn.execute(
            text("SELECT role FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group["id"], "uid": owner["user_id"]},
        ).scalar()
    assert old_owner_role == "admin"


def test_transfer_ownership_to_member_demotes_to_member(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")

    r = owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership",
        json={"new_owner_id": other_user["user_id"], "demote_to": "member"},
    )
    assert r.status_code == 200

    with engine.begin() as conn:
        old_owner_role = conn.execute(
            text("SELECT role FROM group_members WHERE group_id = :gid AND user_id = :uid"),
            {"gid": group["id"], "uid": owner["user_id"]},
        ).scalar()
    assert old_owner_role == "member"


def test_ownership_invariant_holds_after_transfer(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership", json={"new_owner_id": other_user["user_id"]}
    )

    with engine.begin() as conn:
        owner_rows = conn.execute(
            text("SELECT user_id FROM group_members WHERE group_id = :gid AND role = 'owner'"),
            {"gid": group["id"]},
        ).fetchall()
    assert len(owner_rows) == 1
    assert str(owner_rows[0][0]) == other_user["user_id"]


def test_admin_cannot_transfer_ownership(owner, other_user, third_user, group):
    insert_membership(group["id"], other_user["user_id"], "admin")
    insert_membership(group["id"], third_user["user_id"], "member")

    r = other_user["client"].post(
        f"/groups/{group['id']}/transfer-ownership", json={"new_owner_id": third_user["user_id"]}
    )
    assert r.status_code == 403


def test_cannot_transfer_ownership_to_non_member(owner, other_user, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership", json={"new_owner_id": other_user["user_id"]}
    )
    assert r.status_code == 404


def test_cannot_transfer_ownership_to_self(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership", json={"new_owner_id": owner["user_id"]}
    )
    assert r.status_code == 400


def test_new_owner_gains_owner_privileges_after_transfer(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    owner["client"].post(
        f"/groups/{group['id']}/transfer-ownership", json={"new_owner_id": other_user["user_id"]}
    )

    # new owner can now delete the group; old owner (now admin) cannot
    r = owner["client"].delete(f"/groups/{group['id']}")
    assert r.status_code == 403

    r2 = other_user["client"].delete(f"/groups/{group['id']}")
    assert r2.status_code == 204
