import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.retention import compute_delivery_floor, run_message_retention
from app.database import SessionLocal, engine
from app.main import app
from tests.conftest import insert_membership


def _register_device(client) -> str:
    device_id = str(uuid.uuid4())
    r = client.post("/devices", json={"device_id": device_id})
    assert r.status_code in (200, 201)
    return device_id


def _send(client, group_id, body="hi") -> dict:
    r = client.post(
        f"/groups/{group_id}/messages", json={"body": body, "client_message_id": str(uuid.uuid4())}
    )
    assert r.status_code == 201
    return r.json()


def _age_messages(group_id, days: int, max_seq: int | None = None) -> None:
    with engine.begin() as conn:
        sql = "UPDATE messages SET created_at = now() - make_interval(days => :days) WHERE group_id = :gid"
        params = {"days": days, "gid": str(group_id)}
        if max_seq is not None:
            sql += " AND seq <= :max_seq"
            params["max_seq"] = max_seq
        conn.execute(text(sql), params)


def _age_cursor(device_id, group_id, days: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE device_cursors SET last_seen_at = now() - make_interval(days => :days) "
                "WHERE device_id = :did AND group_id = :gid"
            ),
            {"days": days, "did": device_id, "gid": str(group_id)},
        )


def _cursor_row(device_id, group_id):
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT last_acked_seq FROM device_cursors WHERE device_id = :did AND group_id = :gid"
            ),
            {"did": device_id, "gid": str(group_id)},
        ).first()


# ============================================================
# Device registration
# ============================================================


def test_device_registration_requires_auth():
    anon = TestClient(app, base_url="https://testserver")
    r = anon.post("/devices", json={"device_id": str(uuid.uuid4())})
    assert r.status_code == 401


def test_device_registration_is_idempotent(owner):
    device_id = str(uuid.uuid4())
    r1 = owner["client"].post("/devices", json={"device_id": device_id})
    r2 = owner["client"].post("/devices", json={"device_id": device_id})
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"] == device_id
    # last_seen_at must have moved forward on re-registration
    assert r2.json()["last_seen_at"] >= r1.json()["last_seen_at"]

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM devices WHERE id = :id"), {"id": device_id}
        ).scalar()
    assert count == 1


def test_device_hijack_attempt_rejected(owner, other_user):
    device_id = str(uuid.uuid4())
    r1 = owner["client"].post("/devices", json={"device_id": device_id})
    assert r1.status_code == 201

    r2 = other_user["client"].post("/devices", json={"device_id": device_id})
    assert r2.status_code == 409

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT user_id FROM devices WHERE id = :id"), {"id": device_id}
        ).first()
    assert row[0] == uuid.UUID(owner["user_id"])  # still belongs to the original owner


# ============================================================
# Sync: device/membership validation
# ============================================================


def test_sync_unknown_device_rejected(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/sync", json={"device_id": str(uuid.uuid4()), "since_seq": 0}
    )
    assert r.status_code == 404


def test_sync_device_belonging_to_another_user_rejected(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    device_id = _register_device(owner["client"])

    r = other_user["client"].post(
        f"/groups/{group['id']}/sync", json={"device_id": device_id, "since_seq": 0}
    )
    assert r.status_code == 404


# ============================================================
# ACK
# ============================================================


def test_ack_requires_own_device(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    owner_device = _register_device(owner["client"])

    r = other_user["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": owner_device, "acked_seq": 1}
    )
    assert r.status_code == 404


def test_ack_unknown_device_rejected(owner, group):
    r = owner["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": str(uuid.uuid4()), "acked_seq": 1}
    )
    assert r.status_code == 404


def test_ack_non_member_rejected(owner, other_user, group):
    device_id = _register_device(other_user["client"])
    r = other_user["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 1}
    )
    assert r.status_code == 404


def test_ack_creates_cursor_if_missing(owner, group):
    _send(owner["client"], group["id"])
    device_id = _register_device(owner["client"])

    r = owner["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 1}
    )
    assert r.status_code == 200
    assert r.json()["last_acked_seq"] == 1


def test_ack_beyond_server_last_seq_is_clamped(owner, group):
    _send(owner["client"], group["id"])  # seq=1
    device_id = _register_device(owner["client"])

    r = owner["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 999999999}
    )
    assert r.status_code == 200
    assert r.json()["last_acked_seq"] == 1  # clamped to server_last_seq, not the claimed value


def test_ack_cannot_move_backwards(owner, group):
    for _ in range(5):
        _send(owner["client"], group["id"])
    device_id = _register_device(owner["client"])

    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 4})
    r = owner["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 2}
    )
    assert r.status_code == 200
    assert r.json()["last_acked_seq"] == 4  # stale, lower ack is a no-op


def test_ack_duplicate_is_harmless(owner, group):
    _send(owner["client"], group["id"])
    device_id = _register_device(owner["client"])

    r1 = owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 1})
    r2 = owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 1})
    assert r1.json()["last_acked_seq"] == r2.json()["last_acked_seq"] == 1


def test_server_does_not_verify_cursor_contiguity_by_design(owner, group):
    """The server cannot see the client's local SQLite state. If a client sends
    12, then skips straight to acking 14 (even though it never actually
    persisted message 13), the server has no way to detect this — it isn't a
    per-message receipt system. This is exactly why retention gates deletion on
    the MINIMUM cursor across ALL active devices, not any single device's claim:
    a device that lies about its own contiguity only endangers its own future
    recovery, never another device's.
    """
    for _ in range(3):
        _send(owner["client"], group["id"])  # seq 1, 2, 3
    device_id = _register_device(owner["client"])

    r = owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 3})
    assert r.status_code == 200
    assert r.json()["last_acked_seq"] == 3  # accepted at face value — contiguity is a client duty


# ============================================================
# Read state
# ============================================================


def test_read_state_requires_member(owner, other_user, group):
    r = other_user["client"].post(f"/groups/{group['id']}/read", json={"last_read_seq": 1})
    assert r.status_code == 404


def test_get_read_state_default_zero(owner, group):
    r = owner["client"].get(f"/groups/{group['id']}/read")
    assert r.status_code == 200
    assert r.json()["last_read_seq"] == 0


def test_read_state_is_monotonic(owner, group):
    for _ in range(5):
        _send(owner["client"], group["id"])

    owner["client"].post(f"/groups/{group['id']}/read", json={"last_read_seq": 4})
    r = owner["client"].post(f"/groups/{group['id']}/read", json={"last_read_seq": 2})
    assert r.json()["last_read_seq"] == 4  # cannot move backwards


def test_read_state_clamped_to_server_last_seq(owner, group):
    for _ in range(3):
        _send(owner["client"], group["id"])  # last_message_seq = 3

    r = owner["client"].post(f"/groups/{group['id']}/read", json={"last_read_seq": 999})
    assert r.status_code == 200
    assert r.json()["last_read_seq"] == 3  # clamped, not 999


def test_read_state_multi_device_merge(owner, group):
    """Read state is (user, group) — not (device, group). Device A reading
    further than device B still produces one merged, monotonic value."""
    for _ in range(5):
        _send(owner["client"], group["id"])

    # simulate device A
    owner["client"].post(f"/groups/{group['id']}/read", json={"last_read_seq": 4})
    # simulate device B reconnecting with a lower position
    r = owner["client"].post(f"/groups/{group['id']}/read", json={"last_read_seq": 1})
    assert r.json()["last_read_seq"] == 4


# ============================================================
# New device / gap detection
# ============================================================


def test_no_gap_when_nothing_deleted(owner, group):
    for _ in range(3):
        _send(owner["client"], group["id"])
    device_id = _register_device(owner["client"])

    r = owner["client"].post(
        f"/groups/{group['id']}/sync", json={"device_id": device_id, "since_seq": 0}
    )
    body = r.json()
    assert body["floor_seq"] == 1
    assert body["gap_detected"] is False
    assert [m["seq"] for m in body["messages"]] == [1, 2, 3]


def test_new_device_after_retention_sees_explicit_gap(owner, group):
    """floor_seq=500 style scenario from the design doc, at small scale:
    messages 1-3 get relay-retention-deleted (simulated directly), a brand new
    device with since_seq=0 must see gap_detected=True and get whatever
    remains, never a silent partial history."""
    for _ in range(5):
        _send(owner["client"], group["id"])  # seq 1..5

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM messages WHERE group_id = :gid AND seq <= 3"), {"gid": group["id"]}
        )

    new_device = _register_device(owner["client"])
    r = owner["client"].post(
        f"/groups/{group['id']}/sync", json={"device_id": new_device, "since_seq": 0}
    )
    body = r.json()
    assert body["floor_seq"] == 4
    assert body["gap_detected"] is True
    assert [m["seq"] for m in body["messages"]] == [4, 5]


# ============================================================
# Multiple devices
# ============================================================


def test_multiple_devices_track_independent_cursors(owner, group):
    for _ in range(5):
        _send(owner["client"], group["id"])

    device_a = _register_device(owner["client"])
    device_b = _register_device(owner["client"])

    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_a, "acked_seq": 5})
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_b, "acked_seq": 2})

    row_a = _cursor_row(device_a, group["id"])
    row_b = _cursor_row(device_b, group["id"])
    assert row_a[0] == 5
    assert row_b[0] == 2  # independent — acking on A never touches B


# ============================================================
# Membership removal
# ============================================================


def test_removed_member_cursor_is_deleted(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    device_id = _register_device(other_user["client"])
    other_user["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 0}
    )
    assert _cursor_row(device_id, group["id"]) is not None

    r = owner["client"].delete(f"/groups/{group['id']}/members/{other_user['user_id']}")
    assert r.status_code == 204

    assert _cursor_row(device_id, group["id"]) is None  # cascaded away, no app code needed


def test_removed_member_leave_also_cascades_cursor(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    device_id = _register_device(other_user["client"])
    other_user["client"].post(
        f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 0}
    )

    r = other_user["client"].post(f"/groups/{group['id']}/leave")
    assert r.status_code == 204
    assert _cursor_row(device_id, group["id"]) is None


# ============================================================
# Retention: mathematical worked examples
# ============================================================


def test_retention_no_deletion_without_active_devices(owner, group):
    for _ in range(3):
        _send(owner["client"], group["id"])
    _age_messages(group["id"], days=40)  # older than the 30-day window

    with SessionLocal() as db:
        deleted = run_message_retention(db)
    assert deleted == 0

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM messages WHERE group_id = :gid"), {"gid": group["id"]}
        ).scalar()
    assert count == 3  # nothing deleted: no device has ever confirmed receipt


def test_retention_respects_age_even_if_fully_acked(owner, group):
    for _ in range(3):
        _send(owner["client"], group["id"])
    device_id = _register_device(owner["client"])
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 3})
    # messages are recent (not aged) — must NOT be deleted despite full ack

    with SessionLocal() as db:
        deleted = run_message_retention(db)
    assert deleted == 0

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM messages WHERE group_id = :gid"), {"gid": group["id"]}
        ).scalar()
    assert count == 3


def test_retention_deletes_when_old_and_fully_acked(owner, group):
    for _ in range(3):
        _send(owner["client"], group["id"])
    device_id = _register_device(owner["client"])
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 3})
    _age_messages(group["id"], days=40)

    with SessionLocal() as db:
        deleted = run_message_retention(db)
    assert deleted == 3

    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM messages WHERE group_id = :gid"), {"gid": group["id"]}
        ).scalar()
    assert count == 0


def test_retention_worked_example_three_devices(owner, other_user, third_user, group):
    """From the design doc: A=500, B=450, C=300 (scaled down to 10 messages).
    A=10, B=8, C=5. seq<=5 deletable (bounded by slowest active device C).
    seq=6..10 must survive, including seq=8 which B already has — C still
    hasn't, and C is what gates group-wide deletion."""
    insert_membership(group["id"], other_user["user_id"], "member")
    insert_membership(group["id"], third_user["user_id"], "member")

    for i in range(10):
        _send(owner["client"], group["id"], body=f"m{i}")  # seq 1..10
    _age_messages(group["id"], days=40)

    device_a = _register_device(owner["client"])
    device_b = _register_device(other_user["client"])
    device_c = _register_device(third_user["client"])

    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_a, "acked_seq": 10})
    other_user["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_b, "acked_seq": 8})
    third_user["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_c, "acked_seq": 5})

    with SessionLocal() as db:
        floor = compute_delivery_floor(db, group["id"])
        assert floor == 5  # MIN(10, 8, 5)
        deleted = run_message_retention(db)
    assert deleted == 5

    with engine.begin() as conn:
        remaining = [
            row[0]
            for row in conn.execute(
                text("SELECT seq FROM messages WHERE group_id = :gid ORDER BY seq"),
                {"gid": group["id"]},
            )
        ]
    assert remaining == [6, 7, 8, 9, 10]


def test_retention_ignores_inactive_devices(owner, other_user, group):
    """Device B is active at cursor=2 (would block deletion past seq 2), but
    B hasn't been seen in 91 days — beyond the 90-day active window — so it
    stops counting. Retention then proceeds based on A alone."""
    insert_membership(group["id"], other_user["user_id"], "member")

    for i in range(5):
        _send(owner["client"], group["id"], body=f"m{i}")
    _age_messages(group["id"], days=40)

    device_a = _register_device(owner["client"])
    device_b = _register_device(other_user["client"])
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_a, "acked_seq": 5})
    other_user["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_b, "acked_seq": 2})

    # B goes inactive
    _age_cursor(device_b, group["id"], days=91)

    with SessionLocal() as db:
        floor = compute_delivery_floor(db, group["id"])
        assert floor == 5  # B excluded; only A (active) counts
        deleted = run_message_retention(db)
    assert deleted == 5


def test_retention_active_inactive_boundary_still_blocks(owner, other_user, group):
    """Same setup, but B is only 89 days silent — still within the 90-day
    active window — so B's low cursor (2) still gates deletion."""
    insert_membership(group["id"], other_user["user_id"], "member")

    for i in range(5):
        _send(owner["client"], group["id"], body=f"m{i}")
    _age_messages(group["id"], days=40)

    device_a = _register_device(owner["client"])
    device_b = _register_device(other_user["client"])
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_a, "acked_seq": 5})
    other_user["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_b, "acked_seq": 2})
    _age_cursor(device_b, group["id"], days=89)

    with SessionLocal() as db:
        floor = compute_delivery_floor(db, group["id"])
        assert floor == 2
        deleted = run_message_retention(db)
    assert deleted == 2  # only seq 1,2 deletable


def test_retention_malicious_cursor_only_endangers_its_own_device(owner, other_user, group):
    """A lies and claims acked_seq=5 (the max) despite never truly persisting
    past seq 1. B is honest at seq=1. The group-wide floor is bounded by B's
    truthful value regardless of A's claim — A's lie cannot cause B's, or
    anyone else's, messages to be deleted prematurely relative to B."""
    insert_membership(group["id"], other_user["user_id"], "member")

    for i in range(5):
        _send(owner["client"], group["id"], body=f"m{i}")
    _age_messages(group["id"], days=40)

    device_a = _register_device(owner["client"])
    device_b = _register_device(other_user["client"])
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_a, "acked_seq": 5})  # lie
    other_user["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_b, "acked_seq": 1})  # truth

    with SessionLocal() as db:
        floor = compute_delivery_floor(db, group["id"])
        assert floor == 1  # B's honest value bounds it, not A's inflated claim
        deleted = run_message_retention(db)
    assert deleted == 1


# ============================================================
# Database constraints (direct SQL, bypassing the API)
# ============================================================


def test_db_rejects_negative_acked_seq(owner, group):
    device_id = _register_device(owner["client"])
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO device_cursors "
                    "(device_id, group_id, user_id, last_acked_seq, last_seen_at, created_at, updated_at) "
                    "VALUES (:did, :gid, :uid, -1, now(), now(), now())"
                ),
                {"did": device_id, "gid": group["id"], "uid": owner["user_id"]},
            )


def test_db_rejects_cursor_for_device_belonging_to_different_user(owner, other_user, group):
    insert_membership(group["id"], other_user["user_id"], "member")
    device_id = _register_device(owner["client"])  # belongs to owner, not other_user

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO device_cursors "
                    "(device_id, group_id, user_id, last_acked_seq, last_seen_at, created_at, updated_at) "
                    "VALUES (:did, :gid, :uid, 0, now(), now(), now())"
                ),
                {"did": device_id, "gid": group["id"], "uid": other_user["user_id"]},
            )


def test_db_rejects_cursor_for_non_member(owner, other_user, group):
    # other_user is deliberately NOT added to group_members
    device_id = _register_device(other_user["client"])

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO device_cursors "
                    "(device_id, group_id, user_id, last_acked_seq, last_seen_at, created_at, updated_at) "
                    "VALUES (:did, :gid, :uid, 0, now(), now(), now())"
                ),
                {"did": device_id, "gid": group["id"], "uid": other_user["user_id"]},
            )


def test_db_rejects_duplicate_cursor_per_device_group(owner, group):
    device_id = _register_device(owner["client"])
    owner["client"].post(f"/groups/{group['id']}/ack", json={"device_id": device_id, "acked_seq": 0})

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO device_cursors "
                    "(device_id, group_id, user_id, last_acked_seq, last_seen_at, created_at, updated_at) "
                    "VALUES (:did, :gid, :uid, 0, now(), now(), now())"
                ),
                {"did": device_id, "gid": group["id"], "uid": owner["user_id"]},
            )
