import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AckRequest(BaseModel):
    device_id: uuid.UUID
    acked_seq: int = Field(ge=0)


class DeviceCursorPublic(BaseModel):
    device_id: uuid.UUID
    group_id: uuid.UUID
    last_acked_seq: int
    last_seen_at: datetime

    model_config = {"from_attributes": True}


class UpdateReadStateRequest(BaseModel):
    last_read_seq: int = Field(ge=0)


class ReadStatePublic(BaseModel):
    group_id: uuid.UUID
    user_id: uuid.UUID
    last_read_seq: int
    updated_at: datetime

    model_config = {"from_attributes": True}
