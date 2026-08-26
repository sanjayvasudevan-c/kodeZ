"""Message retention: PostgreSQL is a durable RELAY BUFFER, not a permanent
chat archive. A message becomes eligible for deletion only when BOTH hold:

  1. it is older than the relay retention window, AND
  2. every currently-ACTIVE device tracking that group has acknowledged it —
     i.e. seq <= MIN(last_acked_seq) over that group's active device_cursors.

Phase 6A could not implement condition (2) at all (no device_cursors existed)
and therefore deleted nothing. Phase 6B adds device_cursors, which makes (2)
computable, so this module now performs real, safe deletions — using the
minimum active cursor as the delivery floor, never per-message ACK rows.

"Active" (per §12/§13 of the Phase 6B design) means the device's cursor for
THIS group was touched (sync/ack/send) within active_device_window. A device
silent longer than that stops counting, so one abandoned laptop can't pin a
group's retention open forever. If a group currently has NO active device
cursors at all, nothing is deleted for it — retention makes no claims about
data no device has ever confirmed receiving. Both windows are configurable
(app.config.settings) because they're policy judgment calls, not physical
constants: 30 days for the relay window is a generous margin over a plausible
"laptop closed for two weeks" absence; 90 days of device inactivity before a
cursor stops counting is 3x that, chosen so a single missed sync cycle can
never look like abandonment.

Security note: a device's last_acked_seq is a CLIENT CLAIM, not something the
server can verify was actually persisted locally. A malicious/buggy device can
therefore falsely advance its own cursor and cause its own future data loss —
but it CANNOT endanger other users' access, because deletion is gated by the
MINIMUM cursor across all active devices. Any other honest, slower device's
true (lower) cursor still bounds what gets deleted, regardless of what one
device claims. See tests/test_devices_and_sync.py::test_retention_* for the
worked examples this docstring describes.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.device_cursor import DeviceCursor
from app.models.message import Message


def _default_retention_window() -> timedelta:
    return timedelta(days=settings.relay_retention_days)


def _default_active_device_window() -> timedelta:
    return timedelta(days=settings.active_device_window_days)


def find_retention_candidates(db: Session, retention_window: timedelta | None = None) -> list[Message]:
    """Messages old enough to be *considered* for deletion, by age alone.

    NOT sufficient to authorize deletion on its own — see module docstring.
    """
    cutoff = datetime.now(timezone.utc) - (retention_window or _default_retention_window())
    return (
        db.query(Message)
        .filter(Message.created_at < cutoff)
        .order_by(Message.group_id, Message.seq)
        .all()
    )


def compute_delivery_floor(
    db: Session, group_id, active_device_window: timedelta | None = None
) -> int | None:
    """Minimum last_acked_seq among GROUP_ID's currently-active device cursors.

    None means no active device cursor exists for this group at all — nothing
    is safe to delete, regardless of message age.
    """
    active_cutoff = datetime.now(timezone.utc) - (
        active_device_window or _default_active_device_window()
    )
    return (
        db.query(func.min(DeviceCursor.last_acked_seq))
        .filter(DeviceCursor.group_id == group_id, DeviceCursor.last_seen_at >= active_cutoff)
        .scalar()
    )


def run_message_retention(
    db: Session,
    retention_window: timedelta | None = None,
    active_device_window: timedelta | None = None,
) -> int:
    """Job-ready entrypoint for a scheduled retention sweep. Not wired to a
    scheduler in Phase 6B (no Celery/cron here) — a caller invokes this
    directly. Returns the number of messages deleted.
    """
    retention_window = retention_window or _default_retention_window()
    active_device_window = active_device_window or _default_active_device_window()
    age_cutoff = datetime.now(timezone.utc) - retention_window

    group_ids = [
        row[0]
        for row in db.query(Message.group_id).filter(Message.created_at < age_cutoff).distinct()
    ]

    deleted_count = 0
    for group_id in group_ids:
        delivery_floor = compute_delivery_floor(db, group_id, active_device_window)
        if delivery_floor is None:
            continue  # no active devices tracked for this group — delete nothing

        result = db.execute(
            delete(Message).where(
                Message.group_id == group_id,
                Message.seq <= delivery_floor,
                Message.created_at < age_cutoff,
            )
        )
        deleted_count += result.rowcount

    db.commit()
    return deleted_count
