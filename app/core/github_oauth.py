import httpx2 as httpx

from app.config import settings

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
EMAILS_URL = "https://api.github.com/user/emails"


class GitHubOAuthError(Exception):
    pass


def build_authorize_url(state: str) -> str:
    url = httpx.URL(
        AUTHORIZE_URL,
        params={
            "client_id": settings.github_client_id,
            "redirect_uri": settings.github_redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        },
    )
    return str(url)


async def exchange_code_for_token(code: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": settings.github_redirect_uri,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("Failed to reach GitHub") from exc

    access_token = data.get("access_token")
    if not access_token:
        raise GitHubOAuthError(data.get("error_description", "GitHub token exchange failed"))
    return access_token


async def fetch_github_profile(access_token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            user_resp = await client.get(USER_URL, headers=headers)
            user_resp.raise_for_status()
            user = user_resp.json()

            email = user.get("email")
            if not email:
                emails_resp = await client.get(EMAILS_URL, headers=headers)
                emails_resp.raise_for_status()
                primary = next(
                    (e for e in emails_resp.json() if e.get("primary") and e.get("verified")),
                    None,
                )
                if primary:
                    email = primary["email"]
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("Failed to fetch GitHub profile") from exc

    return {
        "github_id": user["id"],
        "github_username": user.get("login"),
        "email": email,
        "full_name": user.get("name"),
        "avatar_url": user.get("avatar_url"),
    }
