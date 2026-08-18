import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user, get_membership
from app.core.crypto import decrypt_secret
from app.core.github_api import (
    GitHubAPIError,
    GitHubRepoNotFoundError,
    GitHubTokenInvalidError,
    get_repo_by_id,
    list_user_repos,
)
from app.database import get_db
from app.models.group import Group
from app.models.repository import Repository
from app.models.user import User
from app.schemas.repository import ImportRepositoryRequest, RepositoryPublic, RepositorySummary

router = APIRouter(prefix="/repositories", tags=["repositories"])


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


@router.get("/github", response_model=list[RepositorySummary])
async def list_github_repositories(user: User = Depends(get_current_active_user)) -> list[dict]:
    token = _github_token_for(user)
    try:
        return await list_user_repos(token)
    except GitHubTokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub authorization has expired or been revoked. Please reconnect your GitHub account.",
        )
    except GitHubAPIError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to reach GitHub")


@router.post("", response_model=RepositoryPublic, status_code=status.HTTP_201_CREATED)
async def import_repository(
    payload: ImportRepositoryRequest,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Repository:
    existing = (
        db.query(Repository).filter(Repository.github_repo_id == payload.github_repo_id).first()
    )
    if existing is not None:
        return existing

    token = _github_token_for(user)
    try:
        repo_data = await get_repo_by_id(token, payload.github_repo_id)
    except GitHubTokenInvalidError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub authorization has expired or been revoked. Please reconnect your GitHub account.",
        )
    except GitHubRepoNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found or not accessible with your GitHub account",
        )
    except GitHubAPIError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to reach GitHub")

    repository = Repository(**repo_data, imported_by_id=user.id)
    db.add(repository)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Lost a race with a concurrent import of the same repo; return the winner.
        existing = (
            db.query(Repository)
            .filter(Repository.github_repo_id == payload.github_repo_id)
            .first()
        )
        if existing is not None:
            return existing
        raise
    db.refresh(repository)
    return repository


@router.get("/{repository_id}", response_model=RepositoryPublic)
def get_repository(
    repository_id: uuid.UUID,
    user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> Repository:
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    repository = db.get(Repository, repository_id)
    if repository is None:
        raise not_found

    group = db.query(Group).filter(Group.repository_id == repository.id).first()
    if group is not None and get_membership(db, group.id, user.id) is None:
        # Same non-disclosing behavior as group authorization: a repository
        # attached to a group the caller can't see must not reveal that it exists.
        raise not_found

    return repository
