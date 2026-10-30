from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PullRequest, RepairJob, RepairJobEvent

ACTIVE_STATES = {
    "RECEIVED", "QUEUED", "TRIAGING", "CONTEXT_GATHERING", "CACHE_LOOKUP",
    "PLANNING", "PATCH_GENERATION", "SANDBOX_VALIDATION", "POLICY_CHECK",
    "PULL_REQUEST_CREATION", "RETRYING",
}
TERMINAL_STATES = {"COMPLETED", "CANCELLED", "FAILED"}
TRANSITIONS = {
    "RECEIVED": {"QUEUED", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "QUEUED": {"TRIAGING", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "TRIAGING": {"CONTEXT_GATHERING", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "CONTEXT_GATHERING": {"CACHE_LOOKUP", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "CACHE_LOOKUP": {"PLANNING", "PATCH_GENERATION", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "PLANNING": {"PATCH_GENERATION", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "PATCH_GENERATION": {"SANDBOX_VALIDATION", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "SANDBOX_VALIDATION": {"POLICY_CHECK", "RETRYING", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "RETRYING": {"PATCH_GENERATION", "SANDBOX_VALIDATION", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "POLICY_CHECK": {"PULL_REQUEST_CREATION", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "PULL_REQUEST_CREATION": {"COMPLETED", "CANCELLED", "FAILED", "NEEDS_HUMAN_REVIEW"},
    "NEEDS_HUMAN_REVIEW": {"QUEUED", "PULL_REQUEST_CREATION", "CANCELLED", "FAILED"},
}


def transition_job(
    db: Session,
    job_id: UUID,
    target: str,
    *,
    actor: str,
    detail: dict | None = None,
) -> RepairJob:
    db.flush()
    job = db.scalar(select(RepairJob).where(RepairJob.id == job_id).with_for_update())
    if not job:
        raise HTTPException(status_code=404, detail="Repair job not found")
    if target not in TRANSITIONS.get(job.status, set()):
        raise HTTPException(status_code=409, detail=f"Invalid repair job transition: {job.status} -> {target}")
    if target == "COMPLETED":
        pull_request = db.scalar(select(PullRequest.id).where(PullRequest.repair_job_id == job.id))
        if not pull_request or not job.pr_url:
            raise HTTPException(status_code=409, detail="A persisted pull request is required before completion")
    previous = job.status
    job.status = target
    db.add(RepairJobEvent(
        repair_job_id=job.id,
        from_state=previous,
        to_state=target,
        actor=actor,
        detail=detail or {},
    ))
    db.flush()
    return job
