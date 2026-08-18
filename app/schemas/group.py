import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.group_member import GroupRole


class CreateGroupRequest(BaseModel):
    repository_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class UpdateGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class GroupPublic(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    repository_id: uuid.UUID
    owner_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    my_role: GroupRole | None = None

    model_config = {"from_attributes": True}


class GroupMemberPublic(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    user_id: uuid.UUID
    role: GroupRole
    created_at: datetime

    model_config = {"from_attributes": True}
