import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CreateMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    client_message_id: uuid.UUID
    client_created_at: datetime | None = None


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
    since_seq: int = Field(ge=0)
    limit: int = Field(default=200, ge=1, le=500)


class SyncResponse(BaseModel):
    messages: list[MessagePublic]
    next_seq: int
    has_more: bool
    server_last_seq: int
    floor_seq: int
