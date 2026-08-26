"""create devices, device_cursors, group_read_state

Revision ID: dc76fa2fcfe4
Revises: a9ca27200b20
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "dc76fa2fcfe4"
down_revision: Union[str, Sequence[str], None] = "a9ca27200b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "user_id", name="ux_devices_id_user_id"),
    )
    op.create_index("ix_devices_user_id", "devices", ["user_id"])

    op.create_table(
        "device_cursors",
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_acked_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("device_id", "group_id"),
        sa.CheckConstraint("last_acked_seq >= 0", name="ck_device_cursors_non_negative"),
        # Proves device_id truly belongs to user_id (not merely that both exist
        # somewhere). A plain single-column FK on device_id alone cannot express
        # this — it would only prove the device exists, for *some* user.
        sa.ForeignKeyConstraint(
            ["device_id", "user_id"],
            ["devices.id", "devices.user_id"],
            ondelete="CASCADE",
            name="fk_device_cursors_device_user",
        ),
        # Proves user_id is a CURRENT member of group_id. When membership is
        # removed (Phase 5's existing remove-member/leave endpoints), this
        # cascades automatically — a removed member's cursor stops existing,
        # and stops participating in retention, with zero Phase 5 code changes.
        sa.ForeignKeyConstraint(
            ["group_id", "user_id"],
            ["group_members.group_id", "group_members.user_id"],
            ondelete="CASCADE",
            name="fk_device_cursors_group_member",
        ),
    )
    op.create_index("ix_device_cursors_user_id", "device_cursors", ["user_id"])
    # The PK (device_id, group_id) already indexes "by device_id" via its
    # leading column. Retention filters by group_id + last_seen_at, which the
    # PK index can't serve efficiently — this is the index that makes the
    # retention sweep an index scan instead of a full table scan.
    op.create_index(
        "ix_device_cursors_group_last_seen", "device_cursors", ["group_id", "last_seen_at"]
    )

    op.create_table(
        "group_read_state",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_read_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        sa.CheckConstraint("last_read_seq >= 0", name="ck_group_read_state_non_negative"),
    )


def downgrade() -> None:
    op.drop_table("group_read_state")
    op.drop_table("device_cursors")
    op.drop_table("devices")
