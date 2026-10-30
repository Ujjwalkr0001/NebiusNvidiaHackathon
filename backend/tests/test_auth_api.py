from collections.abc import Generator
import asyncio
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
import app.main as main_module
import app.rate_limit as rate_limit_module
from app.main import app
from app.models import AuditLog, Base, GitHubInstallation, MilisaCacheLookup, MilisaFixPattern, PullRequest, RepairJob, Repository, RepositorySetting
from app.rate_limit import RateLimitRule, consume_rate_limit, rate_limit_rule
from app.settings import settings
from starlette.requests import Request


@pytest.fixture(autouse=True)
def disable_external_rate_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def under_limit(_request, rule):
        return 1, rule.window_seconds

    monkeypatch.setattr(main_module, "consume_rate_limit", under_limit)


@pytest.fixture
def client() -> Generator[tuple[TestClient, sessionmaker], None, None]:
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


def register(client: TestClient, email: str) -> dict:
    response = client.post("/api/v1/auth/register", json={
        "email": email,
        "password": "correct horse battery staple",
        "display_name": "Test User",
        "organization_name": "Test Org",
    })
    assert response.status_code == 201
    return response.json()


def test_register_session_and_logout(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, _ = client
    register(test_client, "owner@example.com")
    assert test_client.cookies.get("patchwork_session")
    assert test_client.get("/api/v1/auth/me").json()["organizations"][0]["role"] == "owner"
    assert test_client.get("/api/v1/dashboard/overview").json()["connected_repositories"] == 0
    assert test_client.post("/api/v1/auth/logout").status_code == 200
    assert test_client.get("/api/v1/auth/me").status_code == 401


def test_dashboard_does_not_leak_other_organizations(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, testing_sessions = client
    register(test_client, "first@example.com")
    first_org = test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"]
    with testing_sessions() as db_session:
        repository = Repository(
            organization_id=UUID(first_org),
            installation_id=77,
            github_repo_id=101,
            full_name="first/private-repo",
            default_branch="main",
            private=True,
            monitored=True,
        )
        db_session.add(repository)
        db_session.flush()
        job = RepairJob(
            organization_id=UUID(first_org), repository_id=repository.id,
            idempotency_key="first-cache-job", status="NEEDS_HUMAN_REVIEW",
            commit_sha="1" * 40, base_branch="main", failure_category="test",
            failure_summary="test failure",
        )
        db_session.add(job)
        db_session.flush()
        db_session.add(MilisaCacheLookup(repair_job_id=job.id, outcome="VALIDATED_HIT", estimated_tokens_saved=1200))
        db_session.commit()

    assert test_client.get("/api/v1/dashboard/overview").json()["estimated_tokens_saved"] == 1200

    test_client.post("/api/v1/auth/logout")
    register(test_client, "second@example.com")
    metrics = test_client.get("/api/v1/dashboard/overview").json()
    assert metrics["connected_repositories"] == 0
    assert metrics["monitored_repositories"] == 0
    assert metrics["estimated_tokens_saved"] == 0


def test_duplicate_email_and_bad_credentials_are_rejected(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, _ = client
    register(test_client, "owner@example.com")
    duplicate = test_client.post("/api/v1/auth/register", json={
        "email": "OWNER@example.com",
        "password": "correct horse battery staple",
        "display_name": "Another User",
        "organization_name": "Another Org",
    })
    assert duplicate.status_code == 409
    test_client.post("/api/v1/auth/logout")
    invalid_login = test_client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "wrong"})
    assert invalid_login.status_code == 401


def test_cross_origin_cookie_mutations_are_rejected(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, _ = client
    response = test_client.post(
        "/api/v1/auth/register",
        headers={"Origin": "https://attacker.example"},
        json={"email": "owner@example.com", "password": "correct horse battery staple", "display_name": "Owner", "organization_name": "Org"},
    )
    assert response.status_code == 403


def test_repository_monitor_patch_preflight_is_allowed(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, _ = client
    response = test_client.options(
        "/api/v1/github/repositories/00000000-0000-0000-0000-000000000000/monitor",
        headers={
            "Origin": settings.frontend_origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == settings.frontend_origin
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_repository_trigger_policy_is_visible_and_owner_configurable(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, testing_sessions = client
    register(test_client, "trigger-owner@example.com")
    organization_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        repository = Repository(
            organization_id=organization_id, installation_id=88, github_repo_id=301,
            full_name="org/triggered", default_branch="main", private=True, monitored=True,
        )
        db.add(repository)
        db.commit()
        repository_id = str(repository.id)

    listed = test_client.get(f"/api/v1/github/repositories?organization_id={organization_id}")
    assert listed.status_code == 200
    assert listed.json()[0]["triggers"] == {"workflow_run_failure": True, "check_run_failure": True}
    assert listed.json()[0]["required_checks"] == []
    updated = test_client.patch(f"/api/v1/github/repositories/{repository_id}/triggers", json={
        "workflow_run_failure": False, "check_run_failure": True,
    })
    assert updated.status_code == 200
    assert updated.json()["triggers"] == {"workflow_run_failure": False, "check_run_failure": True}
    listed = test_client.get(f"/api/v1/github/repositories?organization_id={organization_id}")
    assert listed.json()[0]["triggers"] == {"workflow_run_failure": False, "check_run_failure": True}
    checks = test_client.patch(f"/api/v1/github/repositories/{repository_id}/checks", json={
        "required_checks": ["ruff", "build"],
    })
    assert checks.status_code == 200 and checks.json()["required_checks"] == ["ruff", "build"]
    invalid = test_client.patch(f"/api/v1/github/repositories/{repository_id}/checks", json={
        "required_checks": ["ruff; touch bad"],
    })
    assert invalid.status_code == 422
    listed = test_client.get(f"/api/v1/github/repositories?organization_id={organization_id}")
    assert listed.json()[0]["required_checks"] == ["ruff", "build"]


def test_owner_can_configure_and_clear_repository_patch_scope(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, testing_sessions = client
    register(test_client, "patch-scope-owner@example.com")
    organization_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        repository = Repository(
            organization_id=organization_id, installation_id=89, github_repo_id=302,
            full_name="org/scoped", default_branch="main", private=True, monitored=True,
        )
        db.add(repository)
        db.commit()
        repository_id = str(repository.id)

    updated = test_client.patch(f"/api/v1/github/repositories/{repository_id}/patch-scope", json={
        "allowed_paths": ["src/app.py", "tests/test_app.py"],
    })
    assert updated.status_code == 200
    assert updated.json()["allowed_paths"] == ["src/app.py", "tests/test_app.py"]
    listed = test_client.get(f"/api/v1/github/repositories?organization_id={organization_id}")
    assert listed.json()[0]["allowed_paths"] == ["src/app.py", "tests/test_app.py"]
    assert test_client.patch(f"/api/v1/github/repositories/{repository_id}/patch-scope", json={
        "allowed_paths": ["../outside.py"],
    }).status_code == 422
    assert test_client.patch(f"/api/v1/github/repositories/{repository_id}/patch-scope", json={
        "allowed_paths": ["src/app.py", "src/app.py"],
    }).status_code == 422
    cleared = test_client.patch(f"/api/v1/github/repositories/{repository_id}/patch-scope", json={"allowed_paths": []})
    assert cleared.status_code == 200 and cleared.json()["allowed_paths"] == []
    with testing_sessions() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "repository.patch_scope.updated"))
        assert audit is not None and audit.user_id is not None


def test_verified_milisa_detail_is_complete_and_tenant_scoped(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, testing_sessions = client
    register(test_client, "cache-detail-owner@example.com")
    organization_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        pattern = MilisaFixPattern(
            organization_id=organization_id, error_signature="e" * 64,
            failure_category="test", language="python", framework="pytest",
            version_constraints={"python": "3.13"}, context_fingerprint="c" * 64,
            root_cause="Incorrect default caused assertion failure.",
            patch_template="diff --git a/app.py b/app.py\n...",
            regression_reference="tests/test_app.py::test_value",
            validation_metadata={"status": "PASSED", "checks": [{"name": "pytest", "outcome": "PASSED"}], "environment": {"network": "disabled"}},
            source_revision="a" * 40, target_revision="b" * 40,
            verification_status="VERIFIED", success_count=1, reuse_count=2,
        )
        db.add(pattern)
        db.commit()
        pattern_id = pattern.id

    detail = test_client.get(f"/api/v1/milisa/patterns/{pattern_id}")
    assert detail.status_code == 200
    assert detail.json()["patch_template"].startswith("diff --git")
    assert detail.json()["validation_metadata"]["environment"]["network"] == "disabled"
    assert detail.json()["version_constraints"] == {"python": "3.13"}
    assert detail.json()["regression_reference"] == "tests/test_app.py::test_value"
    assert detail.json()["source_revision"] == "a" * 40
    assert detail.json()["target_revision"] == "b" * 40

    test_client.post("/api/v1/auth/logout")
    register(test_client, "cache-detail-other@example.com")
    assert test_client.get(f"/api/v1/milisa/patterns/{pattern_id}").status_code == 404


def test_repair_job_detail_includes_current_pull_request_status(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, testing_sessions = client
    register(test_client, "pr-status-owner@example.com")
    organization_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        repository = Repository(
            organization_id=organization_id, installation_id=90, github_repo_id=303,
            full_name="org/pr-status", default_branch="main", private=True,
        )
        db.add(repository)
        db.flush()
        job = RepairJob(
            organization_id=organization_id, repository_id=repository.id,
            idempotency_key="pr-status-job", status="COMPLETED",
            commit_sha="c" * 40, base_branch="main", failure_category="test",
            failure_summary="A test failed", pr_url="https://github.com/org/pr-status/pull/4",
        )
        db.add(job)
        db.flush()
        db.add(PullRequest(
            repair_job_id=job.id, github_pr_id=444, number=4,
            url=job.pr_url, branch_name="patchwork/repair-pr-status", status="MERGED",
        ))
        db.commit()
        job_id = job.id

    detail = test_client.get(f"/api/v1/repair-jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["pull_request"]["number"] == 4
    assert detail.json()["pull_request"]["status"] == "MERGED"
    assert detail.json()["pull_request"]["url"].endswith("/pull/4")


def test_owner_can_manually_queue_tenant_scoped_repair(client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch) -> None:
    test_client, testing_sessions = client
    register(test_client, "manual-owner@example.com")
    organization_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        db.add(GitHubInstallation(
            organization_id=organization_id, installation_id=888,
            account_login="manual-owner", account_type="User", suspended=False,
        ))
        repository = Repository(
            organization_id=organization_id, installation_id=888, github_repo_id=989,
            full_name="org/manual", default_branch="main", private=True, monitored=False,
        )
        db.add(repository)
        db.commit()
        repository_id = repository.id

    queued = []
    monkeypatch.setattr(main_module, "enqueue_repair", lambda job_id: queued.append(job_id) or str(job_id))
    response = test_client.post(f"/api/v1/github/repositories/{repository_id}/repair", json={
        "commit_sha": "A" * 40,
        "failure_category": "test",
        "failure_summary": "AssertionError: greeting should capitalize the name",
    })
    assert response.status_code == 202
    assert response.json()["queued"] is True
    with testing_sessions() as db:
        job = db.get(RepairJob, UUID(response.json()["job_id"]))
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "repair_job.manual_request_created"))
        assert job is not None and job.status == "QUEUED"
        assert job.commit_sha == "a" * 40 and job.workflow_run_id is None
        assert job.error_message is None and str(job.id) == str(queued[0])
        assert audit is not None and audit.user_id is not None


def test_manual_repair_rejects_unlinked_or_cross_tenant_repository(client: tuple[TestClient, sessionmaker]) -> None:
    test_client, testing_sessions = client
    register(test_client, "manual-first@example.com")
    org_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        repository = Repository(
            organization_id=org_id, installation_id=999,
            github_repo_id=1001, full_name="org/private", default_branch="main", private=True,
        )
        db.add(repository)
        db.commit()
        repo_id = repository.id
    body = {"commit_sha": "b" * 40, "failure_category": "unknown", "failure_summary": "Investigate this commit"}
    assert test_client.post(f"/api/v1/github/repositories/{repo_id}/repair", json=body).status_code == 409

    test_client.post("/api/v1/auth/logout")
    register(test_client, "manual-second@example.com")
    assert test_client.post(f"/api/v1/github/repositories/{repo_id}/repair", json=body).status_code == 404


def test_manual_repair_is_preserved_when_queue_is_unavailable(
    client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, testing_sessions = client
    register(test_client, "manual-queue-owner@example.com")
    org_id = UUID(test_client.get("/api/v1/auth/me").json()["organizations"][0]["id"])
    with testing_sessions() as db:
        db.add(GitHubInstallation(
            organization_id=org_id, installation_id=889,
            account_login="manual-queue", account_type="User", suspended=False,
        ))
        repository = Repository(
            organization_id=org_id, installation_id=889, github_repo_id=990,
            full_name="org/manual-queue", default_branch="main", private=True,
        )
        db.add(repository)
        db.commit()
        repo_id = repository.id

    def unavailable(_job_id):
        raise ConnectionError("redis is unavailable")

    monkeypatch.setattr(main_module, "enqueue_repair", unavailable)
    response = test_client.post(f"/api/v1/github/repositories/{repo_id}/repair", json={
        "commit_sha": "c" * 40, "failure_category": "unknown", "failure_summary": "Investigate test regression",
    })
    assert response.status_code == 202 and response.json()["queued"] is False
    with testing_sessions() as db:
        job = db.get(RepairJob, UUID(response.json()["job_id"]))
        assert job is not None and job.status == "QUEUED"
        assert job.queue_job_id is None and "queue unavailable" in job.error_message.lower()


def test_rate_limits_cover_auth_webhook_and_job_actions() -> None:
    assert rate_limit_rule("POST", "/api/v1/auth/login").limit == 10
    assert rate_limit_rule("POST", "/api/v1/auth/register").window_seconds == 3600
    assert rate_limit_rule("POST", "/api/v1/webhooks/github").limit == 600
    assert rate_limit_rule("POST", "/api/v1/repair-jobs/12345678-1234-1234-1234-123456789abc/cancel").limit == 20
    assert rate_limit_rule("POST", "/api/v1/github/repositories/12345678-1234-1234-1234-123456789abc/repair").limit == 10
    assert rate_limit_rule("GET", "/api/v1/dashboard/overview") is None


def test_rate_limit_counter_uses_atomic_expiring_redis_script(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    class FakeRedis:
        def eval(self, script, key_count, key, window):
            seen.update(script=script, key_count=key_count, key=key, window=window)
            return [4, 52]

    monkeypatch.setattr(rate_limit_module, "REDIS_CLIENT", FakeRedis())
    request = Request({"type": "http", "client": ("203.0.113.9", 54321)})
    count, ttl = asyncio.run(consume_rate_limit(request, RateLimitRule("login", 10, 300)))
    assert (count, ttl) == (4, 52)
    assert seen["key_count"] == 1 and seen["window"] == 300
    assert "INCR" in seen["script"] and "EXPIRE" in seen["script"]
    assert "203.0.113.9" not in seen["key"]


def test_rate_limited_request_returns_retry_after(
    client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, _ = client

    async def over_limit(_request, _rule):
        return 11, 37

    monkeypatch.setattr(main_module, "consume_rate_limit", over_limit)
    response = test_client.post("/api/v1/auth/login", headers={"Origin": settings.frontend_origin}, json={"email": "u@example.com", "password": "bad"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "37"
    assert response.headers["access-control-allow-origin"] == settings.frontend_origin


def test_production_rate_limit_fails_closed_if_redis_is_unavailable(
    client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, _ = client
    monkeypatch.setattr(settings, "app_env", "production")

    async def redis_unavailable(_request, _rule):
        from redis.exceptions import RedisError
        raise RedisError("connection unavailable")

    monkeypatch.setattr(main_module, "consume_rate_limit", redis_unavailable)
    response = test_client.post("/api/v1/auth/login", json={"email": "u@example.com", "password": "bad"})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"


def test_model_configuration_reports_status_without_returning_credentials(
    client: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, _ = client
    register(test_client, "model-user@example.com")
    monkeypatch.setattr(settings, "nebius_base_url", "https://tokenfactory.example/v1")
    monkeypatch.setattr(settings, "nebius_api_key", "do-not-return-this")
    monkeypatch.setattr(settings, "nebius_model", "nvidia/nemotron-test")
    response = test_client.get("/api/v1/model/config")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["model_id"] == "nvidia/nemotron-test"
    assert "api_key" not in response.json()
    assert "do-not-return-this" not in response.text
