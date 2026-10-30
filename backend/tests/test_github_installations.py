from collections.abc import Generator
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.database import get_db
from app.main import app
from app.models import Base, GitHubInstallation, GitHubOAuthState, Repository
from app.settings import settings


@pytest.fixture
def client_and_sessions() -> Generator[tuple[TestClient, sessionmaker], None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    testing_sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine)

    def override_db():
        with testing_sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client, testing_sessions
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_github_installation_oauth_and_repository_sync(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, sessions = client_and_sessions
    monkeypatch.setattr(settings, "github_app_slug", "patchwork-ci")
    monkeypatch.setattr(settings, "github_app_id", "12345")
    monkeypatch.setattr(settings, "github_client_id", "client-id")
    monkeypatch.setattr(settings, "github_client_secret", "test-client-secret")
    monkeypatch.setattr(settings, "github_callback_url", "https://platform.example/api/v1/github/callback")

    register = client.post("/api/v1/auth/register", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "display_name": "Workspace Owner", "organization_name": "Workspace",
    })
    assert register.status_code == 201
    organization_id = client.get("/api/v1/auth/me").json()["organizations"][0]["id"]

    started = client.post("/api/v1/github/installations/start", json={"organization_id": organization_id})
    assert started.status_code == 200
    install_url = urlparse(started.json()["installation_url"])
    state = parse_qs(install_url.query)["state"][0]
    assert install_url.hostname == "github.com"

    setup = client.get(f"/api/v1/github/setup?installation_id=77&state={state}", follow_redirects=False)
    assert setup.status_code == 303
    authorization = urlparse(setup.headers["location"])
    oauth_params = parse_qs(authorization.query)
    assert authorization.path == "/login/oauth/authorize"
    assert oauth_params["code_challenge_method"] == ["S256"]
    assert oauth_params["state"] == [state]

    def exchange(*args):
        return "temporary-user-token"

    def installations(*args):
        return [{"id": 77, "app_id": 12345, "account": {"login": "org", "type": "Organization"}, "suspended_at": None}]

    def repositories(*args):
        return [{"id": 9, "full_name": "org/demo", "default_branch": "main", "private": True}]

    monkeypatch.setattr(main_module, "exchange_oauth_code", exchange)
    monkeypatch.setattr(main_module, "list_user_installations", installations)
    monkeypatch.setattr(main_module, "list_user_installation_repositories", repositories)
    callback = client.get(f"/api/v1/github/callback?code=oauth-code&state={state}", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"].endswith("/?github=connected")

    with sessions() as db:
        linked = db.scalar(select(GitHubInstallation).where(GitHubInstallation.installation_id == 77))
        repository = db.scalar(select(Repository).where(Repository.github_repo_id == 9))
        state_row = db.scalar(select(GitHubOAuthState))
        assert linked is not None and linked.account_login == "org"
        assert repository is not None and repository.monitored is False
        assert state_row is not None and state_row.consumed_at is not None


def test_github_connection_start_requires_app_configuration(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = client_and_sessions
    monkeypatch.setattr(settings, "github_app_slug", "")
    client.post("/api/v1/auth/register", json={
        "email": "owner@example.com", "password": "correct horse battery staple",
        "display_name": "Owner", "organization_name": "Org",
    })
    organization_id = client.get("/api/v1/auth/me").json()["organizations"][0]["id"]
    response = client.post("/api/v1/github/installations/start", json={"organization_id": organization_id})
    assert response.status_code == 503
