from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False)


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    memberships: Mapped[list[OrganizationMember]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Organization(Timestamped, Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    members: Mapped[list[OrganizationMember]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class OrganizationMember(Timestamped, Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_member"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(30), default="member", nullable=False)
    organization: Mapped[Organization] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class UserSession(Timestamped, Base):
    __tablename__ = "user_sessions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class GitHubOAuthState(Timestamped, Base):
    __tablename__ = "github_oauth_states"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    pkce_verifier_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    installation_id: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubInstallation(Timestamped, Base):
    __tablename__ = "github_installations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    installation_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(30), nullable=False)
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Repository(Timestamped, Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("installation_id", "github_repo_id", name="uq_github_repo"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    installation_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    github_repo_id: Mapped[int] = mapped_column(Integer, nullable=False)
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    private: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    monitored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RepositorySetting(Timestamped, Base):
    __tablename__ = "repository_settings"
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True)
    trigger_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    allowed_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    required_checks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WebhookEvent(Timestamped, Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("delivery_id", name="uq_webhook_delivery"), Index("ix_webhook_received", "received_at"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(40), default="received", nullable=False)


class RepairJob(Timestamped, Base):
    __tablename__ = "repair_jobs"
    __table_args__ = (UniqueConstraint("organization_id", "idempotency_key", name="uq_repair_idempotency"), Index("ix_jobs_org_status", "organization_id", "status"))
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RECEIVED", nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    failure_category: Mapped[str] = mapped_column(String(60), nullable=False)
    failure_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    workflow_run_id: Mapped[int | None] = mapped_column(Integer)
    check_run_id: Mapped[int | None] = mapped_column(Integer)
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("webhook_events.id", ondelete="SET NULL"))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    queue_job_id: Mapped[str | None] = mapped_column(String(128))
    pr_url: Mapped[str | None] = mapped_column(String(2048))
    error_message: Mapped[str | None] = mapped_column(Text)


class RepairJobEvent(Timestamped, Base):
    __tablename__ = "repair_job_events"
    __table_args__ = (Index("ix_job_events_job_created", "repair_job_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repair_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="CASCADE"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(40))
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AgentRun(Timestamped, Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repair_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="CASCADE"), index=True)
    agent_name: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodePatch(Timestamped, Base):
    __tablename__ = "code_patches"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repair_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="CASCADE"), index=True)
    diff: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class ValidationRun(Timestamped, Base):
    __tablename__ = "validation_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repair_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    environment: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    log_excerpt: Mapped[str] = mapped_column(Text, default="", nullable=False)


class ValidationResult(Timestamped, Base):
    __tablename__ = "validation_results"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    validation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("validation_runs.id", ondelete="CASCADE"), index=True)
    check_name: Mapped[str] = mapped_column(String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class MilisaFixPattern(Timestamped, Base):
    __tablename__ = "milisa_fix_patterns"
    __table_args__ = (Index("ix_milisa_signature_language", "error_signature", "language"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    error_signature: Mapped[str] = mapped_column(String(128), nullable=False)
    failure_category: Mapped[str] = mapped_column(String(60), nullable=False)
    language: Mapped[str] = mapped_column(String(60), nullable=False)
    framework: Mapped[str | None] = mapped_column(String(100))
    version_constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    context_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    patch_template: Mapped[str] = mapped_column(Text, nullable=False)
    regression_reference: Mapped[str | None] = mapped_column(String(512))
    validation_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(64))
    target_revision: Mapped[str | None] = mapped_column(String(64))
    verification_status: Mapped[str] = mapped_column(String(30), default="UNVERIFIED", nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reuse_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MilisaCacheLookup(Timestamped, Base):
    __tablename__ = "milisa_cache_lookups"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repair_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="CASCADE"), index=True)
    fix_pattern_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("milisa_fix_patterns.id", ondelete="SET NULL"))
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float | None] = mapped_column()
    compatibility: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    estimated_tokens_saved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PullRequest(Timestamped, Base):
    __tablename__ = "pull_requests"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repair_job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="CASCADE"), unique=True)
    github_pr_id: Mapped[int] = mapped_column(Integer, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)


class ModelUsageRecord(Timestamped, Base):
    __tablename__ = "model_usage_records"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    repair_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("repair_jobs.id", ondelete="SET NULL"), index=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost: Mapped[float | None] = mapped_column()


class AuditLog(Timestamped, Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(64))
