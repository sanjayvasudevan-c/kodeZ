"""create messages table and groups.last_message_seq

Revision ID: a9ca27200b20
Revises: c1c5e2334c8f
Create Date: 2026-08-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a9ca27200b20"
down_revision: Union[str, Sequence[str], None] = "c1c5e2334c8f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "groups",
        sa.Column("last_message_seq", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("client_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(body) BETWEEN 1 AND 10000", name="ck_messages_body_length"
        ),
    )

    op.create_index("ux_messages_group_seq", "messages", ["group_id", "seq"], unique=True)
    op.create_index(
        "ux_messages_group_sender_client_id",
        "messages",
        ["group_id", "sender_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )
    op.create_index("ix_messages_sender_id", "messages", ["sender_id"])
    # Primary access pattern: keyset pagination / forward sync within a group.
    op.create_index("ix_messages_group_id_seq_desc", "messages", ["group_id", sa.text("seq DESC")])
    # Retention sweep scans by age across all groups.
    op.create_index("ix_messages_created_at", "messages", ["created_at"])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_column("groups", "last_message_seq")
