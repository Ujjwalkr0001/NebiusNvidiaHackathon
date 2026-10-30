import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.github_webhooks import redact_text
from app.models import MilisaCacheLookup, MilisaFixPattern, ModelUsageRecord, RepairJob
from app.settings import settings


def cache_identity(job: RepairJob, context: dict) -> dict[str, object]:
    normalized = job.failure_summary.lower()
    normalized = re.sub(r"\b[0-9a-f]{8,64}\b", "<hash>", normalized)
    normalized = re.sub(r"\b\d+\b", "<number>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    signature = hashlib.sha256(f"{job.failure_category}:{normalized}".encode("utf-8")).hexdigest()
    source_files = context.get("source_files") or {}
    python_sources = {
        path: content for path, content in source_files.items()
        if path.lower().endswith(".py")
    }
    source_material = json.dumps(python_sources, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    context_fingerprint = hashlib.sha256(source_material.encode("utf-8")).hexdigest()
    dependency_files = {
        path: content for path, content in source_files.items()
        if path.rsplit("/", 1)[-1].lower() in {
            "pyproject.toml", "requirements.txt", "requirements-dev.txt", "poetry.lock", "uv.lock",
            "pipfile", ".python-version", "runtime.txt",
        }
    }
    dependency_material = json.dumps(dependency_files, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    dependency_fingerprint = hashlib.sha256(dependency_material.encode("utf-8")).hexdigest()
    dependency_text = dependency_material.lower()
    framework = next((name for token, name in (("fastapi", "FastAPI"), ("django", "Django"), ("flask", "Flask")) if token in dependency_text), None)
    languages = context.get("languages") or {}
    language = max(languages, key=languages.get) if languages else "unknown"
    version_constraints = {
        "dependency_fingerprint": dependency_fingerprint,
        "python_version": (source_files.get(".python-version") or source_files.get("runtime.txt") or "").strip(),
    }
    return {
        "error_signature": signature,
        "context_fingerprint": context_fingerprint,
        "language": str(language).lower(),
        "framework": framework,
        "version_constraints": version_constraints,
    }


def lookup_verified_fix(db: Session, job: RepairJob, context: dict) -> MilisaFixPattern | None:
    identity = cache_identity(job, context)
    now = datetime.now(timezone.utc)
    candidates = db.scalars(select(MilisaFixPattern).where(
        MilisaFixPattern.error_signature == identity["error_signature"],
        MilisaFixPattern.failure_category == job.failure_category,
        MilisaFixPattern.language == identity["language"],
        MilisaFixPattern.framework == identity["framework"],
        MilisaFixPattern.context_fingerprint == identity["context_fingerprint"],
        MilisaFixPattern.verification_status == "VERIFIED",
        or_(MilisaFixPattern.organization_id == job.organization_id, MilisaFixPattern.organization_id.is_(None)),
        or_(MilisaFixPattern.expires_at.is_(None), MilisaFixPattern.expires_at > now),
    ).order_by(MilisaFixPattern.success_count.desc(), MilisaFixPattern.reuse_count.desc()).limit(50)).all()
    pattern = next((
        candidate for candidate in candidates
        if candidate.version_constraints == identity["version_constraints"]
    ), None)
    saved_tokens = 0
    if pattern:
        try:
            saved_tokens = max(0, int((pattern.validation_metadata or {}).get("agent_tokens", 0)))
        except (TypeError, ValueError):
            saved_tokens = 0
    db.add(MilisaCacheLookup(
        repair_job_id=job.id,
        fix_pattern_id=pattern.id if pattern else None,
        outcome="HIT" if pattern else "MISS",
        confidence=1.0 if pattern else None,
        compatibility={"mode": "exact", "compatible": bool(pattern)},
        estimated_tokens_saved=saved_tokens,
    ))
    db.commit()
    return pattern


def record_verified_fix(
    db: Session,
    job: RepairJob,
    context: dict,
    *,
    diff: str,
    root_cause: str,
    checks: list[dict[str, str]],
    validation_environment: dict,
    regression_reference: str | None = None,
    reused_pattern: MilisaFixPattern | None = None,
) -> MilisaFixPattern:
    if redact_text(diff) != diff:
        raise ValueError("A patch containing secret-like values cannot be promoted to the cache")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=settings.milisa_fix_ttl_days) if settings.milisa_fix_ttl_days else None
    if reused_pattern:
        reused_pattern.reuse_count += 1
        reused_pattern.last_used_at = now
        reused_pattern.expires_at = expires_at
        if not reused_pattern.regression_reference and regression_reference:
            reused_pattern.regression_reference = regression_reference[:512]
        db.commit()
        return reused_pattern

    identity = cache_identity(job, context)
    agent_tokens = int(db.scalar(select(
        func.coalesce(func.sum(ModelUsageRecord.prompt_tokens), 0)
        + func.coalesce(func.sum(ModelUsageRecord.completion_tokens), 0)
    ).where(ModelUsageRecord.repair_job_id == job.id)) or 0)
    validation_metadata = {
        "status": "PASSED", "checks": checks,
        "environment": validation_environment, "agent_tokens": agent_tokens,
    }
    existing = db.scalar(select(MilisaFixPattern).where(
        MilisaFixPattern.organization_id == job.organization_id,
        MilisaFixPattern.error_signature == identity["error_signature"],
        MilisaFixPattern.failure_category == job.failure_category,
        MilisaFixPattern.language == identity["language"],
        MilisaFixPattern.framework == identity["framework"],
        MilisaFixPattern.context_fingerprint == identity["context_fingerprint"],
        MilisaFixPattern.verification_status == "VERIFIED",
    ).order_by(MilisaFixPattern.success_count.desc()).limit(1))
    if existing and existing.version_constraints == identity["version_constraints"] and existing.patch_template == diff:
        existing.success_count += 1
        existing.last_used_at = now
        existing.expires_at = expires_at
        existing.validation_metadata = validation_metadata
        db.commit()
        return existing

    pattern = MilisaFixPattern(
        organization_id=job.organization_id,
        error_signature=identity["error_signature"],
        failure_category=job.failure_category,
        language=identity["language"],
        framework=identity["framework"],
        version_constraints=identity["version_constraints"],
        context_fingerprint=identity["context_fingerprint"],
        root_cause=redact_text(root_cause)[:5000],
        patch_template=diff,
        validation_metadata=validation_metadata,
        regression_reference=regression_reference[:512] if regression_reference else None,
        source_revision=job.commit_sha,
        target_revision=None,
        expires_at=expires_at,
        verification_status="VERIFIED",
        success_count=1,
        reuse_count=0,
        last_used_at=now,
    )
    db.add(pattern)
    db.commit()
    return pattern
