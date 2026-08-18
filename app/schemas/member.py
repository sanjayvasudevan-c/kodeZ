import uuid

from pydantic import BaseModel, field_validator

from app.models.group_member import GroupRole

ASSIGNABLE_ROLES = (GroupRole.MEMBER, GroupRole.ADMIN)


class UpdateMemberRoleRequest(BaseModel):
    role: GroupRole

    @field_validator("role")
    @classmethod
    def _assignable_role(cls, value: GroupRole) -> GroupRole:
        if value not in ASSIGNABLE_ROLES:
            raise ValueError("role must be 'member' or 'admin'")
        return value


class TransferOwnershipRequest(BaseModel):
    new_owner_id: uuid.UUID
    demote_to: GroupRole = GroupRole.ADMIN

    @field_validator("demote_to")
    @classmethod
    def _assignable_demotion(cls, value: GroupRole) -> GroupRole:
        if value not in ASSIGNABLE_ROLES:
            raise ValueError("demote_to must be 'member' or 'admin'")
        return value
