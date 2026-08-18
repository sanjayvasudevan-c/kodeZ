import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.config import settings
from app.core.crypto import encrypt_secret
from app.core.github_oauth import (
    GitHubOAuthError,
    build_authorize_url,
    exchange_code_for_token,
    fetch_github_profile,
)
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.database import get_db
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, UserPublic

router = APIRouter(prefix="/auth", tags=["auth"])

_ACCESS_COOKIE = "access_token"
_REFRESH_COOKIE = "refresh_token"
_REFRESH_COOKIE_PATH = "/auth"
_OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_STATE_COOKIE_PATH = "/auth/github"


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    response.set_cookie(
        key=_ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
    )
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.refresh_token_expire_days * 24 * 60 * 60,
        path=_REFRESH_COOKIE_PATH,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(_ACCESS_COOKIE, path="/")
    response.delete_cookie(_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)


def _issue_tokens(db: Session, response: Response, user: User, family_id: uuid.UUID | None = None) -> None:
    access_token = create_access_token(user.id)
    raw_refresh_token = generate_refresh_token()

    refresh_row = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh_token),
        family_id=family_id or uuid.uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(refresh_row)
    db.commit()

    _set_auth_cookies(response, access_token, raw_refresh_token)


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> User:
    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

    if payload.username is not None:
        if db.query(User).filter(User.username == payload.username).first() is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already taken")

    user = User(
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=UserPublic)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
    )

    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or user.password_hash is None:
        raise invalid_credentials
    if not verify_password(payload.password, user.password_hash):
        raise invalid_credentials
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")

    _issue_tokens(db, response, user)
    return user


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> None:
    invalid_token = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    if refresh_token is None:
        raise invalid_token

    token_hash = hash_refresh_token(refresh_token)
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
    if row is None:
        raise invalid_token

    if row.revoked_at is not None:
        # Reuse of an already-rotated/revoked token: treat as compromise, kill the whole family.
        db.query(RefreshToken).filter(
            RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None)
        ).update({"revoked_at": datetime.now(timezone.utc)})
        db.commit()
        raise invalid_token

    if row.expires_at < datetime.now(timezone.utc):
        raise invalid_token

    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise invalid_token

    row.revoked_at = datetime.now(timezone.utc)
    db.add(row)
    _issue_tokens(db, response, user, family_id=row.family_id)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> None:
    if refresh_token is not None:
        token_hash = hash_refresh_token(refresh_token)
        row = db.query(RefreshToken).filter(RefreshToken.token_hash == token_hash).first()
        if row is not None and row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            db.add(row)
            db.commit()

    _clear_auth_cookies(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)
    ).update({"revoked_at": datetime.now(timezone.utc)})
    db.commit()
    _clear_auth_cookies(response)


def _get_or_create_github_user(db: Session, profile: dict, encrypted_github_token: str) -> User:
    user = db.query(User).filter(User.github_id == profile["github_id"]).first()

    if user is None and profile.get("email"):
        # Local account with a matching verified GitHub email: link, don't duplicate.
        user = db.query(User).filter(User.email == profile["email"]).first()
        if user is not None:
            user.github_id = profile["github_id"]
            user.github_username = profile["github_username"]
            user.avatar_url = user.avatar_url or profile.get("avatar_url")
            user.full_name = user.full_name or profile.get("full_name")

    if user is None:
        user = User(
            github_id=profile["github_id"],
            github_username=profile["github_username"],
            email=profile.get("email"),
            full_name=profile.get("full_name"),
            avatar_url=profile.get("avatar_url"),
            email_verified=bool(profile.get("email")),
        )

    user.github_access_token = encrypted_github_token
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/github/login")
def github_login() -> RedirectResponse:
    if not settings.github_client_id:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="GitHub OAuth is not configured")

    state = secrets.token_urlsafe(24)
    redirect = RedirectResponse(build_authorize_url(state))
    redirect.set_cookie(
        key=_OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=600,
        path=_OAUTH_STATE_COOKIE_PATH,
    )
    return redirect


@router.get("/github/callback", response_model=UserPublic)
async def github_callback(
    code: str,
    state: str,
    response: Response,
    oauth_state: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    if oauth_state is None or not secrets.compare_digest(state, oauth_state):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")

    try:
        github_access_token = await exchange_code_for_token(code)
        profile = await fetch_github_profile(github_access_token)
    except GitHubOAuthError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="GitHub authentication failed")

    user = _get_or_create_github_user(db, profile, encrypt_secret(github_access_token))
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")

    _issue_tokens(db, response, user)
    response.delete_cookie(_OAUTH_STATE_COOKIE, path=_OAUTH_STATE_COOKIE_PATH)
    return user
