import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeviceCursor(Base):
    """One row per (device, group): 'this device has safely persisted all
    messages through seq last_acked_seq' — never a per-message receipt."""

    __tablename__ = "device_cursors"

    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    group_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    last_acked_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # Per-(device,group) liveness signal, updated on sync/ack for *this* group —
    # deliberately distinct from devices.last_seen_at (global), since a device
    # active in one group says nothing about whether it still cares about another.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("last_acked_seq >= 0", name="ck_device_cursors_non_negative"),
        # Proves device_id truly belongs to user_id (not merely that both exist).
        ForeignKeyConstraint(
            ["device_id", "user_id"],
            ["devices.id", "devices.user_id"],
            ondelete="CASCADE",
            name="fk_device_cursors_device_user",
        ),
        # Proves user_id is a CURRENT member of group_id. When Phase 5's
        # remove-member/leave endpoints delete the group_members row, this
        # cascades automatically — a removed member's cursor stops existing,
        # and stops participating in retention, with no Phase 5 code changes.
        ForeignKeyConstraint(
            ["group_id", "user_id"],
            ["group_members.group_id", "group_members.user_id"],
            ondelete="CASCADE",
            name="fk_device_cursors_group_member",
        ),
        # The PK (device_id, group_id) already indexes "by device_id" for free
        # (leading column). Retention filters by group_id + last_seen_at, which
        # the PK index can't serve efficiently (group_id isn't the leading
        # column) — this is the index that actually makes the retention sweep
        # an index scan instead of a full table scan.
        Index("ix_device_cursors_group_last_seen", "group_id", "last_seen_at"),
    )
