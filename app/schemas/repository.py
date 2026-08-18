import uuid
from datetime import datetime

from pydantic import BaseModel


class RepositorySummary(BaseModel):
    """A repository as seen live from GitHub, not yet imported."""

    github_repo_id: int
    owner_login: str
    name: str
    full_name: str
    html_url: str
    default_branch: str | None
    private: bool


class ImportRepositoryRequest(BaseModel):
    github_repo_id: int


class RepositoryPublic(BaseModel):
    id: uuid.UUID
    github_repo_id: int
    owner_login: str
    name: str
    full_name: str
    html_url: str
    default_branch: str | None
    private: bool
    imported_by_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}
