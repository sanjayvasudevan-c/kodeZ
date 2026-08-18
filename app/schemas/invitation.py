import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.group_invitation import InvitationStatus
from app.models.group_member import GroupRole

INVITABLE_ROLES = (GroupRole.MEMBER, GroupRole.ADMIN)


class CreateInvitationRequest(BaseModel):
    user_id: uuid.UUID | None = None
    email: EmailStr | None = None
    role: GroupRole = GroupRole.MEMBER

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "CreateInvitationRequest":
        if (self.user_id is None) == (self.email is None):
            raise ValueError("Provide exactly one of user_id or email")
        if self.role not in INVITABLE_ROLES:
            raise ValueError("role must be 'member' or 'admin'")
        return self


class InvitationPublic(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    inviter_id: uuid.UUID | None
    invited_user_id: uuid.UUID | None
    invited_email: str | None
    role: GroupRole
    status: InvitationStatus
    expires_at: datetime
    responded_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class InvitationCreated(InvitationPublic):
    token: str


class TokenRequest(BaseModel):
    token: str = Field(min_length=1)
