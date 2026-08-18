from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user, get_membership
from app.core.security import hash_invitation_token
from app.database import get_db
from app.models.group_invitation import GroupInvitation, InvitationStatus
from app.models.group_member import GroupMember
from app.models.user import User
from app.schemas.group import GroupMemberPublic
from app.schemas.invitation import InvitationPublic, TokenRequest

router = APIRouter(prefix="/invitations", tags=["invitations"])


@router.get("", response_model=list[InvitationPublic])
def list_my_invitations(
    user: User = Depends(get_current_active_user), db: Session = Depends(get_db)
) -> list[GroupInvitation]:
    now = datetime.now(timezone.utc)
    query = db.query(GroupInvitation).filter(
        GroupInvitation.status == InvitationStatus.PENDING, GroupInvitation.expires_at > now
    )

    if user.email:
        query = query.filter(
            or_(
                GroupInvitation.invited_user_id == user.id,
                func.lower(GroupInvitation.invited_email) == user.email.lower(),
            )
        )
    else:
        query = query.filter(GroupInvitation.invited_user_id == user.id)

    return query.all()


def _resolve_live_invitation_for_user(
    db: Session, token: str, user: User
) -> GroupInvitation:
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    invitation = (
        db.query(GroupInvitation)
        .filter(GroupInvitation.token_hash == hash_invitation_token(token))
        .first()
    )
    if invitation is None:
        raise not_found
    if invitation.status != InvitationStatus.PENDING:
        raise not_found
    if invitation.expires_at < datetime.now(timezone.utc):
        raise not_found

    if invitation.invited_user_id is not None:
        if invitation.invited_user_id != user.id:
            raise not_found
    else:
        if not user.email or user.email.lower() != (invitation.invited_email or "").lower():
            raise not_found

    return invitation


@router.post("/accept", response_model=GroupMemberPublic)
def accept_invitation(
    payload: TokenRequest,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> GroupMember:
    invitation = _resolve_live_invitation_for_user(db, payload.token, user)

    # Conditional UPDATE ... WHERE status = 'pending' is the single-use / no-reuse guard:
    # a lost race (double accept) updates zero rows and the loser gets 404.
    updated = (
        db.query(GroupInvitation)
        .filter(GroupInvitation.id == invitation.id, GroupInvitation.status == InvitationStatus.PENDING)
        .update(
            {"status": InvitationStatus.ACCEPTED, "responded_at": datetime.now(timezone.utc)}
        )
    )
    if updated == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    membership = get_membership(db, invitation.group_id, user.id)
    if membership is None:
        membership = GroupMember(group_id=invitation.group_id, user_id=user.id, role=invitation.role)
        db.add(membership)

    db.commit()
    db.refresh(membership)
    return membership


@router.post("/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_invitation(
    payload: TokenRequest,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> None:
    invitation = _resolve_live_invitation_for_user(db, payload.token, user)

    updated = (
        db.query(GroupInvitation)
        .filter(GroupInvitation.id == invitation.id, GroupInvitation.status == InvitationStatus.PENDING)
        .update(
            {"status": InvitationStatus.REJECTED, "responded_at": datetime.now(timezone.utc)}
        )
    )
    db.commit()
    if updated == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
