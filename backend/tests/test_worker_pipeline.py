import pytest
from threading import Event
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents.agents import validate_diff_policy as real_validate_diff_policy
from app.agents.nebius import InferenceResult
from app.agents.schemas import CoderOutput, RepoArchitectOutput
from app.validation import ValidationOutcome
from app.models import (
    AgentRun, Base, CodePatch, GitHubInstallation, MilisaCacheLookup,
    MilisaFixPattern, Organization, PullRequest, RepairJob, RepairJobEvent, Repository, RepositorySetting,
    ValidationResult, ValidationRun,
)
from app.milisa import cache_identity
from app.settings import settings
import app.worker as worker


@pytest.fixture(autouse=True)
def acquire_test_repair_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    class TestSlot:
        def __init__(self, *_args, **_kwargs):
            self.lost = Event()

        def acquire(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(worker, "OrganizationRepairSlot", TestSlot)


def test_context_failure_triage_uses_logs_before_agent_calls() -> None:
    assert worker._context_failure_category("Workflow tests failed", {
        "failure_log_excerpt": "Error: Resource temporarily unavailable while starting runner",
    }) == "infrastructure"
    assert worker._context_failure_category("Workflow tests failed", {
        "failure_log_excerpt": "test passed on retry after intermittent failure",
    }) == "flaky"
    assert worker._context_failure_category("Workflow tests failed", {
        "failure_log_excerpt": "AssertionError: expected 2, got 1",
    }) == "test"


def test_validation_retry_budget_allows_only_two_additional_attempts() -> None:
    assert worker._can_retry_validation("FAILED", 0, True)
    assert worker._can_retry_validation("TIMED_OUT", 1, True)
    assert not worker._can_retry_validation("FAILED", 2, True)
    assert not worker._can_retry_validation("TIMED_OUT", 3, True)
    assert not worker._can_retry_validation("ERROR", 0, True)
    assert not worker._can_retry_validation("FAILED", 0, False)


def test_worker_persists_proposal_and_stops_for_validation(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "create_installation_token", lambda installation_id, repository_id: "scoped-token")
    monkeypatch.setattr(settings, "github_app_id", "app-id")
    monkeypatch.setattr(settings, "github_private_key_path", "private-key")
    monkeypatch.setattr(settings, "nebius_base_url", "https://tokenfactory.example/v1")
    monkeypatch.setattr(settings, "nebius_api_key", "server-key")
    monkeypatch.setattr(settings, "nebius_model", "nvidia/nemotron-test")

    class FakeGitHub:
        def __init__(self, token):
            assert token == "scoped-token"

        def gather_context(self, *args):
            return {
                "source_files": {"app.py": "print('ok')"},
                "repository_files": ["app.py"],
                "languages": {"Python": 100},
                "failure_summary": "test failed",
            }

    class FakeNebius:
        pass

    usage = InferenceResult(content="", model_id="model", prompt_tokens=12, completion_tokens=8)
    architect = RepoArchitectOutput(
        failure_summary="A test failed", root_cause_hypotheses=["bad value"],
        supporting_evidence=["assertion"], relevant_files=["app.py"], relevant_symbols=[],
        recommended_patch_scope=["app.py"], tests_to_run=["pytest"], confidence=0.8,
        risk_assessment="Low",
    )
    coder = CoderOutput(
        root_cause="bad value", explanation="Corrected the value.",
        unified_diff="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new",
        regression_test_plan=["add a regression case"], tests_to_run=["pytest"], risk_notes=[],
    )
    monkeypatch.setattr(worker, "GitHubRepositoryClient", FakeGitHub)
    monkeypatch.setattr(worker, "NebiusClient", FakeNebius)
    monkeypatch.setattr(worker, "run_repo_architect", lambda client, context: (architect, usage))
    monkeypatch.setattr(worker, "run_coder", lambda *args, **kwargs: (coder, usage))

    with sessions() as db:
        organization = Organization(name="Acme")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id, installation_id=7,
            account_login="acme", account_type="Organization", suspended=False,
        ))
        repository = Repository(
            organization_id=organization.id, installation_id=7, github_repo_id=99,
            full_name="acme/project", default_branch="main", private=True, monitored=True,
        )
        db.add(repository)
        db.flush()
        job = RepairJob(
            organization_id=organization.id, repository_id=repository.id,
            idempotency_key="delivery-1", status="QUEUED", commit_sha="a" * 40,
            base_branch="main", failure_category="test", failure_summary="pytest failed",
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    worker.process_repair_job(job_id)

    with sessions() as db:
        job = db.scalar(select(RepairJob))
        assert job is not None and job.status == "NEEDS_HUMAN_REVIEW"
        assert "validation" in (job.error_message or "").lower()
        assert db.scalar(select(CodePatch)) is not None
        assert db.scalar(select(MilisaCacheLookup)).outcome == "MISS"
        assert len(db.scalars(select(AgentRun)).all()) == 2
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.parametrize(
    "cache_hit,validation_statuses",
    [(False, ["PASSED"]), (True, ["PASSED"]), (False, ["FAILED", "FAILED", "FAILED"])],
    ids=["cache-miss", "verified-cache-hit", "retry-exhaustion"],
)
def test_worker_completes_only_after_passed_validation_and_persisted_pr(
    monkeypatch, cache_hit: bool, validation_statuses: list[str],
) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(
        worker, "create_installation_token",
        lambda installation_id, repository_id, *, write_access=False: "write-token" if write_access else "read-token",
    )
    monkeypatch.setattr(settings, "github_app_id", "app-id")
    monkeypatch.setattr(settings, "github_private_key_path", "private-key")
    monkeypatch.setattr(settings, "nebius_base_url", "https://tokenfactory.example/v1")
    monkeypatch.setattr(settings, "nebius_api_key", "server-key")
    monkeypatch.setattr(settings, "nebius_model", "nvidia/nemotron-test")
    monkeypatch.setattr(settings, "validation_image", "trusted/python:3.12")

    class FakeGitHub:
        def __init__(self, token):
            self.token = token

        def gather_context(self, *args):
            return {
                "source_files": {"app.py": "def value(): return 'old'\n"},
                "repository_files": ["app.py"],
                "languages": {"Python": 100},
                "failure_summary": "AssertionError: expected new value",
                "failing_test_selector": "tests/test_app.py::test_value",
            }

        def download_source_archive(self, _full_name, _commit_sha, destination):
            (destination / "app.py").write_text("def value(): return 'old'\n", encoding="utf-8")

        def create_repair_pull_request(self, **kwargs):
            assert self.token == "write-token"
            assert kwargs["validation_checks"][0]["outcome"] == "PASSED"
            return {
                "id": 987, "number": 12,
                "url": "https://github.com/acme/project/pull/12",
                "branch": "patchwork/repair-123",
                "commit_sha": "f" * 40,
            }

    usage = InferenceResult(content="", model_id="model", prompt_tokens=20, completion_tokens=10)
    architect = RepoArchitectOutput(
        failure_summary="A test expected another value", root_cause_hypotheses=["incorrect value"],
        supporting_evidence=["assertion output"], relevant_files=["app.py"], relevant_symbols=["value"],
        recommended_patch_scope=["app.py"], tests_to_run=["tests/test_app.py::test_value"],
        confidence=0.9, risk_assessment="Low",
    )
    coder = CoderOutput(
        root_cause="The function returns the wrong value.",
        explanation="Updated the return value to match the expected behavior.",
        unified_diff=(
            "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1 @@\n-def value(): return 'old'\n+def value(): return 'new'"
        ),
        regression_test_plan=["Cover the corrected value."],
        tests_to_run=["tests/test_app.py::test_value"], risk_notes=[],
    )
    monkeypatch.setattr(worker, "GitHubRepositoryClient", FakeGitHub)
    monkeypatch.setattr(worker, "NebiusClient", lambda: object())
    def architect_call(_client, _context):
        assert not cache_hit, "verified cache hit should bypass the agents"
        return architect, usage

    coder_feedback = []

    def coder_call(*_args, **kwargs):
        assert not cache_hit, "verified cache hit should bypass the agents"
        coder_feedback.append(kwargs.get("repair_feedback", ""))
        return coder, usage

    monkeypatch.setattr(worker, "run_repo_architect", architect_call)
    monkeypatch.setattr(worker, "run_coder", coder_call)
    patch_scope_checks = []

    def validate_with_repository_scope(diff, **kwargs):
        patch_scope_checks.append(kwargs.get("repository_allowed_paths"))
        return real_validate_diff_policy(diff, **kwargs)

    monkeypatch.setattr(worker, "validate_diff_policy", validate_with_repository_scope)
    validation_calls = []

    def passed_validation(_source, _patch, *, cancelled, test_selector, required_checks):
        attempt_index = len(validation_calls)
        validation_calls.append(test_selector)
        assert required_checks == ["ruff"]
        status = validation_statuses[attempt_index]
        check_outcome = "PASSED" if status == "PASSED" else "FAILED"
        return ValidationOutcome(
            status=status, log_excerpt=f"diagnostic-{attempt_index + 1}", duration_ms=120,
            environment={"runtime": "docker", "network": "disabled"},
            checks=[
                {"name": "Apply patch", "outcome": check_outcome},
                {"name": "Original failing test", "outcome": check_outcome},
                {"name": "Pytest suite", "outcome": check_outcome},
                {"name": "Ruff lint", "outcome": check_outcome},
            ],
        )

    monkeypatch.setattr(worker, "run_python_validation", passed_validation)
    monkeypatch.setattr(worker.subprocess, "run", lambda *_args, **_kwargs: None)

    with sessions() as db:
        organization = Organization(name="Acme")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id, installation_id=7,
            account_login="acme", account_type="Organization", suspended=False,
        ))
        repository = Repository(
            organization_id=organization.id, installation_id=7, github_repo_id=99,
            full_name="acme/project", default_branch="main", private=True, monitored=True,
        )
        db.add(repository)
        db.flush()
        db.add(RepositorySetting(repository_id=repository.id, required_checks=["ruff"], allowed_paths=["app.py"]))
        job = RepairJob(
            organization_id=organization.id, repository_id=repository.id,
            idempotency_key="delivery-success", status="QUEUED", commit_sha="c" * 40,
            base_branch="main", failure_category="test", failure_summary="AssertionError: expected new value",
        )
        db.add(job)
        db.flush()
        if cache_hit:
            context = {
                "source_files": {"app.py": "def value(): return 'old'\n"},
                "repository_files": ["app.py"],
                "languages": {"Python": 100},
                "failure_summary": "AssertionError: expected new value",
                "failing_test_selector": "tests/test_app.py::test_value",
            }
            db.add(MilisaFixPattern(
                organization_id=organization.id,
                **cache_identity(job, context),
                failure_category="test",
                root_cause="The cached patch corrects the value.",
                patch_template=coder.unified_diff,
                validation_metadata={"status": "PASSED", "agent_tokens": 75},
                verification_status="VERIFIED",
                success_count=1,
            ))
        db.commit()
        job_id = str(job.id)

    worker.process_repair_job(job_id)

    with sessions() as db:
        job = db.scalar(select(RepairJob))
        pull = db.scalar(select(PullRequest))
        pattern = db.scalar(select(MilisaFixPattern))
        validation_runs = db.scalars(select(ValidationRun).order_by(ValidationRun.attempt)).all()
        agent_names = [run.agent_name for run in db.scalars(select(AgentRun)).all()]
        assert len(validation_runs) == len(validation_statuses)
        assert [run.attempt for run in validation_runs] == list(range(1, len(validation_statuses) + 1))
        assert len(db.scalars(select(ValidationResult)).all()) == 4 * len(validation_statuses)
        lookup = db.scalar(select(MilisaCacheLookup))
        assert lookup is not None and lookup.outcome == ("VALIDATED_HIT" if cache_hit else "MISS")
        assert validation_calls == ["tests/test_app.py::test_value"] * len(validation_statuses)
        assert patch_scope_checks == [["app.py"]] * len(validation_statuses)
        if validation_statuses[-1] == "PASSED":
            assert job is not None and job.status == "COMPLETED"
            assert job.pr_url == "https://github.com/acme/project/pull/12"
            assert pull is not None and pull.number == 12 and pull.status == "OPEN"
            assert pattern is not None and pattern.verification_status == "VERIFIED"
            assert pattern.regression_reference == "tests/test_app.py::test_value"
            assert pattern.target_revision == "f" * 40
            assert pattern.expires_at is not None
            assert pattern.validation_metadata["agent_tokens"] == (75 if cache_hit else 60)
            assert lookup.estimated_tokens_saved == (75 if cache_hit else 0)
            assert lookup.fix_pattern_id == pattern.id
            if not cache_hit:
                assert pattern.source_revision == "c" * 40
            expected_agents = {"validation_agent"} if cache_hit else {"repo_architect", "coder", "validation_agent"}
            assert set(agent_names) == expected_agents
            assert pattern.reuse_count == (1 if cache_hit else 0)
            assert coder_feedback == ([""] if not cache_hit else [])
        else:
            assert job is not None and job.status == "NEEDS_HUMAN_REVIEW"
            assert job.retry_count == 2 and job.pr_url is None and pull is None
            assert pattern is None and lookup.fix_pattern_id is None
            job_events = db.scalars(select(RepairJobEvent)).all()
            assert sum(event.to_state == "RETRYING" for event in job_events) == 2
            assert agent_names.count("repo_architect") == 1
            assert agent_names.count("coder") == 3
            assert agent_names.count("validation_agent") == 3
            assert coder_feedback == ["", "diagnostic-1", "diagnostic-2"]
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_worker_parks_infrastructure_failure_from_logs_before_agents(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "create_installation_token", lambda installation_id, repository_id: "scoped-token")
    monkeypatch.setattr(settings, "github_app_id", "app-id")
    monkeypatch.setattr(settings, "github_private_key_path", "private-key")
    monkeypatch.setattr(settings, "nebius_base_url", "https://tokenfactory.example/v1")
    monkeypatch.setattr(settings, "nebius_api_key", "server-key")
    monkeypatch.setattr(settings, "nebius_model", "nvidia/nemotron-test")

    class FakeGitHub:
        def __init__(self, token):
            assert token == "scoped-token"

        def gather_context(self, *args):
            return {
                "source_files": {"app.py": "print('ok')"},
                "failure_log_excerpt": "Runner lost: resource temporarily unavailable",
                "repository_files": ["app.py"],
                "languages": {"Python": 100},
            }

    monkeypatch.setattr(worker, "GitHubRepositoryClient", FakeGitHub)
    monkeypatch.setattr(worker, "NebiusClient", lambda: object())
    monkeypatch.setattr(worker, "run_repo_architect", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent must not run")))

    with sessions() as db:
        organization = Organization(name="Acme")
        db.add(organization)
        db.flush()
        db.add(GitHubInstallation(
            organization_id=organization.id, installation_id=7,
            account_login="acme", account_type="Organization", suspended=False,
        ))
        repository = Repository(
            organization_id=organization.id, installation_id=7, github_repo_id=99,
            full_name="acme/project", default_branch="main", private=True, monitored=True,
        )
        db.add(repository)
        db.flush()
        job = RepairJob(
            organization_id=organization.id, repository_id=repository.id,
            idempotency_key="delivery-infra", status="QUEUED", commit_sha="b" * 40,
            base_branch="main", failure_category="test", failure_summary="Workflow tests failed",
        )
        db.add(job)
        db.commit()
        job_id = str(job.id)

    worker.process_repair_job(job_id)

    with sessions() as db:
        job = db.scalar(select(RepairJob))
        assert job is not None
        assert job.status == "NEEDS_HUMAN_REVIEW"
        assert job.failure_category == "infrastructure"
        assert "no code patch" in (job.error_message or "").lower()
        assert db.scalars(select(AgentRun)).all() == []
        assert db.scalars(select(CodePatch)).all() == []
    Base.metadata.drop_all(engine)
    engine.dispose()
