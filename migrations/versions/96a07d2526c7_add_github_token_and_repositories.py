"""add github access token to users and create repositories table

Revision ID: 96a07d2526c7
Revises: cd57b0eae787
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "96a07d2526c7"
down_revision: Union[str, Sequence[str], None] = "cd57b0eae787"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("github_access_token", sa.String(length=1024), nullable=True))

    op.create_table(
        "repositories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_login", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=510), nullable=False),
        sa.Column("html_url", sa.String(length=500), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=True),
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "imported_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_repositories_github_repo_id", "repositories", ["github_repo_id"], unique=True
    )
    op.create_index("ix_repositories_full_name", "repositories", ["full_name"])
    op.create_index("ix_repositories_imported_by_id", "repositories", ["imported_by_id"])


def downgrade() -> None:
    op.drop_table("repositories")
    op.drop_column("users", "github_access_token")
