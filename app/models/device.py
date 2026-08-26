import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Device(Base):
    __tablename__ = "devices"

    # Client-generated and persisted locally. NOT an authentication credential —
    # the authenticated session always determines identity. This id is only a
    # stable key the client uses to name its own local sync/outbox state across
    # reconnects and app restarts.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    __table_args__ = (
        # Referenced by device_cursors' composite FK so a cursor can prove its
        # device_id truly belongs to its user_id — a fact a single-column FK
        # (which only proves device_id exists, for *some* user) cannot express.
        UniqueConstraint("id", "user_id", name="ux_devices_id_user_id"),
    )
