import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import (
    GroupContext,
    MemberManagementContext,
    get_current_active_user,
    get_group_and_membership,
    get_membership,
    require_can_manage_member,
    require_group_role,
)
from app.core.crypto import decrypt_secret
from app.core.github_api import (
    GitHubAPIError,
    GitHubRepoNotFoundError,
    GitHubTokenInvalidError,
    get_repo_by_id,
)
from app.core.security import generate_invitation_token, hash_invitation_token
from app.database import get_db
from app.models.group import Group
from app.models.group_invitation import GroupInvitation, InvitationStatus
from app.models.group_member import GroupMember, GroupRole
from app.models.repository import Repository
from app.models.user import User
from app.schemas.group import CreateGroupRequest, GroupMemberPublic, GroupPublic, UpdateGroupRequest
from app.schemas.invitation import CreateInvitationRequest, InvitationCreated, InvitationPublic
from app.schemas.member import TransferOwnershipRequest, UpdateMemberRoleRequest

router = APIRouter(prefix="/groups", tags=["groups"])


def _to_group_public(group: Group, my_role: GroupRole | None) -> GroupPublic:
    public = GroupPublic.model_validate(group)
    public.my_role = my_role
    return public


def _github_token_for(user: User) -> str:
    if not user.github_access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub account connected. Sign in with GitHub first.",
        )
    token = decrypt_secret(user.github_access_token)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Stored GitHub authorization is invalid. Please reconnect your GitHub account.",
        )
    return token


@router.post("", response_model=GroupPublic, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: CreateGroupRequest,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> GroupPublic:
    repository = db.get(Repository, payload.repository_id)
    if repository is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    existing = db.query(Group).filter(Group.repository_id == repository.id).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repository is already attached to a group",
        )

    token = _github_token_for(user)
    try:
        await get_repo_by_id(token, repository.github_repo_id)
    except GitHubTokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub authorization has expired or been revoked. Please reconnect your GitHub account.",
        )
    except GitHubRepoNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your GitHub account no longer has access to this repository",
        )
    except GitHubAPIError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to reach GitHub")

    group = Group(
        name=payload.name or repository.full_name,
        description=payload.description,
        repository_id=repository.id,
        owner_id=user.id,
    )
    db.add(group)
    try:
        db.flush()
        db.add(GroupMember(group_id=group.id, user_id=user.id, role=GroupRole.OWNER))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Repository is already attached to a group",
        )
    db.refresh(group)

    return _to_group_public(group, GroupRole.OWNER)


@router.get("", response_model=list[GroupPublic])
def list_my_groups(
    user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
) -> list[GroupPublic]:
    rows = (
        db.query(Group, GroupMember)
        .join(GroupMember, GroupMember.group_id == Group.id)
        .filter(GroupMember.user_id == user.id)
        .all()
    )
    return [_to_group_public(group, member.role) for group, member in rows]


@router.get("/{group_id}", response_model=GroupPublic)
def get_group(ctx: GroupContext = Depends(get_group_and_membership)) -> GroupPublic:
    return _to_group_public(ctx.group, ctx.membership.role)


@router.patch("/{group_id}", response_model=GroupPublic)
def update_group(
    payload: UpdateGroupRequest,
    ctx: GroupContext = Depends(require_group_role(GroupRole.ADMIN, GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> GroupPublic:
    if payload.name is not None:
        ctx.group.name = payload.name
    if payload.description is not None:
        ctx.group.description = payload.description

    db.add(ctx.group)
    db.commit()
    db.refresh(ctx.group)
    return _to_group_public(ctx.group, ctx.membership.role)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    ctx: GroupContext = Depends(require_group_role(GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> None:
    db.delete(ctx.group)
    db.commit()


@router.get("/{group_id}/members", response_model=list[GroupMemberPublic])
def list_members(
    ctx: GroupContext = Depends(get_group_and_membership), db: Session = Depends(get_db)
) -> list[GroupMember]:
    return db.query(GroupMember).filter(GroupMember.group_id == ctx.group.id).all()


@router.post("/{group_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
def leave_group(
    ctx: GroupContext = Depends(get_group_and_membership), db: Session = Depends(get_db)
) -> None:
    if ctx.membership.role == GroupRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The owner cannot leave the group. Delete the group instead.",
        )
    db.delete(ctx.membership)
    db.commit()


def _find_pending_invitation(
    db: Session, group_id: uuid.UUID, invited_user_id: uuid.UUID | None, invited_email: str | None
) -> GroupInvitation | None:
    query = db.query(GroupInvitation).filter(
        GroupInvitation.group_id == group_id, GroupInvitation.status == InvitationStatus.PENDING
    )
    if invited_user_id is not None:
        query = query.filter(GroupInvitation.invited_user_id == invited_user_id)
    else:
        query = query.filter(func.lower(GroupInvitation.invited_email) == invited_email.lower())
    return query.first()


@router.post(
    "/{group_id}/invitations", response_model=InvitationCreated, status_code=status.HTTP_201_CREATED
)
def create_invitation(
    payload: CreateInvitationRequest,
    ctx: GroupContext = Depends(require_group_role(GroupRole.ADMIN, GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> GroupInvitation:
    if payload.role == GroupRole.ADMIN and ctx.membership.role != GroupRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can invite at admin role"
        )

    invited_user_id: uuid.UUID | None = None
    invited_email: str | None = None

    if payload.user_id is not None:
        target_user = db.get(User, payload.user_id)
        if target_user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        invited_user_id = target_user.id
        invited_email = target_user.email.lower() if target_user.email else None
    else:
        email = payload.email.lower()
        existing_user = db.query(User).filter(func.lower(User.email) == email).first()
        if existing_user is not None:
            invited_user_id = existing_user.id
        invited_email = email

    if invited_user_id is not None and get_membership(db, ctx.group.id, invited_user_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="User is already a member of this group"
        )

    now = datetime.now(timezone.utc)
    existing_pending = _find_pending_invitation(db, ctx.group.id, invited_user_id, invited_email)
    if existing_pending is not None:
        if existing_pending.expires_at > now:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An active invitation already exists for this user",
            )
        # Stale pending row past expiry — cancel it to free the unique (group, target) slot.
        existing_pending.status = InvitationStatus.CANCELLED
        existing_pending.responded_at = now
        db.add(existing_pending)
        db.flush()

    raw_token = generate_invitation_token()
    invitation = GroupInvitation(
        group_id=ctx.group.id,
        inviter_id=ctx.membership.user_id,
        invited_user_id=invited_user_id,
        invited_email=invited_email,
        token_hash=hash_invitation_token(raw_token),
        role=payload.role,
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)

    return InvitationCreated(
        **InvitationPublic.model_validate(invitation).model_dump(), token=raw_token
    )


@router.get("/{group_id}/invitations", response_model=list[InvitationPublic])
def list_invitations(
    ctx: GroupContext = Depends(require_group_role(GroupRole.ADMIN, GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> list[GroupInvitation]:
    now = datetime.now(timezone.utc)
    return (
        db.query(GroupInvitation)
        .filter(
            GroupInvitation.group_id == ctx.group.id,
            GroupInvitation.status == InvitationStatus.PENDING,
            GroupInvitation.expires_at > now,
        )
        .all()
    )


@router.delete("/{group_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
def cancel_invitation(
    invitation_id: uuid.UUID,
    ctx: GroupContext = Depends(require_group_role(GroupRole.ADMIN, GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> None:
    updated = (
        db.query(GroupInvitation)
        .filter(
            GroupInvitation.id == invitation_id,
            GroupInvitation.group_id == ctx.group.id,
            GroupInvitation.status == InvitationStatus.PENDING,
        )
        .update({"status": InvitationStatus.CANCELLED, "responded_at": datetime.now(timezone.utc)})
    )
    db.commit()
    if updated == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found"
        )


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    mgmt: MemberManagementContext = Depends(require_can_manage_member),
    db: Session = Depends(get_db),
) -> None:
    db.delete(mgmt.target_membership)
    db.commit()


@router.patch("/{group_id}/members/{user_id}/role", response_model=GroupMemberPublic)
def update_member_role(
    payload: UpdateMemberRoleRequest,
    user_id: uuid.UUID,
    ctx: GroupContext = Depends(require_group_role(GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> GroupMember:
    if user_id == ctx.membership.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use transfer-ownership to change the owner's role",
        )

    target = get_membership(db, ctx.group.id, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    if target.role == GroupRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot change the owner's role directly"
        )

    target.role = payload.role
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.post("/{group_id}/transfer-ownership", response_model=GroupPublic)
def transfer_ownership(
    payload: TransferOwnershipRequest,
    ctx: GroupContext = Depends(require_group_role(GroupRole.OWNER)),
    db: Session = Depends(get_db),
) -> GroupPublic:
    if payload.new_owner_id == ctx.membership.user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is already the owner")

    new_owner_membership = get_membership(db, ctx.group.id, payload.new_owner_id)
    if new_owner_membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Target user is not a member of this group"
        )

    # Demote the current owner BEFORE promoting the new one: the partial unique index
    # (group_id) WHERE role='owner' cannot be deferred, so two 'owner' rows can never
    # coexist mid-transaction — promote-then-demote would violate it.
    ctx.membership.role = payload.demote_to
    db.add(ctx.membership)
    db.flush()

    new_owner_membership.role = GroupRole.OWNER
    db.add(new_owner_membership)
    db.flush()

    ctx.group.owner_id = payload.new_owner_id
    db.add(ctx.group)
    db.commit()
    db.refresh(ctx.group)

    return _to_group_public(ctx.group, payload.demote_to)
