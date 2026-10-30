from hashlib import sha256
from pathlib import Path
import tempfile
import subprocess
from datetime import datetime, timedelta, timezone
from uuid import UUID
from collections.abc import Callable

from redis import Redis
from rq import Queue, Worker
from sqlalchemy import select

from app.agents.agents import run_coder, run_repo_architect, validate_diff_policy, validate_repository_patch_paths
from app.agents.millisa import MillisaClient
from app.agents.nebius import NebiusClient
from app.agents.service import run_and_record_agent
from app.database import SessionLocal
from app.concurrency import OrganizationRepairSlot
from app.github_app import GitHubConnectionError, create_installation_token
from app.github_repo import GitHubRepositoryClient, GitHubRepositoryError
from app.github_webhooks import classify_failure
from app.job_state import TRANSITIONS, transition_job
from app.models import AgentRun, AuditLog, CodePatch, MilisaCacheLookup, MilisaFixPattern, PullRequest, RepairJob, RepairJobEvent, Repository, RepositorySetting, ValidationResult, ValidationRun
from app.milisa import lookup_verified_fix, record_verified_fix
from app.queueing import enqueue_repair_after_capacity_wait
from app.settings import settings
from app.validation import ValidationOutcome, run_python_validation

RECOVERABLE_STATES = {
    "TRIAGING", "CONTEXT_GATHERING", "CACHE_LOOKUP", "PLANNING", "PATCH_GENERATION",
    "SANDBOX_VALIDATION", "RETRYING", "POLICY_CHECK", "PULL_REQUEST_CREATION",
}


def _context_failure_category(failure_summary: str, context: dict) -> str:
    evidence = "\n".join((failure_summary, str(context.get("failure_log_excerpt") or "")))
    return classify_failure(evidence)


def _can_retry_validation(status: str, retry_count: int, has_architect: bool) -> bool:
    return status in {"FAILED", "TIMED_OUT"} and retry_count < 2 and has_architect


def _required_validation_checks(setting: RepositorySetting | None) -> list[str]:
    allowed = {"ruff", "mypy", "build"}
    configured = setting.required_checks if setting else []
    selected = {item for item in configured if isinstance(item, str) and item in allowed} if isinstance(configured, list) else set()
    return [item for item in ("ruff", "mypy", "build") if item in selected]


def recover_stale_jobs() -> int:
    """Park jobs interrupted by a dead worker while preserving all saved artifacts."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=20)
    recovered = 0
    with SessionLocal() as db:
        jobs = db.scalars(select(RepairJob).where(
            RepairJob.status.in_(RECOVERABLE_STATES),
            RepairJob.updated_at < cutoff,
        ).with_for_update(skip_locked=True)).all()
        for job in jobs:
            previous_state = job.status
            if "NEEDS_HUMAN_REVIEW" not in TRANSITIONS.get(previous_state, set()):
                continue
            transition_job(
                db, job.id, "NEEDS_HUMAN_REVIEW", actor="worker-recovery",
                detail={"reason": "stale_worker_recovery", "interrupted_state": previous_state},
            )
            job.error_message = "Worker stopped before the repair finished; saved patches and validation records were preserved."
            abandoned_runs = db.scalars(select(AgentRun).where(
                AgentRun.repair_job_id == job.id,
                AgentRun.status == "RUNNING",
            )).all()
            for run in abandoned_runs:
                run.status = "ABANDONED"
                run.output = {"error": "Worker stopped before the agent run completed."}
                run.finished_at = datetime.now(timezone.utc)
            db.add(AuditLog(
                organization_id=job.organization_id,
                action="repair_job.stale_worker_recovered",
                entity_type="repair_job",
                entity_id=str(job.id),
                details={"interrupted_state": previous_state},
            ))
            recovered += 1
        db.commit()
    return recovered


def _park_for_review(db, job: RepairJob, reason: str) -> None:
    if "NEEDS_HUMAN_REVIEW" in TRANSITIONS.get(job.status, set()):
        transition_job(db, job.id, "NEEDS_HUMAN_REVIEW", actor="worker", detail={"reason": reason[:500]})
        job.error_message = reason[:500]
        db.commit()


def _validate_candidate(
    db, job: RepairJob, repository: Repository, github: GitHubRepositoryClient, diff: str,
    *, test_selector: str | None = None, required_checks: list[str] | None = None,
    slot_lost: Callable[[], bool] | None = None,
) -> ValidationOutcome:
    transition_job(db, job.id, "SANDBOX_VALIDATION", actor="worker")
    db.commit()
    started_at = datetime.now(timezone.utc)
    outcome: ValidationOutcome
    try:
        with tempfile.TemporaryDirectory(prefix="repair-validation-") as temp_path:
            root = Path(temp_path)
            source_path = root / "repository"
            source_path.mkdir()
            patch_path = root / "patch.diff"
            patch_path.write_text(diff, encoding="utf-8")
            github.download_source_archive(repository.full_name, job.commit_sha, source_path)
            outcome = run_python_validation(
                source_path, patch_path,
                cancelled=lambda: _job_is_cancelled(job.id) or bool(slot_lost and slot_lost()),
                test_selector=test_selector,
                required_checks=required_checks or [],
            )
    except Exception:
        outcome = ValidationOutcome(
            status="ERROR", log_excerpt="Validation workspace could not be prepared.",
            duration_ms=0, environment={}, checks=[],
        )
    validation = ValidationRun(
        repair_job_id=job.id,
        attempt=job.retry_count + 1,
        status=outcome.status,
        environment=outcome.environment,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        log_excerpt=outcome.log_excerpt[:20_000],
    )
    db.add(validation)
    db.flush()
    db.add(AgentRun(
        repair_job_id=job.id,
        agent_name="validation_agent",
        status="CANCELLED" if outcome.status == "CANCELLED" else ("SUCCEEDED" if outcome.status in {"PASSED", "FAILED"} else "FAILED"),
        input_summary=("Fixed patch-apply, Python compileall, pytest, and configured allowlisted checks "
                       "in an isolated Docker container."),
        output={"status": outcome.status, "checks": outcome.checks, "environment": outcome.environment},
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
    ))
    for check in outcome.checks:
        db.add(ValidationResult(
            validation_run_id=validation.id,
            check_name=check["name"],
            outcome=check["outcome"],
            duration_ms=outcome.duration_ms if check["name"] == "Pytest" else None,
            details={},
        ))
    db.commit()
    return outcome


def _job_is_cancelled(job_id: UUID) -> bool:
    with SessionLocal() as monitor_db:
        monitored_job = monitor_db.get(RepairJob, job_id)
        return bool(monitored_job and monitored_job.status == "CANCELLED")


def _create_pull_request(
    db,
    job: RepairJob,
    repository: Repository,
    *,
    diff: str,
    changed_paths: list[str],
    failure_summary: str,
    root_cause: str,
    explanation: str,
    validation_checks: list[dict[str, str]],
    validation_environment: dict,
    verified_pattern=None,
    transition_state: bool = True,
) -> None:
    if transition_state:
        transition_job(db, job.id, "PULL_REQUEST_CREATION", actor="worker")
        db.commit()
    try:
        write_token = create_installation_token(
            repository.installation_id, repository.github_repo_id, write_access=True,
        )
        with tempfile.TemporaryDirectory(prefix="repair-pr-") as temp_path:
            root = Path(temp_path)
            source_path = root / "repository"
            source_path.mkdir()
            patch_path = root / "patch.diff"
            patch_path.write_text(diff, encoding="utf-8")
            read_client = GitHubRepositoryClient(create_installation_token(
                repository.installation_id, repository.github_repo_id,
            ))
            read_client.download_source_archive(repository.full_name, job.commit_sha, source_path)
            try:
                subprocess.run(
                    ["git", "apply", "--check", str(patch_path)],
                    cwd=source_path, capture_output=True, timeout=20, check=True, shell=False,
                )
                subprocess.run(
                    ["git", "apply", str(patch_path)],
                    cwd=source_path, capture_output=True, timeout=20, check=True, shell=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("Validated patch could not be reconstructed") from exc
            writer = GitHubRepositoryClient(write_token)
            pull = writer.create_repair_pull_request(
                full_name=repository.full_name,
                base_sha=job.commit_sha,
                base_branch=job.base_branch,
                repair_id=str(job.id),
                changed_paths=changed_paths,
                worktree=source_path,
                failure_summary=failure_summary,
                root_cause=root_cause,
                explanation=explanation,
                validation_checks=validation_checks,
                validation_environment=validation_environment,
                retry_count=job.retry_count,
            )
        target_revision = pull.get("commit_sha")
        if verified_pattern is not None and isinstance(target_revision, str) and len(target_revision) in {40, 64}:
            verified_pattern.target_revision = target_revision
        record = PullRequest(
            repair_job_id=job.id,
            github_pr_id=int(pull["id"]),
            number=int(pull["number"]),
            url=str(pull["url"]),
            branch_name=str(pull["branch"]),
            status="OPEN",
        )
        db.add(record)
        db.flush()
        job.pr_url = record.url
        transition_job(db, job.id, "COMPLETED", actor="worker", detail={"pull_request_number": record.number})
        db.add(AuditLog(
            organization_id=job.organization_id,
            action="repair_job.pull_request_created",
            entity_type="repair_job",
            entity_id=str(job.id),
            details={"pull_request_number": record.number, "url": record.url},
        ))
        db.commit()
    except Exception:
        db.rollback()
        db.refresh(job)
        _park_for_review(db, job, "Validation passed, but GitHub could not create and persist a repair pull request. Check repository write permissions and service diagnostics.")


def _defer_for_organization_capacity(job_id: UUID) -> None:
    deferred_id = enqueue_repair_after_capacity_wait(job_id)
    with SessionLocal() as db:
        job = db.scalar(select(RepairJob).where(RepairJob.id == job_id).with_for_update())
        if not job or job.status != "QUEUED":
            return
        previous_queue_id = job.queue_job_id
        job.queue_job_id = deferred_id
        db.add(RepairJobEvent(
            repair_job_id=job.id, from_state="QUEUED", to_state="QUEUED",
            actor="worker-concurrency",
            detail={
                "reason": "organization_concurrency_limit",
                "retry_delay_seconds": settings.repair_queue_wait_seconds,
                "previous_queue_job_id": previous_queue_id,
            },
        ))
        db.commit()


def process_repair_job(job_id: str) -> None:
    """Run one repair pipeline while holding an organization-scoped Redis slot."""
    try:
        repair_id = UUID(job_id)
    except ValueError:
        return
    with SessionLocal() as db:
        job = db.get(RepairJob, repair_id)
        if not job or job.status != "QUEUED":
            return
        organization_id = str(job.organization_id)

    connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    slot = OrganizationRepairSlot(
        connection, organization_id,
        settings.repair_concurrency_per_org, settings.repair_slot_lease_seconds,
    )
    acquired = False
    try:
        acquired = slot.acquire()
        if not acquired:
            _defer_for_organization_capacity(repair_id)
            return
        _process_repair_job(job_id, slot_lost=slot.lost.is_set)
    except Exception:
        if acquired:
            raise
        with SessionLocal() as db:
            job = db.get(RepairJob, repair_id)
            if job and job.status == "QUEUED":
                _park_for_review(db, job, "Repair concurrency protection or delayed queue is unavailable.")
    finally:
        if acquired:
            slot.release()
        connection.close()


def _process_repair_job(job_id: str, *, slot_lost: Callable[[], bool] | None = None) -> None:
    """Gather evidence, validate a bounded repair proposal, and publish a draft PR."""
    with SessionLocal() as db:
        try:
            job = db.scalar(select(RepairJob).where(RepairJob.id == UUID(job_id)).with_for_update())
        except ValueError:
            return
        if not job or job.status != "QUEUED":
            return
        repository = db.get(Repository, job.repository_id)
        if not repository or not repository.monitored:
            _park_for_review(db, job, "Repository is missing or no longer monitored.")
            return
        repository_settings = db.get(RepositorySetting, repository.id)
        required_checks = _required_validation_checks(repository_settings)
        try:
            repository_allowed_paths = (
                validate_repository_patch_paths(repository_settings.allowed_paths)
                if repository_settings and repository_settings.allowed_paths else None
            )
        except (TypeError, ValueError):
            _park_for_review(db, job, "Repository patch scope is invalid; an owner or admin must update it before repairs can continue.")
            return

        transition_job(db, job.id, "TRIAGING", actor="worker")
        job.error_message = None
        db.commit()

        if job.failure_category == "infrastructure" or any(
            marker in job.failure_summary.lower()
            for marker in ("flaky", "flakiness", "non-deterministic", "passed on retry")
        ):
            _park_for_review(db, job, "Failure appears infrastructure-related or flaky; no code patch was generated.")
            return

        if not settings.nebius_api_key or not settings.nebius_model or not settings.nebius_base_url:
            _park_for_review(db, job, "Nebius inference is not configured; diagnosis was not attempted.")
            return
        if not settings.github_app_id or not settings.github_private_key_path:
            _park_for_review(db, job, "GitHub App credentials are not configured; repository context was not fetched.")
            return

        try:
            token = create_installation_token(repository.installation_id, repository.github_repo_id)
            github = GitHubRepositoryClient(token)
            transition_job(db, job.id, "CONTEXT_GATHERING", actor="worker")
            db.commit()
            context = github.gather_context(
                repository.full_name,
                job.commit_sha,
                job.failure_summary,
                job.workflow_run_id,
            )
        except (GitHubConnectionError, GitHubRepositoryError) as exc:
            db.refresh(job)
            _park_for_review(db, job, str(exc))
            return
        except Exception:
            db.refresh(job)
            _park_for_review(db, job, "Repository context gathering failed; inspect service diagnostics.")
            return

        db.refresh(job)
        if job.status != "CONTEXT_GATHERING":
            return
        if slot_lost and slot_lost():
            _park_for_review(db, job, "Repair concurrency lease was lost; the repair was stopped for human review.")
            return
        observed_category = _context_failure_category(job.failure_summary, context)
        if observed_category in {"infrastructure", "flaky"}:
            job.failure_category = observed_category
            db.commit()
            _park_for_review(db, job, f"Failure evidence indicates a {observed_category} issue; no code patch was generated.")
            return
        if observed_category != "unknown" and job.failure_category != observed_category:
            job.failure_category = observed_category
            db.commit()
        transition_job(db, job.id, "CACHE_LOOKUP", actor="worker")
        pattern = lookup_verified_fix(db, job, context)
        lookup_event = db.scalar(select(RepairJobEvent).where(
            RepairJobEvent.repair_job_id == job.id,
            RepairJobEvent.to_state == "CACHE_LOOKUP",
        ).order_by(RepairJobEvent.created_at.desc()))
        if lookup_event:
            lookup_event.detail = {"outcome": "HIT" if pattern else "MISS"}
            db.commit()
        db.refresh(job)
        if job.status != "CACHE_LOOKUP":
            return

        try:
            client = NebiusClient()
            if pattern:
                transition_job(db, job.id, "PATCH_GENERATION", actor="worker", detail={"source": "verified_cache", "pattern_id": str(pattern.id)})
                db.commit()
                architect = None
                coder = None
                proposed_diff = pattern.patch_template
                explanation = pattern.root_cause
            else:
                transition_job(db, job.id, "PLANNING", actor="worker", detail={"cache": "miss"})
                db.commit()
                architect = run_and_record_agent(
                    db, job, agent_name="repo_architect",
                    input_summary=f"{repository.full_name}@{job.commit_sha}: {job.failure_summary}",
                    execute=lambda: run_repo_architect(client, context),
                )
                db.refresh(job)
                if job.status != "PLANNING":
                    return
                if slot_lost and slot_lost():
                    _park_for_review(db, job, "Repair concurrency lease was lost; the repair was stopped for human review.")
                    return
                coder_client = MillisaClient() if settings.millisa_api_key else client
                transition_job(db, job.id, "PATCH_GENERATION", actor="worker")
                db.commit()
                coder = run_and_record_agent(
                    db, job, agent_name="coder",
                    input_summary=f"{repository.full_name}@{job.commit_sha}: {architect.failure_summary}",
                    execute=lambda: run_coder(
                        coder_client,
                        failure_context=context,
                        architect=architect,
                        source_files=context["source_files"],
                    ),
                )
                proposed_diff = coder.unified_diff
                explanation = coder.explanation
            db.refresh(job)
            if job.status != "PATCH_GENERATION":
                return
            if slot_lost and slot_lost():
                _park_for_review(db, job, "Repair concurrency lease was lost; the repair was stopped for human review.")
                return
            changed_paths = validate_diff_policy(
                proposed_diff,
                allowed_paths=architect.recommended_patch_scope if architect else context.get("repository_files", []),
                existing_paths=context.get("repository_files", []),
                repository_allowed_paths=repository_allowed_paths,
            )
            db.add(CodePatch(
                repair_job_id=job.id,
                diff=proposed_diff,
                explanation=explanation,
                patch_hash=sha256(proposed_diff.encode("utf-8")).hexdigest(),
                approved=False,
            ))
            db.commit()
            if not settings.validation_image:
                _park_for_review(db, job, "Patch proposal passed basic scope checks; a trusted validation image is not configured.")
                return

            feedback = ""
            while True:
                selector = context.get("failing_test_selector")
                outcome = _validate_candidate(
                    db, job, repository, github, proposed_diff,
                    test_selector=selector if isinstance(selector, str) else None,
                    required_checks=required_checks,
                    slot_lost=slot_lost,
                )
                db.refresh(job)
                if job.status != "SANDBOX_VALIDATION":
                    return
                if pattern:
                    cache_lookup = db.scalar(select(MilisaCacheLookup).where(
                        MilisaCacheLookup.repair_job_id == job.id,
                    ).order_by(MilisaCacheLookup.created_at.desc()).limit(1))
                    if cache_lookup:
                        cache_lookup.outcome = "VALIDATED_HIT" if outcome.status == "PASSED" else "HIT_VALIDATION_FAILED"
                        db.commit()
                if outcome.status == "PASSED":
                    if slot_lost and slot_lost():
                        _park_for_review(db, job, "Repair concurrency lease was lost after validation; the repair was stopped before publishing a pull request.")
                        return
                    transition_job(db, job.id, "POLICY_CHECK", actor="worker", detail={"result": "passed"})
                    db.commit()
                    verified_pattern = record_verified_fix(
                        db, job, context, diff=proposed_diff,
                        root_cause=coder.root_cause if coder else pattern.root_cause,
                        checks=outcome.checks,
                        validation_environment=outcome.environment,
                        regression_reference=(
                            selector if isinstance(selector, str)
                            else next((path for path in changed_paths if path.startswith("tests/") or path.rsplit("/", 1)[-1].startswith("test_")), None)
                        ),
                        reused_pattern=pattern,
                    )
                    cache_lookup = db.scalar(select(MilisaCacheLookup).where(
                        MilisaCacheLookup.repair_job_id == job.id,
                    ).order_by(MilisaCacheLookup.created_at.desc()).limit(1))
                    if cache_lookup:
                        cache_lookup.fix_pattern_id = verified_pattern.id
                        db.commit()
                    _create_pull_request(
                        db, job, repository, diff=proposed_diff,
                        changed_paths=changed_paths, failure_summary=job.failure_summary,
                        root_cause=coder.root_cause if coder else pattern.root_cause,
                        explanation=coder.explanation if coder else pattern.root_cause,
                        validation_checks=outcome.checks,
                        validation_environment=outcome.environment,
                        verified_pattern=verified_pattern,
                    )
                    return
                if not _can_retry_validation(outcome.status, job.retry_count, architect is not None):
                    _park_for_review(db, job, f"Sandbox validation ended with {outcome.status.lower()}; human review is required.")
                    return

                transition_job(db, job.id, "RETRYING", actor="worker", detail={"attempt": job.retry_count + 1, "result": outcome.status})
                job.retry_count += 1
                db.commit()
                feedback = outcome.log_excerpt[-12_000:]
                transition_job(db, job.id, "PATCH_GENERATION", actor="worker")
                db.commit()
                coder = run_and_record_agent(
                    db, job, agent_name="coder",
                    input_summary=f"Repair attempt {job.retry_count} for {repository.full_name}@{job.commit_sha}",
                    execute=lambda: run_coder(
                        coder_client,
                        failure_context=context,
                        architect=architect,
                        source_files=context["source_files"],
                        repair_feedback=feedback,
                    ),
                )
                db.refresh(job)
                if job.status != "PATCH_GENERATION":
                    return
                proposed_diff = coder.unified_diff
                changed_paths = validate_diff_policy(
                    proposed_diff,
                    allowed_paths=architect.recommended_patch_scope,
                    existing_paths=context.get("repository_files", []),
                    repository_allowed_paths=repository_allowed_paths,
                )
                db.add(CodePatch(
                    repair_job_id=job.id,
                    diff=proposed_diff,
                    explanation=coder.explanation,
                    patch_hash=sha256(proposed_diff.encode("utf-8")).hexdigest(),
                    approved=False,
                ))
                db.commit()
        except Exception as exc:
            db.refresh(job)
            _park_for_review(
                db, job,
                f"Agent pipeline stopped ({type(exc).__name__}); inspect the recorded agent run and service diagnostics.",
            )


def process_pull_request_retry(job_id: str) -> None:
    """Retry GitHub publication from a previously validated, persisted patch."""
    with SessionLocal() as db:
        try:
            job = db.scalar(select(RepairJob).where(RepairJob.id == UUID(job_id)).with_for_update())
        except ValueError:
            return
        if not job or job.status != "PULL_REQUEST_CREATION" or job.pr_url:
            return
        repository = db.get(Repository, job.repository_id)
        repository_settings = db.get(RepositorySetting, repository.id) if repository else None
        patch = db.scalar(select(CodePatch).where(
            CodePatch.repair_job_id == job.id,
        ).order_by(CodePatch.created_at.desc()).limit(1))
        validation = db.scalar(select(ValidationRun).where(
            ValidationRun.repair_job_id == job.id,
        ).order_by(ValidationRun.attempt.desc()).limit(1))
        if not repository or not patch or not validation or validation.status != "PASSED":
            _park_for_review(db, job, "The validated patch or passing validation record is unavailable.")
            return
        try:
            cache_lookup = db.scalar(select(MilisaCacheLookup).where(
                MilisaCacheLookup.repair_job_id == job.id,
            ).order_by(MilisaCacheLookup.created_at.desc()).limit(1))
            verified_pattern = db.get(MilisaFixPattern, cache_lookup.fix_pattern_id) if cache_lookup and cache_lookup.fix_pattern_id else None
            repository_allowed_paths = (
                validate_repository_patch_paths(repository_settings.allowed_paths)
                if repository_settings and repository_settings.allowed_paths else None
            )
            changed_paths = validate_diff_policy(
                patch.diff, allowed_paths=None, existing_paths=None,
                repository_allowed_paths=repository_allowed_paths,
            )
            results = db.scalars(select(ValidationResult).where(
                ValidationResult.validation_run_id == validation.id,
            )).all()
            checks = [{"name": result.check_name, "outcome": result.outcome} for result in results]
            _create_pull_request(
                db, job, repository, diff=patch.diff, changed_paths=changed_paths,
                failure_summary=job.failure_summary, root_cause=patch.explanation,
                explanation=patch.explanation, validation_checks=checks,
                validation_environment=validation.environment, verified_pattern=verified_pattern,
                transition_state=False,
            )
        except Exception:
            db.refresh(job)
            _park_for_review(db, job, "The saved patch could not be retried for GitHub publication; the validated artifact remains available.")


def run_worker() -> None:
    recover_stale_jobs()
    connection = Redis.from_url(settings.redis_url)
    Worker([Queue("repairs", connection=connection)], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    run_worker()
