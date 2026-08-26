import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    client_message_id: uuid.UUID
    client_created_at: datetime | None = None
    # Optional: if the sender includes its device_id, that device's per-group
    # liveness (device_cursors.last_seen_at) is touched. Not required — Phase 6A
    # callers with no device concept yet keep working unchanged.
    device_id: uuid.UUID | None = None


class MessagePublic(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    sender_id: uuid.UUID | None
    seq: int
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageListResponse(BaseModel):
    messages: list[MessagePublic]
    has_more: bool
    next_before_seq: int | None


class SyncRequest(BaseModel):
    device_id: uuid.UUID
    since_seq: int = Field(ge=0)
    limit: int = Field(default=200, ge=1, le=500)


class SyncResponse(BaseModel):
    messages: list[MessagePublic]
    next_seq: int
    has_more: bool
    server_last_seq: int
    floor_seq: int
    # True when since_seq falls before what the server can still guarantee —
    # i.e. messages between since_seq+1 and floor_seq-1 are permanently gone.
    # The client must show this as an explicit gap, never infer it silently.
    gap_detected: bool
