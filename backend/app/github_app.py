import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode
from pathlib import Path

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.settings import settings

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"


class GitHubConnectionError(Exception):
    pass


def installation_url(app_slug: str, state: str) -> str:
    return f"https://github.com/apps/{app_slug}/installations/new?{urlencode({'state': state})}"


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _state_cipher() -> Fernet:
    material = hashlib.sha256((settings.github_client_secret + ":patchwork-github-pkce").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(material))


def encrypt_pkce_verifier(verifier: str) -> str:
    return _state_cipher().encrypt(verifier.encode()).decode()


def decrypt_pkce_verifier(ciphertext: str) -> str:
    try:
        return _state_cipher().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise GitHubConnectionError("GitHub authorization state could not be decrypted") from exc


def oauth_authorization_url(client_id: str, callback_url: str, state: str, challenge: str) -> str:
    query = urlencode({
        "client_id": client_id,
        "redirect_uri": callback_url,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"https://github.com/login/oauth/authorize?{query}"


def api_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
    }


def create_installation_token(installation_id: int, repository_id: int, *, write_access: bool = False) -> str:
    if not settings.github_app_id or not settings.github_private_key_path:
        raise GitHubConnectionError("GitHub App private key configuration is missing")
    try:
        private_key = Path(settings.github_private_key_path).read_text(encoding="utf-8")
        now = int(time.time())
        app_jwt = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id}, private_key, algorithm="RS256")
    except (OSError, ValueError) as exc:
        raise GitHubConnectionError("GitHub App private key could not be loaded") from exc
    try:
        with httpx.Client(base_url=API_URL, timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
            permissions = {
                "contents": "write" if write_access else "read",
                "actions": "read",
                "checks": "read",
            }
            if write_access:
                permissions["pull_requests"] = "write"
            response = client.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers=api_headers(app_jwt),
                json={"repository_ids": [repository_id], "permissions": permissions},
            )
    except httpx.RequestError as exc:
        raise GitHubConnectionError("GitHub could not be reached") from exc
    if response.is_error:
        raise GitHubConnectionError("GitHub could not issue a repository-scoped installation token")
    try:
        token = response.json().get("token")
    except (ValueError, AttributeError) as exc:
        raise GitHubConnectionError("GitHub returned an invalid installation token response") from exc
    if not isinstance(token, str) or not token:
        raise GitHubConnectionError("GitHub returned an invalid installation token response")
    return token


def exchange_oauth_code(client_id: str, client_secret: str, code: str, code_verifier: str) -> str:
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            response = client.post(
                "https://github.com/login/oauth/access_token",
                data={"client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": code_verifier},
                headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        raise GitHubConnectionError("GitHub could not be reached") from exc
    if response.is_error:
        raise GitHubConnectionError("GitHub authorization could not be completed")
    try:
        token = response.json().get("access_token")
    except ValueError as exc:
        raise GitHubConnectionError("GitHub returned an invalid authorization response") from exc
    if not isinstance(token, str) or not token:
        raise GitHubConnectionError("GitHub authorization was not granted")
    return token


def list_user_installations(user_token: str, app_id: str) -> list[dict]:
    installations: list[dict] = []
    with httpx.Client(base_url=API_URL, timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
        for page in range(1, 101):
            try:
                response = client.get(
                    "/user/installations",
                    params={"per_page": 100, "page": page},
                    headers=api_headers(user_token),
                )
            except httpx.RequestError as exc:
                raise GitHubConnectionError("GitHub could not be reached") from exc
            if response.is_error:
                raise GitHubConnectionError("GitHub could not verify the app installation for this user")
            try:
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("installations", []), list):
                    raise ValueError("Invalid installations shape")
                page_items = body.get("installations", [])
            except ValueError as exc:
                raise GitHubConnectionError("GitHub returned an invalid installation list") from exc
            installations.extend(item for item in page_items if str(item.get("app_id")) == app_id)
            if len(page_items) < 100:
                return installations
    raise GitHubConnectionError("GitHub installation list exceeded the supported pagination limit")


def list_user_installation_repositories(user_token: str, installation_id: int) -> list[dict]:
    repositories: list[dict] = []
    with httpx.Client(base_url=API_URL, timeout=httpx.Timeout(10.0, connect=3.0), follow_redirects=False) as client:
        for page in range(1, 101):
            try:
                response = client.get(
                    f"/user/installations/{installation_id}/repositories",
                    params={"per_page": 100, "page": page},
                    headers=api_headers(user_token),
                )
            except httpx.RequestError as exc:
                raise GitHubConnectionError("GitHub could not be reached") from exc
            if response.is_error:
                raise GitHubConnectionError("GitHub could not list repositories for this installation")
            try:
                body = response.json()
                if not isinstance(body, dict) or not isinstance(body.get("repositories", []), list):
                    raise ValueError("Invalid repositories shape")
                page_items = body.get("repositories", [])
            except ValueError as exc:
                raise GitHubConnectionError("GitHub returned an invalid repository list") from exc
            repositories.extend(page_items)
            if len(page_items) < 100:
                return repositories
    raise GitHubConnectionError("Repository list exceeded the supported pagination limit")
