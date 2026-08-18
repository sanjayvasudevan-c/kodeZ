import uuid
from dataclasses import dataclass

from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.database import get_db
from app.models.group import Group
from app.models.group_member import GroupMember, GroupRole
from app.models.user import User


def get_current_user(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    if access_token is None:
        raise credentials_error

    user_id = decode_access_token(access_token)
    if user_id is None:
        raise credentials_error

    user = db.get(User, user_id)
    if user is None:
        raise credentials_error

    return user


def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


@dataclass
class GroupContext:
    group: Group
    membership: GroupMember


def get_membership(db: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> GroupMember | None:
    return (
        db.query(GroupMember)
        .filter(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
        .first()
    )


def get_group_and_membership(
    group_id: uuid.UUID,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> GroupContext:
    """Resolve the caller's relationship to a group. Non-members get 404, never 403 —
    a 403 would confirm the group exists for a repository they can't see."""
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    group = db.get(Group, group_id)
    if group is None:
        raise not_found

    membership = get_membership(db, group_id, user.id)
    if membership is None:
        raise not_found

    return GroupContext(group=group, membership=membership)


def require_group_role(*allowed_roles: GroupRole):
    def _dependency(ctx: GroupContext = Depends(get_group_and_membership)) -> GroupContext:
        if ctx.membership.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
            )
        return ctx

    return _dependency


_ROLE_RANK = {GroupRole.MEMBER: 0, GroupRole.ADMIN: 1, GroupRole.OWNER: 2}


@dataclass
class MemberManagementContext:
    group: Group
    actor_membership: GroupMember
    target_membership: GroupMember


def require_can_manage_member(
    user_id: uuid.UUID,
    ctx: GroupContext = Depends(get_group_and_membership),
    db: Session = Depends(get_db),
) -> MemberManagementContext:
    """A caller may only act on a member strictly below their own role — this is what
    stops an admin from removing a peer admin or (transitively) the owner."""
    target = get_membership(db, ctx.group.id, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    if _ROLE_RANK[ctx.membership.role] <= _ROLE_RANK[target.role]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )

    return MemberManagementContext(
        group=ctx.group, actor_membership=ctx.membership, target_membership=target
    )
