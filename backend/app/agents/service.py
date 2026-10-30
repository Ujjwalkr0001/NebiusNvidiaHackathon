from datetime import datetime, timezone
from typing import Callable, TypeVar

from sqlalchemy.orm import Session

from app.agents.nebius import InferenceResult
from app.github_webhooks import redact_text
from app.models import AgentRun, ModelUsageRecord, RepairJob
from app.settings import settings

Output = TypeVar("Output")


def run_and_record_agent(
    db: Session,
    job: RepairJob,
    *,
    agent_name: str,
    input_summary: str,
    execute: Callable[[], tuple[Output, InferenceResult]],
) -> Output:
    started_at = datetime.now(timezone.utc)
    run = AgentRun(
        repair_job_id=job.id,
        agent_name=agent_name,
        status="RUNNING",
        input_summary=input_summary[:2000],
        started_at=started_at,
    )
    db.add(run)
    db.flush()
    db.commit()
    try:
        output, usage = execute()
    except Exception as exc:
        run.status = "FAILED"
        run.output = {"error": _safe_error(exc)}
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        raise

    run.status = "SUCCEEDED"
    run.output = _redact_output(output.model_dump(mode="json"))
    run.model_id = usage.model_id
    run.finished_at = datetime.now(timezone.utc)
    estimated_cost = None
    if usage.provider == "nebius-token-factory" and settings.nebius_input_price_per_million is not None and settings.nebius_output_price_per_million is not None:
        estimated_cost = (
            usage.prompt_tokens * settings.nebius_input_price_per_million
            + usage.completion_tokens * settings.nebius_output_price_per_million
        ) / 1_000_000
    db.add(ModelUsageRecord(
        organization_id=job.organization_id,
        repair_job_id=job.id,
        provider=usage.provider,
        model_id=usage.model_id,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        estimated_cost=estimated_cost,
    ))
    db.commit()
    return output


def _safe_error(exc: Exception) -> str:
    # Exception messages from model output, repository content, or HTTP bodies
    # are never persisted here.
    if exc.__class__.__module__.startswith("app.agents"):
        return redact_text(str(exc))[:500]
    return "Agent execution failed; consult server diagnostics."


def _redact_output(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_output(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_output(item) for key, item in value.items()}
    return value
