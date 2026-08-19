"""Phase 6A: message retention is job-ready but intentionally inert.

PostgreSQL is a durable RELAY BUFFER for chat messages, not a permanent archive.
A message becomes eligible for deletion only when it satisfies BOTH:

  1. it is older than the configured relay retention window, AND
  2. it has been acknowledged by every relevant active device.

Condition (2) requires per-device delivery cursors (`device_cursors`), which do
not exist until Phase 6B. Deleting on age alone would violate the relay-buffer
guarantee: a client that never synced a message would lose it permanently, with
PostgreSQL supposedly acting as the safety net that let it happen.

This module therefore computes retention *candidates* by age — useful for
visibility, monitoring, and testing — but performs no deletion. Phase 6B adds
the acknowledgement check here and only then authorizes an actual DELETE.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.message import Message

DEFAULT_RETENTION_WINDOW = timedelta(days=30)


def find_retention_candidates(
    db: Session, retention_window: timedelta = DEFAULT_RETENTION_WINDOW
) -> list[Message]:
    """Messages old enough to be *considered* for deletion, by age alone.

    NOT sufficient on its own to authorize deletion — see module docstring.
    """
    cutoff = datetime.now(timezone.utc) - retention_window
    return (
        db.query(Message)
        .filter(Message.created_at < cutoff)
        .order_by(Message.group_id, Message.seq)
        .all()
    )


def run_message_retention(db: Session, retention_window: timedelta = DEFAULT_RETENTION_WINDOW) -> int:
    """Job-ready entrypoint for a scheduled retention sweep.

    Phase 6A: always returns 0 and deletes nothing. There is no safe way yet to
    confirm a message has been delivered to every active device without
    device_cursors. Phase 6B adds that check here before any row is deleted.
    """
    find_retention_candidates(db, retention_window)
    return 0
