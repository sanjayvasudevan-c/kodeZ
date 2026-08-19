import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import GroupContext, get_group_and_membership
from app.database import get_db
from app.models.message import Message
from app.schemas.message import (
    CreateMessageRequest,
    MessageListResponse,
    MessagePublic,
    SyncRequest,
    SyncResponse,
)

router = APIRouter(prefix="/groups", tags=["messages"])


def _find_by_client_message_id(
    db: Session, group_id: uuid.UUID, sender_id: uuid.UUID, client_message_id: uuid.UUID
) -> Message | None:
    return (
        db.query(Message)
        .filter(
            Message.group_id == group_id,
            Message.sender_id == sender_id,
            Message.client_message_id == client_message_id,
        )
        .first()
    )


def _floor_seq(db: Session, group_id: uuid.UUID) -> int:
    # Phase 6A never deletes messages, so this is always the true minimum retained
    # seq for the group (1, or 0 if empty). Computed for real — rather than
    # hardcoded — so it stays correct once Phase 6B activates retention deletion.
    min_seq = db.query(func.min(Message.seq)).filter(Message.group_id == group_id).scalar()
    return min_seq if min_seq is not None else 0


@router.post("/{group_id}/messages", response_model=MessagePublic, status_code=status.HTTP_201_CREATED)
def send_message(
    payload: CreateMessageRequest,
    response: Response,
    ctx: GroupContext = Depends(get_group_and_membership),
    db: Session = Depends(get_db),
) -> Message:
    group_id = ctx.group.id
    sender_id = ctx.membership.user_id

    existing = _find_by_client_message_id(db, group_id, sender_id, payload.client_message_id)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return existing

    # Lock the group row for the whole transaction: increment + insert + commit.
    # The lock is held until commit, which serializes concurrent senders in this
    # group and guarantees seq order matches commit (visibility) order exactly.
    #
    # ctx.group was already loaded (unlocked) by get_group_and_membership earlier
    # in this request, so it's already in this session's identity map. A plain
    # `select(...).with_for_update()` still issues (and is blocked by) the lock
    # correctly, but SQLAlchemy does NOT overwrite an already-identity-mapped
    # object's attributes with the freshly-locked row by default — last_message_seq
    # would silently stay stale even though the lock itself was acquired correctly.
    # db.refresh(..., with_for_update=True) both locks AND refreshes the attributes.
    db.refresh(ctx.group, with_for_update=True)
    group = ctx.group
    group.last_message_seq += 1
    new_seq = group.last_message_seq

    message = Message(
        group_id=group_id,
        sender_id=sender_id,
        seq=new_seq,
        body=payload.body,
        client_message_id=payload.client_message_id,
        client_created_at=payload.client_created_at,
    )
    db.add(message)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Lost a race with a concurrent retry carrying the same client_message_id.
        existing = _find_by_client_message_id(db, group_id, sender_id, payload.client_message_id)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return existing
        raise

    db.refresh(message)
    return message


@router.get("/{group_id}/messages", response_model=MessageListResponse)
def list_messages(
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: GroupContext = Depends(get_group_and_membership),
    db: Session = Depends(get_db),
) -> MessageListResponse:
    query = db.query(Message).filter(Message.group_id == ctx.group.id)
    if before_seq is not None:
        query = query.filter(Message.seq < before_seq)

    rows_desc = query.order_by(Message.seq.desc()).limit(limit + 1).all()
    has_more = len(rows_desc) > limit
    page = rows_desc[:limit]
    next_before_seq = page[-1].seq if (has_more and page) else None

    return MessageListResponse(
        messages=list(reversed(page)), has_more=has_more, next_before_seq=next_before_seq
    )


@router.post("/{group_id}/sync", response_model=SyncResponse)
def sync_messages(
    payload: SyncRequest,
    ctx: GroupContext = Depends(get_group_and_membership),
    db: Session = Depends(get_db),
) -> SyncResponse:
    rows = (
        db.query(Message)
        .filter(Message.group_id == ctx.group.id, Message.seq > payload.since_seq)
        .order_by(Message.seq.asc())
        .limit(payload.limit + 1)
        .all()
    )
    has_more = len(rows) > payload.limit
    rows = rows[: payload.limit]
    next_seq = rows[-1].seq if rows else payload.since_seq

    return SyncResponse(
        messages=rows,
        next_seq=next_seq,
        has_more=has_more,
        server_last_seq=ctx.group.last_message_seq,
        floor_seq=_floor_seq(db, ctx.group.id),
    )
