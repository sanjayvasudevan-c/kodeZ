from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user
from app.database import get_db
from app.models.device import Device
from app.models.user import User
from app.schemas.device import DevicePublic, RegisterDeviceRequest

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", response_model=DevicePublic, status_code=status.HTTP_201_CREATED)
def register_device(
    payload: RegisterDeviceRequest,
    response: Response,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Device:
    now = datetime.now(timezone.utc)

    existing = db.get(Device, payload.device_id)
    if existing is not None:
        # The devices.id PK/devices(id, user_id) FK target only prove device_id
        # is *some* device — they cannot stop a legitimate UPDATE from silently
        # reassigning user_id to a different account. That check has to live
        # here, in application logic, inside the same transaction as the write.
        if existing.user_id != user.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This device ID is already registered to a different account",
            )
        existing.last_seen_at = now
        db.add(existing)
        db.commit()
        db.refresh(existing)
        response.status_code = status.HTTP_200_OK
        return existing

    device = Device(id=payload.device_id, user_id=user.id, last_seen_at=now)
    db.add(device)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Lost a race with a concurrent registration of the same device_id.
        existing = db.get(Device, payload.device_id)
        if existing is not None and existing.user_id == user.id:
            response.status_code = status.HTTP_200_OK
            return existing
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This device ID is already registered to a different account",
        )

    db.refresh(device)
    return device
