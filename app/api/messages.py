import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import GroupContext, get_group_and_membership, get_own_device
from app.database import get_db
from app.models.device import Device
from app.models.device_cursor import DeviceCursor
from app.models.group_read_state import GroupReadState
from app.models.message import Message
from app.schemas.message import (
    CreateMessageRequest,
    MessageListResponse,
    MessagePublic,
    SyncRequest,
    SyncResponse,
)
from app.schemas.sync import (
    AckRequest,
    DeviceCursorPublic,
    ReadStatePublic,
    UpdateReadStateRequest,
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
    # Once Phase 6B retention actually deletes old rows, this reflects the true
    # oldest retained seq automatically — it's a live MIN(), never hardcoded.
    min_seq = db.query(func.min(Message.seq)).filter(Message.group_id == group_id).scalar()
    return min_seq if min_seq is not None else 0


def _get_or_create_cursor(
    db: Session, device_id: uuid.UUID, group_id: uuid.UUID, user_id: uuid.UUID
) -> DeviceCursor:
    cursor = db.get(DeviceCursor, (device_id, group_id))
    if cursor is None:
        cursor = DeviceCursor(
            device_id=device_id,
            group_id=group_id,
            user_id=user_id,
            last_acked_seq=0,
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(cursor)
        db.flush()
    return cursor


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

    if payload.device_id is not None:
        # Best-effort liveness touch only — never advances last_acked_seq.
        # Sending a message doesn't mean the device has persisted the group's
        # full history locally, only that it exists and is reachable. Unlike
        # sync/ack (where device identity IS the point of the call and an
        # invalid device_id correctly 404s), a bad device_id here must not
        # fail the send — it's a courtesy signal, not an auth requirement of
        # this endpoint. Hence a non-raising lookup instead of get_own_device.
        device = db.get(Device, payload.device_id)
        if device is not None and device.user_id == sender_id:
            cursor = _get_or_create_cursor(db, device.id, group_id, sender_id)
            cursor.last_seen_at = datetime.now(timezone.utc)
            db.add(cursor)

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
    # 1. device belongs to the authenticated user (get_own_device)
    # 2. authenticated user is a current group member (get_group_and_membership,
    #    already resolved before this body even runs)
    # 3+4. the cursor we touch is keyed by exactly (device_id, group_id), so it
    #    can never be scoped to the wrong device or the wrong group.
    device = get_own_device(db, ctx.membership.user_id, payload.device_id)
    cursor = _get_or_create_cursor(db, device.id, ctx.group.id, ctx.membership.user_id)
    cursor.last_seen_at = datetime.now(timezone.utc)
    db.add(cursor)
    db.commit()

    floor_seq = _floor_seq(db, ctx.group.id)
    # since_seq < floor_seq alone is off-by-one: if nothing has ever been
    # deleted, floor_seq equals the oldest EXISTING seq (e.g. 1), and a brand
    # new device with since_seq=0 would be flagged even though it can obtain
    # everything. The real gap condition is "the message right after since_seq
    # is no longer available", i.e. since_seq + 1 < floor_seq.
    gap_detected = floor_seq > 0 and payload.since_seq < floor_seq - 1

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
        floor_seq=floor_seq,
        gap_detected=gap_detected,
    )


@router.post("/{group_id}/ack", response_model=DeviceCursorPublic)
def ack_messages(
    payload: AckRequest,
    ctx: GroupContext = Depends(get_group_and_membership),
    db: Session = Depends(get_db),
) -> DeviceCursor:
    device = get_own_device(db, ctx.membership.user_id, payload.device_id)
    cursor = _get_or_create_cursor(db, device.id, ctx.group.id, ctx.membership.user_id)

    # Never trust a client-claimed seq beyond what the group actually has, and
    # never let the cursor move backwards — stale/duplicate ACKs are no-ops.
    clamped = min(payload.acked_seq, ctx.group.last_message_seq)
    cursor.last_acked_seq = max(cursor.last_acked_seq, clamped)
    cursor.last_seen_at = datetime.now(timezone.utc)
    db.add(cursor)
    db.commit()
    db.refresh(cursor)
    return cursor


@router.post("/{group_id}/read", response_model=ReadStatePublic)
def update_read_state(
    payload: UpdateReadStateRequest,
    ctx: GroupContext = Depends(get_group_and_membership),
    db: Session = Depends(get_db),
) -> GroupReadState:
    state = db.get(GroupReadState, (ctx.group.id, ctx.membership.user_id))
    if state is None:
        state = GroupReadState(
            group_id=ctx.group.id, user_id=ctx.membership.user_id, last_read_seq=0
        )
        db.add(state)
        db.flush()

    clamped = min(payload.last_read_seq, ctx.group.last_message_seq)
    state.last_read_seq = max(state.last_read_seq, clamped)
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


@router.get("/{group_id}/read", response_model=ReadStatePublic)
def get_read_state(
    ctx: GroupContext = Depends(get_group_and_membership), db: Session = Depends(get_db)
) -> GroupReadState:
    state = db.get(GroupReadState, (ctx.group.id, ctx.membership.user_id))
    if state is None:
        # Transient (never persisted) placeholder — SQLAlchemy's column
        # default= only fires on an actual INSERT, so updated_at must be set
        # explicitly here or the response would try to serialize None.
        return GroupReadState(
            group_id=ctx.group.id,
            user_id=ctx.membership.user_id,
            last_read_seq=0,
            updated_at=datetime.now(timezone.utc),
        )
    return state
