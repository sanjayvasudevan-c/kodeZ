import httpx2 as httpx

REPOS_URL = "https://api.github.com/user/repos"
REPO_BY_ID_URL = "https://api.github.com/repositories/{repo_id}"


class GitHubTokenInvalidError(Exception):
    """The stored GitHub token is missing, expired, or has been revoked."""


class GitHubRepoNotFoundError(Exception):
    """The repository doesn't exist or the user doesn't have access to it."""


class GitHubAPIError(Exception):
    """Any other unexpected failure talking to the GitHub API."""


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }


async def list_user_repos(access_token: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                REPOS_URL,
                headers=_headers(access_token),
                params={"per_page": 100, "sort": "updated"},
            )
    except httpx.HTTPError as exc:
        raise GitHubAPIError("Failed to reach GitHub") from exc

    if resp.status_code == 401:
        raise GitHubTokenInvalidError("GitHub token is invalid or has been revoked")
    if resp.status_code != 200:
        raise GitHubAPIError(f"GitHub API returned {resp.status_code}")

    return [
        {
            "github_repo_id": repo["id"],
            "owner_login": repo["owner"]["login"],
            "name": repo["name"],
            "full_name": repo["full_name"],
            "html_url": repo["html_url"],
            "default_branch": repo.get("default_branch"),
            "private": repo["private"],
        }
        for repo in resp.json()
    ]


async def get_repo_by_id(access_token: str, github_repo_id: int) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                REPO_BY_ID_URL.format(repo_id=github_repo_id), headers=_headers(access_token)
            )
    except httpx.HTTPError as exc:
        raise GitHubAPIError("Failed to reach GitHub") from exc

    if resp.status_code == 401:
        raise GitHubTokenInvalidError("GitHub token is invalid or has been revoked")
    if resp.status_code == 404:
        raise GitHubRepoNotFoundError("Repository not found or not accessible")
    if resp.status_code != 200:
        raise GitHubAPIError(f"GitHub API returned {resp.status_code}")

    repo = resp.json()
    return {
        "github_repo_id": repo["id"],
        "owner_login": repo["owner"]["login"],
        "name": repo["name"],
        "full_name": repo["full_name"],
        "html_url": repo["html_url"],
        "default_branch": repo.get("default_branch"),
        "private": repo["private"],
    }
