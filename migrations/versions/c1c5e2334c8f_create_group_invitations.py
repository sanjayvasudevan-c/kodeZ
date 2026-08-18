"""create group_invitations table

Revision ID: c1c5e2334c8f
Revises: b753b5cbc272
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c1c5e2334c8f"
down_revision: Union[str, Sequence[str], None] = "b753b5cbc272"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "group_invitations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "inviter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "invited_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("invited_email", sa.String(length=320), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="member"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "invited_user_id IS NOT NULL OR invited_email IS NOT NULL",
            name="ck_group_invitations_has_target",
        ),
        sa.CheckConstraint(
            "role IN ('member', 'admin')", name="ck_group_invitations_role"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'cancelled')",
            name="ck_group_invitations_status",
        ),
    )

    op.create_index("ix_group_invitations_group_id", "group_invitations", ["group_id"])
    op.create_index(
        "ix_group_invitations_invited_user_id", "group_invitations", ["invited_user_id"]
    )
    op.create_index("ix_group_invitations_invited_email", "group_invitations", ["invited_email"])
    op.create_index(
        "ux_group_invitations_token_hash", "group_invitations", ["token_hash"], unique=True
    )

    # At most one PENDING invitation per (group, target) at a time — enforced at the DB
    # level. Expiry can't be part of the predicate (now() isn't immutable), so the app
    # cancels stale pending rows before inserting a fresh one past expiry.
    op.create_index(
        "ux_group_invitations_pending_user",
        "group_invitations",
        ["group_id", "invited_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND invited_user_id IS NOT NULL"),
    )
    op.create_index(
        "ux_group_invitations_pending_email",
        "group_invitations",
        ["group_id", "invited_email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending' AND invited_email IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("group_invitations")
