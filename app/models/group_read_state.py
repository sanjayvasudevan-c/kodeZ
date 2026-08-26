import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GroupReadState(Base):
    """One row per (user, group) — user-facing 'read through' position.

    Deliberately NOT tied to device_cursors: a user can have received messages
    through seq 200 (device_cursors) while having only read through 150
    (group_read_state). Also deliberately NOT cascaded from group_members like
    device_cursors is — a stale read position for a removed member is inert
    (they can no longer reach the group via the API at all) and, unlike a
    delivery cursor, is harmless and arguably desirable to preserve if they
    later rejoin.
    """

    __tablename__ = "group_read_state"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    last_read_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("last_read_seq >= 0", name="ck_group_read_state_non_negative"),
    )
