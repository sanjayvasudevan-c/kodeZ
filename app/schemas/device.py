import uuid
from datetime import datetime

from pydantic import BaseModel


class RegisterDeviceRequest(BaseModel):
    device_id: uuid.UUID


class DevicePublic(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime

    model_config = {"from_attributes": True}
