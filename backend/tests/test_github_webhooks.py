import hashlib
import hmac
import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_module
from app.database import get_db
from app.github_webhooks import actionable_failure, classify_failure, redact_text, verify_signature
from app.main import app
from app.models import AuditLog, Base, GitHubInstallation, Organization, PullRequest, RepairJob, Repository, RepositorySetting, WebhookEvent
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


def signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_signature_verification_and_failure_classification() -> None:
    body = b'{"action":"completed"}'
    valid = signature(body, "test-secret")
    assert verify_signature(body, valid, "test-secret")
    assert not verify_signature(body, valid, "wrong-secret")
    assert not verify_signature(body, "sha1=bad", "test-secret")

    actionable, sha, summary, category = actionable_failure("workflow_run", {
        "action": "completed",
        "workflow_run": {"conclusion": "failure", "head_sha": "a" * 40, "name": "tests"},
    })
    assert actionable and sha == "a" * 40 and "tests" in summary and category == "test"
    assert not actionable_failure("workflow_run", {
        "action": "completed", "workflow_run": {"conclusion": "success", "head_sha": "b" * 40},
    })[0]
    redacted = redact_text("github_pat_abcdefghijklmnopqrstuvwxyz0123456789 and AKIA1234567890ABCDEF")
    assert "github_pat_" not in redacted and "AKIA1234567890ABCDEF" not in redacted
    assert classify_failure("pytest: AssertionError") == "test"
    assert classify_failure("ModuleNotFoundError: No module named 'httpx'") == "dependency"
    assert classify_failure("Invalid configuration: YAML error") == "configuration"
    assert classify_failure("Traceback (most recent call last): TypeError") == "runtime"
    assert classify_failure("ruff: lint failed") == "lint"
    assert classify_failure("Compilation failed") == "build"
    assert classify_failure("permission denied while downloading runner image") == "infrastructure"
    assert classify_failure("test passed on retry; intermittent failure") == "flaky"


def test_webhook_deduplicates_and_enqueues_only_linked_monitored_failures(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, testing_sessions = client_and_sessions
    secret = "local-test-webhook-secret"
    monkeypatch.setattr(settings, "github_webhook_secret", secret)
    monkeypatch.setattr(main_module, "enqueue_repair", lambda job_id: f"queue-{job_id}")
    with testing_sessions() as db:
        organization = Organization(name="Tenant A")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id,
            installation_id=55,
            account_login="tenant-a",
            account_type="Organization",
            suspended=False,
        ))
        db.add(Repository(
            organization_id=organization.id,
            installation_id=55,
            github_repo_id=91,
            full_name="tenant-a/project",
            default_branch="main",
            private=True,
            monitored=True,
        ))
        db.commit()

    payload = {
        "action": "completed",
        "installation": {"id": 55},
        "repository": {"id": 91, "full_name": "tenant-a/project"},
        "workflow_run": {"id": 777, "conclusion": "failure", "head_sha": "c" * 40, "name": "unit tests"},
    }
    body = json.dumps(payload).encode()
    headers = {
        "x-hub-signature-256": signature(body, secret),
        "x-github-delivery": "delivery-abc-123",
        "x-github-event": "workflow_run",
        "content-type": "application/json",
    }
    first = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    duplicate = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert first.status_code == 202 and first.json()["queued"] is True
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True

    with testing_sessions() as db:
        assert db.scalar(select(func.count(RepairJob.id))) == 1
        created = db.scalar(select(RepairJob))
        assert created is not None and created.status == "QUEUED"
        assert created.commit_sha == "c" * 40
        assert created.workflow_run_id == 777 and created.check_run_id is None


def test_push_webhook_audits_commit_metadata_without_triggering_repairs(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, testing_sessions = client_and_sessions
    secret = "local-test-webhook-secret"
    monkeypatch.setattr(settings, "github_webhook_secret", secret)
    with testing_sessions() as db:
        organization = Organization(name="Push audit")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id, installation_id=59,
            account_login="push-org", account_type="Organization", suspended=False,
        ))
        repository = Repository(
            organization_id=organization.id, installation_id=59, github_repo_id=95,
            full_name="push-org/project", default_branch="main", private=True, monitored=False,
        )
        db.add(repository)
        db.commit()
        repository_id = repository.id

    payload = {
        "ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40,
        "installation": {"id": 59}, "repository": {"id": 95},
        "commits": [{"id": "b" * 40}, {"id": "c" * 40}],
    }
    body = json.dumps(payload).encode()
    headers = {
        "x-hub-signature-256": signature(body, secret),
        "x-github-delivery": "push-delivery-001",
        "x-github-event": "push",
        "content-type": "application/json",
    }
    observed = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    duplicate = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert observed.status_code == 202 and observed.json()["commit_sha"] == "b" * 40
    assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
    with testing_sessions() as db:
        assert db.scalar(select(func.count(RepairJob.id))) == 0
        event = db.scalar(select(WebhookEvent).where(WebhookEvent.delivery_id == "push-delivery-001"))
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "github.push.observed"))
        assert event is not None and event.outcome == "push_observed"
        assert audit is not None and audit.entity_id == str(repository_id)
        assert audit.details == {"ref": "refs/heads/main", "before": "a" * 40, "after": "b" * 40, "commit_count": 2}


def test_missing_secret_and_invalid_signature_are_rejected(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = client_and_sessions
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    assert client.post("/api/v1/webhooks/github", content=b"{}").status_code == 503
    monkeypatch.setattr(settings, "github_webhook_secret", "configured")
    response = client.post("/api/v1/webhooks/github", content=b"{}", headers={
        "x-hub-signature-256": "sha256=bad",
        "x-github-delivery": "bad",
        "x-github-event": "workflow_run",
    })
    assert response.status_code == 401


def test_disabled_repository_trigger_records_event_without_creating_job(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, testing_sessions = client_and_sessions
    secret = "local-test-webhook-secret"
    monkeypatch.setattr(settings, "github_webhook_secret", secret)
    with testing_sessions() as db:
        organization = Organization(name="Trigger policy")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id, installation_id=57,
            account_login="trigger-org", account_type="Organization", suspended=False,
        ))
        repository = Repository(
            organization_id=organization.id, installation_id=57, github_repo_id=93,
            full_name="trigger-org/project", default_branch="main", private=True, monitored=True,
        )
        db.add(repository)
        db.flush()
        db.add(RepositorySetting(repository_id=repository.id, trigger_policy={
            "workflow_run_failure": False, "check_run_failure": True,
        }))
        db.commit()

    payload = {
        "action": "completed", "installation": {"id": 57}, "repository": {"id": 93},
        "workflow_run": {"id": 778, "conclusion": "failure", "head_sha": "e" * 40, "name": "tests"},
    }
    body = json.dumps(payload).encode()
    response = client.post("/api/v1/webhooks/github", content=body, headers={
        "x-hub-signature-256": signature(body, secret),
        "x-github-delivery": "trigger-disabled-delivery",
        "x-github-event": "workflow_run",
        "content-type": "application/json",
    })
    assert response.status_code == 202
    assert response.json()["repair_job_id"] is None
    with testing_sessions() as db:
        assert db.scalar(select(func.count(RepairJob.id))) == 0


def test_pull_request_webhook_tracks_only_linked_pull_requests(
    client_and_sessions: tuple[TestClient, sessionmaker], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, testing_sessions = client_and_sessions
    secret = "local-test-webhook-secret"
    monkeypatch.setattr(settings, "github_webhook_secret", secret)
    with testing_sessions() as db:
        organization = Organization(name="Tenant PR")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id,
            installation_id=56,
            account_login="tenant-pr",
            account_type="Organization",
            suspended=False,
        ))
        repository = Repository(
            organization_id=organization.id,
            installation_id=56,
            github_repo_id=92,
            full_name="tenant-pr/project",
            default_branch="main",
            private=True,
            monitored=True,
        )
        db.add(repository)
        db.flush()
        job = RepairJob(
            organization_id=organization.id,
            repository_id=repository.id,
            idempotency_key="webhook-pr-job",
            status="COMPLETED",
            commit_sha="d" * 40,
            base_branch="main",
            failure_category="test",
            failure_summary="Unit test failure",
            pr_url="https://github.com/tenant-pr/project/pull/8",
        )
        db.add(job)
        db.flush()
        tracked = PullRequest(
            repair_job_id=job.id,
            github_pr_id=808,
            number=8,
            url=job.pr_url,
            branch_name="patchwork/repair-123",
            status="OPEN",
        )
        db.add(tracked)
        db.commit()
        tracked_id = tracked.id

    def deliver(action: str, *, merged: bool, delivery_id: str, repo_id: int = 92) -> dict:
        payload = {
            "action": action,
            "installation": {"id": 56},
            "repository": {"id": repo_id, "full_name": "tenant-pr/project"},
            "pull_request": {"id": 808, "number": 8, "merged": merged},
        }
        body = json.dumps(payload).encode()
        response = client.post("/api/v1/webhooks/github", content=body, headers={
            "x-hub-signature-256": signature(body, secret),
            "x-github-delivery": delivery_id,
            "x-github-event": "pull_request",
            "content-type": "application/json",
        })
        assert response.status_code == 202
        return response.json()

    assert deliver("closed", merged=False, delivery_id="pr-closed")["accepted"] is True
    with testing_sessions() as db:
        assert db.get(PullRequest, tracked_id).status == "CLOSED"

    assert deliver("reopened", merged=False, delivery_id="pr-reopened")["accepted"] is True
    with testing_sessions() as db:
        assert db.get(PullRequest, tracked_id).status == "OPEN"

    assert deliver("closed", merged=True, delivery_id="pr-merged")["accepted"] is True
    with testing_sessions() as db:
        assert db.get(PullRequest, tracked_id).status == "MERGED"

    assert deliver("closed", merged=False, delivery_id="pr-unlinked", repo_id=999)["accepted"] is True
    with testing_sessions() as db:
        assert db.get(PullRequest, tracked_id).status == "MERGED"
