from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from redis.exceptions import RedisError
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Literal
from uuid import UUID, uuid4
from urllib.parse import urlparse

from app.auth_security import hash_password, new_session_token, session_token_hash, verify_password
from app.agents.agents import validate_repository_patch_paths
from app.agents.millisa import MillisaClient
from app.agents.nebius import InferenceError
from app.database import get_db
from app.github_app import (
    GitHubConnectionError, decrypt_pkce_verifier, encrypt_pkce_verifier,
    exchange_oauth_code, installation_url, list_user_installation_repositories,
    list_user_installations, new_pkce_pair, oauth_authorization_url, pkce_challenge,
)
from app.github_webhooks import actionable_failure, parse_payload, payload_digest, redact_text, verify_signature
from app.job_state import transition_job
from app.models import (
    AgentRun, AuditLog, CodePatch, GitHubInstallation, GitHubOAuthState, MilisaCacheLookup, MilisaFixPattern,
    ModelUsageRecord, Organization, OrganizationMember, PullRequest, RepairJob,
    RepairJobEvent, Repository, RepositorySetting, User, UserSession, ValidationRun, ValidationResult,
    WebhookEvent,
)
from app.queueing import enqueue_pull_request_retry, enqueue_repair
from app.rate_limit import consume_rate_limit, rate_limit_rule
from app.settings import settings
from rq.exceptions import DuplicateJobError

app = FastAPI(
    title="Self-Healing CI/CD API",
    version="0.1.0",
    description="Authenticated repair orchestration API for the self-healing CI/CD platform.",
)

@app.middleware("http")
async def enforce_rate_limits(request: Request, call_next):
    rule = rate_limit_rule(request.method, request.url.path)
    if rule:
        try:
            count, ttl = await consume_rate_limit(request, rule)
        except RedisError:
            if settings.app_env.lower() == "production":
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Request protection is temporarily unavailable."},
                    headers={"Retry-After": "1"},
                )
        else:
            if count > rule.limit:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests; retry later."},
                    headers={"Retry-After": str(ttl)},
                )
    return await call_next(request)


@app.middleware("http")
async def check_browser_origin(request: Request, call_next):
    if request.url.path != "/api/v1/webhooks/github" and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in ALLOWED_BROWSER_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "Cross-origin request rejected"})
    return await call_next(request)


ALLOWED_BROWSER_ORIGINS = {settings.frontend_origin.rstrip("/")}
if settings.app_env.lower() in {"development", "test"}:
    ALLOWED_BROWSER_ORIGINS.update({
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    })

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(ALLOWED_BROWSER_ORIGINS),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


class RegisterInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    organization_name: str = Field(min_length=1, max_length=160)


class LoginInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class GitHubInstallInput(BaseModel):
    organization_id: UUID


class RepositoryMonitorInput(BaseModel):
    monitored: bool


class RepositoryTriggerInput(BaseModel):
    workflow_run_failure: bool
    check_run_failure: bool


class RepositoryChecksInput(BaseModel):
    required_checks: list[Literal["ruff", "mypy", "build"]] = Field(default_factory=list, max_length=3)


class RepositoryPatchScopeInput(BaseModel):
    allowed_paths: list[str] = Field(default_factory=list, max_length=100)


class ManualRepairInput(BaseModel):
    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    failure_category: Literal["test", "build", "lint", "dependency", "configuration", "runtime", "unknown"] = "unknown"
    failure_summary: str = Field(min_length=3, max_length=5000)


def _repository_triggers(setting: RepositorySetting | None) -> dict[str, bool]:
    policy = setting.trigger_policy if setting else {}
    return {
        "workflow_run_failure": policy.get("workflow_run_failure", True) is True,
        "check_run_failure": policy.get("check_run_failure", True) is True,
    }


def _repository_checks(setting: RepositorySetting | None) -> list[str]:
    allowed = {"ruff", "mypy", "build"}
    configured = setting.required_checks if setting else []
    selected = {item for item in configured if isinstance(item, str) and item in allowed} if isinstance(configured, list) else set()
    return [item for item in ("ruff", "mypy", "build") if item in selected]


def _create_session(user: User, db: Session, response: Response) -> None:
    token = new_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    db.add(UserSession(user_id=user.id, token_hash=session_token_hash(token), expires_at=expires_at))
    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    saved = db.scalar(select(UserSession).where(UserSession.token_hash == session_token_hash(token)))
    now = datetime.now(timezone.utc)
    expiry = saved.expires_at if saved else None
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if not saved or not expiry or expiry <= now:
        if saved:
            db.delete(saved)
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    user = db.get(User, saved.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")
    return user


@app.get("/health", tags=["operations"])
def health() -> dict[str, str]:
    """Liveness only; this does not imply dependencies or repair flows are ready."""
    return {"status": "ok"}


@app.get("/api/v1/status", tags=["operations"])
def api_status() -> dict[str, str | bool]:
    """Report current implementation/configuration state without exposing secrets."""
    github_app_configured = bool(settings.github_app_id and settings.github_app_slug and settings.github_client_id and settings.github_client_secret and settings.github_callback_url and settings.github_private_key_path)
    webhook_configured = bool(settings.github_webhook_secret)
    nebius_configured = bool(settings.nebius_base_url and settings.nebius_api_key and settings.nebius_model)
    millisa_configured = bool(settings.millisa_base_url and settings.millisa_api_key and settings.millisa_model)
    validation_configured = bool(settings.validation_image)
    return {
        "service": "self-healing-ci-api",
        "milestone": "8-hardening-and-verification",
        "integrations_configured": github_app_configured and webhook_configured and nebius_configured,
        "repair_jobs_enabled": github_app_configured and webhook_configured and nebius_configured and validation_configured,
        "github_app_configured": github_app_configured,
        "github_webhook_configured": webhook_configured,
        "nebius_configured": nebius_configured,
        "millisa_configured": millisa_configured,
        "validation_configured": validation_configured,
    }


@app.post("/api/v1/auth/register", status_code=status.HTTP_201_CREATED, tags=["authentication"])
def register(data: RegisterInput, response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    email = data.email.strip().lower()
    if not data.display_name.strip() or not data.organization_name.strip():
        raise HTTPException(status_code=422, detail="Display and organization names cannot be blank")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(email=email, display_name=data.display_name.strip(), password_hash=hash_password(data.password))
    organization = Organization(name=data.organization_name.strip())
    db.add_all([user, organization])
    try:
        db.flush()
        db.add(OrganizationMember(user_id=user.id, organization_id=organization.id, role="owner"))
        db.add(AuditLog(organization_id=organization.id, user_id=user.id, action="account.registered", entity_type="user", entity_id=str(user.id)))
        _create_session(user, db, response)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists") from exc
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name}


@app.post("/api/v1/auth/login", tags=["authentication"])
def login(data: LoginInput, response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    email = data.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.is_active or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    _create_session(user, db, response)
    db.commit()
    return {"id": str(user.id), "email": user.email, "display_name": user.display_name}


@app.post("/api/v1/auth/logout", tags=["authentication"])
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, str]:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        saved = db.scalar(select(UserSession).where(UserSession.token_hash == session_token_hash(token)))
        if saved:
            db.delete(saved)
            db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/", httponly=True, secure=settings.session_cookie_secure, samesite="lax")
    return {"status": "signed_out"}


@app.post("/api/v1/github/installations/start", tags=["github"])
def start_github_installation(
    data: GitHubInstallInput,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    if not (settings.github_app_slug and settings.github_client_id and settings.github_client_secret and settings.github_callback_url and settings.github_app_id):
        raise HTTPException(status_code=503, detail="GitHub App OAuth configuration is incomplete")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == data.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    nonce = new_session_token()
    pkce_verifier, _ = new_pkce_pair()
    db.add(GitHubOAuthState(
        user_id=user.id,
        organization_id=data.organization_id,
        state_hash=session_token_hash(nonce),
        pkce_verifier_encrypted=encrypt_pkce_verifier(pkce_verifier),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    ))
    db.add(AuditLog(organization_id=data.organization_id, user_id=user.id, action="github.installation.started", entity_type="github_installation"))
    db.commit()
    return {"installation_url": installation_url(settings.github_app_slug, nonce)}


@app.get("/api/v1/github/setup", tags=["github"])
def github_setup(
    installation_id: int,
    state: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    if installation_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid GitHub installation")
    state_row = db.scalar(select(GitHubOAuthState).where(
        GitHubOAuthState.state_hash == session_token_hash(state),
        GitHubOAuthState.user_id == user.id,
    ).with_for_update())
    now = datetime.now(timezone.utc)
    expiry = state_row.expires_at if state_row else None
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if not state_row or not expiry or expiry <= now or state_row.consumed_at:
        raise HTTPException(status_code=400, detail="GitHub connection state is invalid or expired")
    state_row.installation_id = installation_id
    verifier = decrypt_pkce_verifier(state_row.pkce_verifier_encrypted)
    db.commit()
    return RedirectResponse(
        oauth_authorization_url(settings.github_client_id, settings.github_callback_url, state, pkce_challenge(verifier)),
        status_code=303,
    )


@app.get("/api/v1/github/callback", tags=["github"])
def github_callback(
    code: str,
    state: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    state_row = db.scalar(select(GitHubOAuthState).where(
        GitHubOAuthState.state_hash == session_token_hash(state),
        GitHubOAuthState.user_id == user.id,
    ).with_for_update())
    now = datetime.now(timezone.utc)
    expiry = state_row.expires_at if state_row else None
    if expiry and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if not state_row or not expiry or expiry <= now or state_row.consumed_at:
        raise HTTPException(status_code=400, detail="GitHub connection state is invalid or expired")
    if not state_row.installation_id:
        raise HTTPException(status_code=400, detail="GitHub installation has not completed")
    organization_id = state_row.organization_id
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    state_row.consumed_at = now
    installation_id = state_row.installation_id
    verifier_encrypted = state_row.pkce_verifier_encrypted
    db.commit()

    try:
        verifier = decrypt_pkce_verifier(verifier_encrypted)
        user_token = exchange_oauth_code(settings.github_client_id, settings.github_client_secret, code, verifier)
        installations = list_user_installations(user_token, settings.github_app_id)
        installation_data = next((item for item in installations if item.get("id") == installation_id), None)
        if not installation_data:
            raise HTTPException(status_code=403, detail="The signed-in GitHub user cannot access this app installation")
        repos = list_user_installation_repositories(user_token, installation_id)
    except GitHubConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        account = installation_data.get("account") or {}
        account_login = account.get("login")
        account_type = account.get("type")
        if not account_login or not account_type:
            raise HTTPException(status_code=502, detail="GitHub installation account data is incomplete")
        installation = db.scalar(select(GitHubInstallation).where(GitHubInstallation.installation_id == installation_id))
        if installation and installation.organization_id != organization_id:
            raise HTTPException(status_code=409, detail="This GitHub installation is already linked to another workspace")
        if installation:
            installation.account_login = account_login
            installation.account_type = account_type
            installation.suspended = bool(installation_data.get("suspended_at"))
        else:
            db.add(GitHubInstallation(
                organization_id=organization_id,
                installation_id=installation_id,
                account_login=account_login,
                account_type=account_type,
                suspended=bool(installation_data.get("suspended_at")),
            ))
        for repo_data in repos:
            repo_id = repo_data.get("id")
            if not isinstance(repo_id, int):
                continue
            repository = db.scalar(select(Repository).where(
                Repository.installation_id == installation_id,
                Repository.github_repo_id == repo_id,
            ))
            if repository and repository.organization_id != organization_id:
                raise HTTPException(status_code=409, detail="A GitHub repository is already linked to another workspace")
            if repository:
                repository.full_name = str(repo_data.get("full_name") or repository.full_name)
                repository.default_branch = str(repo_data.get("default_branch") or repository.default_branch)
                repository.private = bool(repo_data.get("private", repository.private))
            else:
                db.add(Repository(
                    organization_id=organization_id,
                    installation_id=installation_id,
                    github_repo_id=repo_id,
                    full_name=str(repo_data.get("full_name") or repo_data.get("name") or repo_id),
                    default_branch=str(repo_data.get("default_branch") or "main"),
                    private=bool(repo_data.get("private", True)),
                    monitored=False,
                ))
        db.add(AuditLog(organization_id=organization_id, user_id=user.id, action="github.installation.connected", entity_type="github_installation"))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="GitHub installation could not be linked to this workspace") from exc
    except HTTPException:
        db.rollback()
        raise
    return RedirectResponse(f"{settings.frontend_origin.rstrip('/')}/?github=connected", status_code=303)


@app.get("/api/v1/github/repositories", tags=["github"])
def list_repositories(
    organization_id: UUID | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    memberships = db.scalars(select(OrganizationMember).where(OrganizationMember.user_id == user.id)).all()
    allowed_ids = {member.organization_id for member in memberships}
    if organization_id is not None:
        if organization_id not in allowed_ids:
            raise HTTPException(status_code=403, detail="Workspace access denied")
        allowed_ids = {organization_id}
    if not allowed_ids:
        return []
    repositories = db.scalars(select(Repository).where(Repository.organization_id.in_(allowed_ids)).order_by(Repository.full_name)).all()
    settings_by_repo = {
        row.repository_id: row for row in db.scalars(select(RepositorySetting).where(
            RepositorySetting.repository_id.in_([repo.id for repo in repositories])
        )).all()
    } if repositories else {}
    return [{
        "id": str(repo.id), "full_name": repo.full_name, "private": repo.private,
        "monitored": repo.monitored, "default_branch": repo.default_branch,
        "triggers": _repository_triggers(settings_by_repo.get(repo.id)),
        "required_checks": _repository_checks(settings_by_repo.get(repo.id)),
        "allowed_paths": settings_by_repo.get(repo.id).allowed_paths if settings_by_repo.get(repo.id) else [],
    } for repo in repositories]


@app.patch("/api/v1/github/repositories/{repository_id}/monitor", tags=["github"])
def set_repository_monitoring(
    repository_id: UUID,
    data: RepositoryMonitorInput,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == repository.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    repository.monitored = data.monitored
    db.add(AuditLog(
        organization_id=repository.organization_id,
        user_id=user.id,
        action="repository.monitoring.enabled" if data.monitored else "repository.monitoring.disabled",
        entity_type="repository",
        entity_id=str(repository.id),
    ))
    db.commit()
    return {"id": str(repository.id), "full_name": repository.full_name, "monitored": repository.monitored}


@app.patch("/api/v1/github/repositories/{repository_id}/triggers", tags=["github"])
def set_repository_triggers(
    repository_id: UUID,
    data: RepositoryTriggerInput,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == repository.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    setting = db.get(RepositorySetting, repository.id)
    if setting is None:
        setting = RepositorySetting(repository_id=repository.id)
        db.add(setting)
    setting.trigger_policy = {
        "workflow_run_failure": data.workflow_run_failure,
        "check_run_failure": data.check_run_failure,
    }
    db.add(AuditLog(
        organization_id=repository.organization_id,
        user_id=user.id,
        action="repository.triggers.updated",
        entity_type="repository",
        entity_id=str(repository.id),
        details=setting.trigger_policy,
    ))
    db.commit()
    return {"id": str(repository.id), "triggers": setting.trigger_policy}


@app.patch("/api/v1/github/repositories/{repository_id}/checks", tags=["github"])
def set_repository_checks(
    repository_id: UUID,
    data: RepositoryChecksInput,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == repository.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    if len(set(data.required_checks)) != len(data.required_checks):
        raise HTTPException(status_code=422, detail="Required checks must not contain duplicates")
    setting = db.get(RepositorySetting, repository.id)
    if setting is None:
        setting = RepositorySetting(repository_id=repository.id)
        db.add(setting)
    selected_checks = set(data.required_checks)
    setting.required_checks = [item for item in ("ruff", "mypy", "build") if item in selected_checks]
    db.add(AuditLog(
        organization_id=repository.organization_id, user_id=user.id,
        action="repository.validation_checks.updated", entity_type="repository",
        entity_id=str(repository.id), details={"required_checks": setting.required_checks},
    ))
    db.commit()
    return {"id": str(repository.id), "required_checks": setting.required_checks}


@app.patch("/api/v1/github/repositories/{repository_id}/patch-scope", tags=["github"])
def set_repository_patch_scope(
    repository_id: UUID,
    data: RepositoryPatchScopeInput,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == repository.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    if len(set(data.allowed_paths)) != len(data.allowed_paths):
        raise HTTPException(status_code=422, detail="Repository patch scope must not contain duplicate paths")
    try:
        allowed_paths = validate_repository_patch_paths(data.allowed_paths)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    setting = db.get(RepositorySetting, repository.id)
    if setting is None:
        setting = RepositorySetting(repository_id=repository.id)
        db.add(setting)
    setting.allowed_paths = allowed_paths
    db.add(AuditLog(
        organization_id=repository.organization_id, user_id=user.id,
        action="repository.patch_scope.updated", entity_type="repository",
        entity_id=str(repository.id), details={"allowed_paths": allowed_paths},
    ))
    db.commit()
    return {"id": str(repository.id), "allowed_paths": allowed_paths}


@app.post("/api/v1/github/repositories/{repository_id}/repair", status_code=status.HTTP_202_ACCEPTED, tags=["repair-jobs"])
def request_repository_repair(
    repository_id: UUID,
    data: ManualRepairInput,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    repository = db.get(Repository, repository_id)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == repository.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership:
        raise HTTPException(status_code=404, detail="Repository not found")
    if membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    installation = db.scalar(select(GitHubInstallation).where(
        GitHubInstallation.installation_id == repository.installation_id,
        GitHubInstallation.organization_id == repository.organization_id,
        GitHubInstallation.suspended.is_(False),
    ))
    if not installation:
        raise HTTPException(status_code=409, detail="An active GitHub App installation is required")

    failure_summary = redact_text(data.failure_summary).strip()
    if len(failure_summary) < 3:
        raise HTTPException(status_code=422, detail="Failure details must contain at least three characters")
    job = RepairJob(
        organization_id=repository.organization_id,
        repository_id=repository.id,
        idempotency_key=f"manual:{uuid4().hex}",
        status="RECEIVED",
        commit_sha=data.commit_sha.lower(),
        base_branch=repository.default_branch,
        failure_category=data.failure_category,
        failure_summary=f"Manually requested repair: {failure_summary[:5000]}",
    )
    db.add(job)
    db.flush()
    transition_job(db, job.id, "QUEUED", actor=f"user:{user.id}", detail={"source": "manual_request"})
    db.add(AuditLog(
        organization_id=repository.organization_id,
        user_id=user.id,
        action="repair_job.manual_request_created",
        entity_type="repair_job",
        entity_id=str(job.id),
        details={"repository_id": str(repository.id), "failure_category": data.failure_category},
    ))
    db.commit()

    try:
        job.queue_job_id = enqueue_repair(job.id)
        db.commit()
        return {"job_id": str(job.id), "queued": True}
    except DuplicateJobError:
        job.queue_job_id = str(job.id)
        db.commit()
        return {"job_id": str(job.id), "queued": True, "already_queued": True}
    except Exception:
        db.rollback()
        saved_job = db.get(RepairJob, job.id)
        if saved_job:
            saved_job.error_message = "Redis queue unavailable; this manually requested repair remains queued for retry."
            db.commit()
        return {"job_id": str(job.id), "queued": False}


@app.get("/api/v1/repair-jobs", tags=["repair-jobs"])
def list_repair_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    organization_ids = db.scalars(select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)).all()
    if not organization_ids:
        return []
    rows = db.execute(select(RepairJob, Repository.full_name).join(
        Repository, Repository.id == RepairJob.repository_id
    ).where(RepairJob.organization_id.in_(organization_ids)).order_by(RepairJob.created_at.desc()).limit(limit)).all()
    return [{
        "id": str(job.id), "repository": repository_name, "status": job.status,
        "failure_category": job.failure_category, "failure_summary": job.failure_summary,
        "commit_sha": job.commit_sha, "base_branch": job.base_branch,
        "workflow_run_id": job.workflow_run_id, "check_run_id": job.check_run_id,
        "retry_count": job.retry_count, "pr_url": job.pr_url,
        "created_at": job.created_at.isoformat(),
    } for job, repository_name in rows]


@app.get("/api/v1/repair-jobs/{job_id}", tags=["repair-jobs"])
def get_repair_job(job_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    job = db.get(RepairJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Repair job not found")
    membership = db.scalar(select(OrganizationMember.id).where(
        OrganizationMember.organization_id == job.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership:
        raise HTTPException(status_code=404, detail="Repair job not found")
    repository = db.get(Repository, job.repository_id)
    pull_request = db.scalar(select(PullRequest).where(PullRequest.repair_job_id == job.id))
    events = db.scalars(select(RepairJobEvent).where(RepairJobEvent.repair_job_id == job.id).order_by(RepairJobEvent.created_at)).all()
    agents = db.scalars(select(AgentRun).where(AgentRun.repair_job_id == job.id).order_by(AgentRun.created_at)).all()
    patches = db.scalars(select(CodePatch).where(CodePatch.repair_job_id == job.id).order_by(CodePatch.created_at)).all()
    validations = db.scalars(select(ValidationRun).where(ValidationRun.repair_job_id == job.id).order_by(ValidationRun.created_at)).all()
    validation_ids = [run.id for run in validations]
    results = db.scalars(select(ValidationResult).where(ValidationResult.validation_run_id.in_(validation_ids))).all() if validation_ids else []
    return {
        "id": str(job.id), "repository": repository.full_name if repository else None,
        "status": job.status, "failure_category": job.failure_category,
        "failure_summary": job.failure_summary, "commit_sha": job.commit_sha,
        "base_branch": job.base_branch, "retry_count": job.retry_count,
        "workflow_run_id": job.workflow_run_id, "check_run_id": job.check_run_id,
        "pr_url": job.pr_url, "error_message": job.error_message,
        "pull_request": ({
            "number": pull_request.number, "url": pull_request.url,
            "branch_name": pull_request.branch_name, "status": pull_request.status,
            "created_at": pull_request.created_at.isoformat(),
        } if pull_request else None),
        "events": [{"from_state": event.from_state, "to_state": event.to_state, "actor": event.actor,
                    "detail": event.detail, "created_at": event.created_at.isoformat()} for event in events],
        "agent_runs": [{"agent": run.agent_name, "status": run.status, "output": run.output,
                        "model_id": run.model_id, "created_at": run.created_at.isoformat()} for run in agents],
        "patches": [{"diff": patch.diff, "explanation": patch.explanation,
                     "patch_hash": patch.patch_hash, "created_at": patch.created_at.isoformat()} for patch in patches],
        "validation_runs": [{"attempt": run.attempt, "status": run.status, "environment": run.environment,
                             "log_excerpt": run.log_excerpt, "created_at": run.created_at.isoformat(),
                             "results": [{"check_name": result.check_name, "outcome": result.outcome,
                                          "duration_ms": result.duration_ms, "details": result.details}
                                         for result in results if result.validation_run_id == run.id]} for run in validations],
    }


@app.post("/api/v1/repair-jobs/{job_id}/retry-queue", tags=["repair-jobs"])
def retry_queued_job(job_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    job = db.get(RepairJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Repair job not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == job.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    if job.status != "QUEUED":
        raise HTTPException(status_code=409, detail="Only queued jobs can be re-enqueued")
    if job.queue_job_id:
        return {"job_id": str(job.id), "queued": True, "already_queued": True}
    try:
        job.queue_job_id = enqueue_repair(job.id)
        job.error_message = None
        db.add(AuditLog(organization_id=job.organization_id, user_id=user.id, action="repair_job.queue_retried", entity_type="repair_job", entity_id=str(job.id)))
        db.commit()
    except DuplicateJobError:
        job.queue_job_id = str(job.id)
        job.error_message = None
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Repair queue is unavailable; the job remains queued") from exc
    return {"job_id": str(job.id), "queued": True, "already_queued": False}


@app.post("/api/v1/repair-jobs/{job_id}/retry-pull-request", tags=["repair-jobs"])
def retry_pull_request_creation(
    job_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    job = db.scalar(select(RepairJob).where(RepairJob.id == job_id).with_for_update())
    if not job:
        raise HTTPException(status_code=404, detail="Repair job not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == job.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    if job.status != "NEEDS_HUMAN_REVIEW" or job.pr_url:
        raise HTTPException(status_code=409, detail="Only reviewed jobs without a pull request can retry PR creation")
    latest_validation = db.scalar(select(ValidationRun).where(
        ValidationRun.repair_job_id == job.id,
    ).order_by(ValidationRun.attempt.desc()).limit(1))
    patch = db.scalar(select(CodePatch).where(
        CodePatch.repair_job_id == job.id,
    ).order_by(CodePatch.created_at.desc()).limit(1))
    if not latest_validation or latest_validation.status != "PASSED" or not patch:
        raise HTTPException(status_code=409, detail="A passing validation run and persisted patch are required")

    transition_job(db, job.id, "PULL_REQUEST_CREATION", actor=f"user:{user.id}", detail={"retry_requested": True})
    db.commit()
    request_id = uuid4().hex[:12]
    try:
        job.queue_job_id = enqueue_pull_request_retry(job.id, request_id)
        job.error_message = None
        db.add(AuditLog(
            organization_id=job.organization_id, user_id=user.id,
            action="repair_job.pull_request_retry_requested", entity_type="repair_job", entity_id=str(job.id),
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(RepairJob, job_id)
        if job and job.status == "PULL_REQUEST_CREATION":
            transition_job(db, job.id, "NEEDS_HUMAN_REVIEW", actor="api", detail={"reason": "queue_unavailable"})
            job.error_message = "Pull request retry could not be queued; the validated patch is preserved."
            db.commit()
        raise HTTPException(status_code=503, detail="Pull request retry queue is unavailable") from exc
    return {"job_id": str(job.id), "queued": True, "request_id": request_id}


@app.post("/api/v1/repair-jobs/{job_id}/cancel", tags=["repair-jobs"])
def cancel_repair_job(job_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, str]:
    job = db.get(RepairJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Repair job not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == job.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization owner or admin access required")
    if job.status == "PULL_REQUEST_CREATION":
        raise HTTPException(status_code=409, detail="Pull request creation is in progress and cannot be cancelled safely")
    transition_job(db, job.id, "CANCELLED", actor=f"user:{user.id}")
    db.add(AuditLog(organization_id=job.organization_id, user_id=user.id, action="repair_job.cancelled", entity_type="repair_job", entity_id=str(job.id)))
    db.commit()
    return {"id": str(job.id), "status": job.status}


@app.post("/api/v1/webhooks/github", status_code=status.HTTP_202_ACCEPTED, tags=["github"])
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, object]:
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=503, detail="GitHub webhook secret is not configured")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 2_000_000:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 2_000_000:
            raise HTTPException(status_code=413, detail="Webhook payload is too large")
        body.extend(chunk)
    raw_body = bytes(body)
    if not verify_signature(raw_body, request.headers.get("x-hub-signature-256"), settings.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    delivery_id = request.headers.get("x-github-delivery", "")
    event_type = request.headers.get("x-github-event", "")
    if not delivery_id or len(delivery_id) > 128 or not event_type or len(event_type) > 100:
        raise HTTPException(status_code=400, detail="Missing or invalid GitHub delivery headers")
    try:
        payload = parse_payload(raw_body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook JSON") from exc

    existing = db.scalar(select(WebhookEvent).where(WebhookEvent.delivery_id == delivery_id))
    if existing:
        prior_job = db.scalar(select(RepairJob.id).where(RepairJob.source_event_id == existing.id))
        return {"accepted": True, "duplicate": True, "repair_job_id": str(prior_job) if prior_job else None}

    event_record = WebhookEvent(
        delivery_id=delivery_id,
        event_type=event_type,
        payload_digest=payload_digest(raw_body),
        outcome="received",
    )
    db.add(event_record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True, "repair_job_id": None}

    installation_data = payload.get("installation") or {}
    installation_id = installation_data.get("id")
    if event_type == "installation" and isinstance(installation_id, int):
        linked_installation = db.scalar(select(GitHubInstallation).where(GitHubInstallation.installation_id == installation_id))
        action = payload.get("action")
        if linked_installation and action in {"deleted", "suspend", "unsuspend"}:
            linked_installation.suspended = action != "unsuspend"
            if action in {"deleted", "suspend"}:
                repositories = db.scalars(select(Repository).where(Repository.installation_id == installation_id)).all()
                for repository in repositories:
                    repository.monitored = False
            db.add(AuditLog(
                organization_id=linked_installation.organization_id,
                action=f"github.installation.{action}",
                entity_type="github_installation",
                entity_id=str(installation_id),
            ))
            event_record.outcome = "installation_state_updated"
        else:
            event_record.outcome = "ignored_unlinked_installation"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}

    if event_type == "installation_repositories" and isinstance(installation_id, int):
        linked_installation = db.scalar(select(GitHubInstallation).where(GitHubInstallation.installation_id == installation_id))
        action = payload.get("action")
        if linked_installation and action in {"added", "removed"}:
            removed = payload.get("repositories_removed") or []
            for repo_data in removed:
                repo = db.scalar(select(Repository).where(
                    Repository.installation_id == installation_id,
                    Repository.github_repo_id == repo_data.get("id"),
                ))
                if repo:
                    repo.monitored = False
            added = payload.get("repositories_added") or []
            for repo_data in added:
                repo_id = repo_data.get("id")
                if not isinstance(repo_id, int):
                    continue
                repo = db.scalar(select(Repository).where(
                    Repository.installation_id == installation_id,
                    Repository.github_repo_id == repo_id,
                ))
                if not repo:
                    db.add(Repository(
                        organization_id=linked_installation.organization_id,
                        installation_id=installation_id,
                        github_repo_id=repo_id,
                        full_name=str(repo_data.get("full_name") or repo_data.get("name") or repo_id),
                        default_branch=str(repo_data.get("default_branch") or "main"),
                        private=bool(repo_data.get("private", True)),
                        monitored=False,
                    ))
            db.add(AuditLog(
                organization_id=linked_installation.organization_id,
                action=f"github.installation_repositories.{action}",
                entity_type="github_installation",
                entity_id=str(installation_id),
            ))
            event_record.outcome = "installation_repositories_updated"
        else:
            event_record.outcome = "ignored_unlinked_installation"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}

    if event_type == "pull_request":
        pull_data = payload.get("pull_request") or {}
        repo_id = (payload.get("repository") or {}).get("id")
        pull_id = pull_data.get("id")
        if not isinstance(installation_id, int) or not isinstance(repo_id, int) or not isinstance(pull_id, int):
            event_record.outcome = "ignored_invalid_pull_request_event"
            db.commit()
            return {"accepted": True, "duplicate": False, "repair_job_id": None}
        linked_installation = db.scalar(select(GitHubInstallation).where(
            GitHubInstallation.installation_id == installation_id,
            GitHubInstallation.suspended.is_(False),
        ))
        tracked_pull = db.scalar(select(PullRequest).join(
            RepairJob, RepairJob.id == PullRequest.repair_job_id,
        ).join(Repository, Repository.id == RepairJob.repository_id).where(
            PullRequest.github_pr_id == pull_id,
            Repository.github_repo_id == repo_id,
            Repository.installation_id == installation_id,
        ))
        if not linked_installation or not tracked_pull:
            event_record.outcome = "ignored_unlinked_pull_request"
            db.commit()
            return {"accepted": True, "duplicate": False, "repair_job_id": None}
        action = payload.get("action")
        if action == "closed":
            tracked_pull.status = "MERGED" if bool(pull_data.get("merged")) else "CLOSED"
        elif action in {"opened", "reopened", "synchronize", "ready_for_review", "converted_to_draft"}:
            tracked_pull.status = "OPEN"
        db.add(AuditLog(
            organization_id=linked_installation.organization_id,
            action=f"github.pull_request.{action or 'updated'}",
            entity_type="pull_request", entity_id=str(tracked_pull.id),
            details={"number": pull_data.get("number"), "status": tracked_pull.status},
        ))
        event_record.outcome = "pull_request_state_updated"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}

    if event_type == "push":
        repository_data = payload.get("repository") or {}
        github_repo_id = repository_data.get("id")
        if not isinstance(installation_id, int) or not isinstance(github_repo_id, int):
            event_record.outcome = "ignored_invalid_push"
            db.commit()
            return {"accepted": True, "duplicate": False, "repair_job_id": None}
        linked_installation = db.scalar(select(GitHubInstallation).where(
            GitHubInstallation.installation_id == installation_id,
            GitHubInstallation.suspended.is_(False),
        ))
        linked_repository = db.scalar(select(Repository).where(
            Repository.installation_id == installation_id,
            Repository.github_repo_id == github_repo_id,
        ))
        if not linked_installation or not linked_repository or linked_repository.organization_id != linked_installation.organization_id:
            event_record.outcome = "ignored_unlinked_push"
            db.commit()
            return {"accepted": True, "duplicate": False, "repair_job_id": None}
        ref = payload.get("ref")
        safe_ref = ref if isinstance(ref, str) and 0 < len(ref) <= 255 and not any(ord(char) < 32 for char in ref) else None
        before = payload.get("before")
        after = payload.get("after")
        safe_before = before.lower() if isinstance(before, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", before) else None
        safe_after = after.lower() if isinstance(after, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", after) else None
        commits = payload.get("commits")
        commit_count = min(len(commits), 10_000) if isinstance(commits, list) else 0
        db.add(AuditLog(
            organization_id=linked_installation.organization_id,
            action="github.push.observed",
            entity_type="repository",
            entity_id=str(linked_repository.id),
            details={"ref": safe_ref, "before": safe_before, "after": safe_after, "commit_count": commit_count},
        ))
        event_record.outcome = "push_observed"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None, "commit_sha": safe_after}

    actionable, sha, summary, category = actionable_failure(event_type, payload)
    repository_data = payload.get("repository") or {}
    installation_id = installation_data.get("id")
    github_repo_id = repository_data.get("id")
    if not actionable:
        event_record.outcome = "ignored_non_failure"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}
    if not isinstance(installation_id, int) or not isinstance(github_repo_id, int):
        event_record.outcome = "ignored_missing_installation_or_repository"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}

    installation = db.scalar(select(GitHubInstallation).where(
        GitHubInstallation.installation_id == installation_id,
        GitHubInstallation.suspended.is_(False),
    ))
    repository = db.scalar(select(Repository).where(
        Repository.installation_id == installation_id,
        Repository.github_repo_id == github_repo_id,
    ))
    if not installation or not repository or repository.organization_id != installation.organization_id or not repository.monitored:
        event_record.outcome = "ignored_unlinked_or_unmonitored_repository"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}

    trigger_setting = db.get(RepositorySetting, repository.id)
    trigger_key = "workflow_run_failure" if event_type == "workflow_run" else "check_run_failure"
    if not _repository_triggers(trigger_setting)[trigger_key]:
        event_record.outcome = "ignored_trigger_disabled"
        db.commit()
        return {"accepted": True, "duplicate": False, "repair_job_id": None}

    check_suite = payload.get("check_suite") or {}
    workflow_run = payload.get("workflow_run") or {}
    check_run = payload.get("check_run") or {}
    branch_candidate = workflow_run.get("head_branch") or check_run.get("head_branch") or check_suite.get("head_branch")
    failure_branch = (
        branch_candidate
        if isinstance(branch_candidate, str) and 0 < len(branch_candidate) <= 255 and not any(ord(char) < 32 for char in branch_candidate)
        else repository.default_branch
    )
    job = RepairJob(
        organization_id=installation.organization_id,
        repository_id=repository.id,
        idempotency_key=delivery_id,
        status="RECEIVED",
        commit_sha=sha,
        base_branch=failure_branch,
        failure_category=category,
        failure_summary=summary,
        workflow_run_id=(payload.get("workflow_run") or {}).get("id") if event_type == "workflow_run" and isinstance((payload.get("workflow_run") or {}).get("id"), int) else None,
        check_run_id=(payload.get("check_run") or {}).get("id") if event_type == "check_run" and isinstance((payload.get("check_run") or {}).get("id"), int) else None,
        source_event_id=event_record.id,
    )
    db.add(job)
    db.flush()
    transition_job(db, job.id, "QUEUED", actor="github-webhook", detail={"event": event_type, "delivery_id": delivery_id})
    event_record.outcome = "queued"
    db.commit()

    queued = False
    try:
        job.queue_job_id = enqueue_repair(job.id)
        db.commit()
        queued = True
    except Exception:
        db.rollback()
        saved_event = db.get(WebhookEvent, event_record.id)
        saved_job = db.get(RepairJob, job.id)
        if saved_event:
            saved_event.outcome = "queue_unavailable"
        if saved_job:
            saved_job.error_message = "Redis queue unavailable; job remains queued for retry."
        db.commit()
    return {"accepted": True, "duplicate": False, "repair_job_id": str(job.id), "queued": queued}


@app.get("/api/v1/auth/me", tags=["authentication"])
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    memberships = db.scalars(select(OrganizationMember).where(OrganizationMember.user_id == user.id)).all()
    organizations = [db.get(Organization, member.organization_id) for member in memberships]
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "organizations": [
            {"id": str(org.id), "name": org.name, "role": member.role}
            for org, member in zip(organizations, memberships, strict=True)
            if org is not None
        ],
    }


@app.get("/api/v1/dashboard/overview", tags=["dashboard"])
def dashboard_overview(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    """Return counts sourced from the signed-in user's organizations only."""
    organization_ids = db.scalars(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)
    ).all()
    if not organization_ids:
        return {
            "connected_repositories": 0, "monitored_repositories": 0,
            "failures_detected": 0, "active_repairs": 0,
            "validated_repairs": 0, "pull_requests_opened": 0,
            "needs_human_review": 0, "cache_hit_rate": None,
            "validation_success_rate": None, "model_tokens": 0,
            "estimated_tokens_saved": 0,
            "estimated_cost": None, "average_pr_minutes": None,
            "recent_jobs": [],
        }

    repo_count = db.scalar(select(func.count(Repository.id)).where(Repository.organization_id.in_(organization_ids))) or 0
    monitored_count = db.scalar(select(func.count(Repository.id)).where(
        Repository.organization_id.in_(organization_ids), Repository.monitored.is_(True)
    )) or 0
    jobs = db.scalar(select(func.count(RepairJob.id)).where(RepairJob.organization_id.in_(organization_ids))) or 0
    active = db.scalar(select(func.count(RepairJob.id)).where(
        RepairJob.organization_id.in_(organization_ids),
        RepairJob.status.in_(["RECEIVED", "QUEUED", "TRIAGING", "CONTEXT_GATHERING", "CACHE_LOOKUP", "PLANNING", "PATCH_GENERATION", "SANDBOX_VALIDATION", "POLICY_CHECK", "PULL_REQUEST_CREATION", "RETRYING"]),
    )) or 0
    needs_review = db.scalar(select(func.count(RepairJob.id)).where(
        RepairJob.organization_id.in_(organization_ids), RepairJob.status == "NEEDS_HUMAN_REVIEW"
    )) or 0
    validated = db.scalar(select(func.count(distinct(ValidationRun.repair_job_id))).join(RepairJob).where(
        RepairJob.organization_id.in_(organization_ids), ValidationRun.status == "PASSED"
    )) or 0
    prs = db.scalar(select(func.count(PullRequest.id)).join(RepairJob).where(
        RepairJob.organization_id.in_(organization_ids)
    )) or 0

    lookup_rows = db.execute(select(MilisaCacheLookup.outcome).join(RepairJob).where(
        RepairJob.organization_id.in_(organization_ids)
    )).scalars().all()
    cache_hits = sum(1 for outcome in lookup_rows if outcome.upper() in {"HIT", "HIT_APPLIED", "VALIDATED_HIT", "HIT_VALIDATION_FAILED"})
    cache_rate = round(cache_hits / len(lookup_rows) * 100, 1) if lookup_rows else None
    estimated_tokens_saved = db.scalar(select(
        func.coalesce(func.sum(MilisaCacheLookup.estimated_tokens_saved), 0)
    ).join(RepairJob, RepairJob.id == MilisaCacheLookup.repair_job_id).where(
        RepairJob.organization_id.in_(organization_ids)
    )) or 0
    compatible_hits = sum(1 for outcome in lookup_rows if outcome.upper() in {"HIT", "HIT_APPLIED", "VALIDATED_HIT", "HIT_VALIDATION_FAILED"})
    validated_hits = sum(1 for outcome in lookup_rows if outcome.upper() == "VALIDATED_HIT")
    safe_reuse_rate = round(validated_hits / compatible_hits * 100, 1) if compatible_hits else None

    validation_rows = db.execute(select(ValidationRun.status).join(RepairJob).where(
        RepairJob.organization_id.in_(organization_ids)
    )).scalars().all()
    validation_passes = sum(1 for outcome in validation_rows if outcome.upper() == "PASSED")
    validation_rate = round(validation_passes / len(validation_rows) * 100, 1) if validation_rows else None

    usage = db.execute(select(
        func.coalesce(func.sum(ModelUsageRecord.prompt_tokens), 0) + func.coalesce(func.sum(ModelUsageRecord.completion_tokens), 0),
        func.sum(ModelUsageRecord.estimated_cost),
    ).where(ModelUsageRecord.organization_id.in_(organization_ids))).one()
    pr_records = db.execute(select(RepairJob.created_at, PullRequest.created_at).join(
        PullRequest, PullRequest.repair_job_id == RepairJob.id
    ).where(RepairJob.organization_id.in_(organization_ids))).all()
    latencies = [(opened - detected).total_seconds() / 60 for detected, opened in pr_records]
    average_pr_minutes = round(sum(latencies) / len(latencies), 1) if latencies else None
    recent_rows = db.execute(select(RepairJob, Repository.full_name).join(
        Repository, Repository.id == RepairJob.repository_id
    ).where(RepairJob.organization_id.in_(organization_ids)).order_by(RepairJob.created_at.desc()).limit(5)).all()

    return {
        "connected_repositories": repo_count,
        "monitored_repositories": monitored_count,
        "failures_detected": jobs,
        "active_repairs": active,
        "validated_repairs": validated,
        "pull_requests_opened": prs,
        "needs_human_review": needs_review,
        "cache_hit_rate": cache_rate,
        "safe_cache_reuse_rate": safe_reuse_rate,
        "validation_success_rate": validation_rate,
        "estimated_tokens_saved": int(estimated_tokens_saved),
        "model_tokens": int(usage[0] or 0),
        "estimated_cost": float(usage[1]) if usage[1] is not None else None,
        "average_pr_minutes": average_pr_minutes,
        "recent_jobs": [
            {"id": str(job.id), "repository": repo_name, "status": job.status,
             "summary": job.failure_summary, "created_at": job.created_at.isoformat()}
            for job, repo_name in recent_rows
        ],
    }


@app.get("/api/v1/model/config", tags=["model"])
def model_configuration(user: User = Depends(current_user)) -> dict[str, object]:
    parsed = urlparse(settings.nebius_base_url) if settings.nebius_base_url else None
    millisa_parsed = urlparse(settings.millisa_base_url) if settings.millisa_base_url else None
    millisa_configured = bool(settings.millisa_api_key and settings.millisa_model and millisa_parsed and millisa_parsed.scheme == "https")
    return {
        "provider": "Nebius Token Factory",
        "configured": bool(settings.nebius_api_key and settings.nebius_model and parsed and parsed.scheme == "https"),
        "model_id": settings.nebius_model or None,
        "endpoint_host": parsed.hostname if parsed else None,
        "coder_provider": "Millisa Gateway" if millisa_configured else "Nebius Token Factory",
        "coder_model_id": settings.millisa_model if millisa_configured else settings.nebius_model or None,
        "millisa_configured": millisa_configured,
        "millisa_endpoint_host": millisa_parsed.hostname if millisa_configured else None,
        "input_price_per_million": settings.nebius_input_price_per_million,
        "output_price_per_million": settings.nebius_output_price_per_million,
    }


@app.get("/api/v1/milisa/gateway/models", tags=["milisa"])
def millisa_gateway_models(user: User = Depends(current_user)) -> list[dict[str, object]]:
    try:
        return MillisaClient().list_models()
    except InferenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/milisa/gateway/usage", tags=["milisa"])
def millisa_gateway_usage(user: User = Depends(current_user)) -> dict[str, object]:
    try:
        return MillisaClient().get_usage()
    except InferenceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/v1/model/usage", tags=["model"])
def model_usage(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    organization_ids = db.scalars(select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)).all()
    if not organization_ids:
        return []
    rows = db.scalars(select(ModelUsageRecord).where(
        ModelUsageRecord.organization_id.in_(organization_ids),
    ).order_by(ModelUsageRecord.created_at.desc()).limit(limit)).all()
    return [{
        "provider": row.provider, "model_id": row.model_id,
        "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
        "estimated_cost": row.estimated_cost, "created_at": row.created_at.isoformat(),
    } for row in rows]


@app.get("/api/v1/milisa/patterns", tags=["milisa"])
def list_milisa_patterns(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    organization_ids = db.scalars(select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)).all()
    if not organization_ids:
        return []
    rows = db.scalars(select(MilisaFixPattern).where(
        MilisaFixPattern.verification_status == "VERIFIED",
        (MilisaFixPattern.organization_id.is_(None) | MilisaFixPattern.organization_id.in_(organization_ids)),
    ).order_by(MilisaFixPattern.updated_at.desc()).limit(limit)).all()
    return [{
        "id": str(row.id), "failure_category": row.failure_category,
        "language": row.language, "framework": row.framework,
        "root_cause": row.root_cause, "verification_status": row.verification_status,
        "success_count": row.success_count, "reuse_count": row.reuse_count,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
    } for row in rows]


@app.delete("/api/v1/milisa/patterns/{pattern_id}", tags=["milisa"])
def invalidate_milisa_pattern(
    pattern_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    pattern = db.get(MilisaFixPattern, pattern_id)
    if not pattern or pattern.organization_id is None:
        raise HTTPException(status_code=404, detail="Verified pattern not found")
    membership = db.scalar(select(OrganizationMember).where(
        OrganizationMember.organization_id == pattern.organization_id,
        OrganizationMember.user_id == user.id,
    ))
    if not membership or membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=404, detail="Verified pattern not found")
    if pattern.verification_status != "VERIFIED":
        raise HTTPException(status_code=409, detail="Only verified patterns can be invalidated")
    pattern.verification_status = "INVALIDATED"
    db.add(AuditLog(
        organization_id=pattern.organization_id, user_id=user.id,
        action="milisa.pattern_invalidated", entity_type="milisa_fix_pattern", entity_id=str(pattern.id),
    ))
    db.commit()
    return {"id": str(pattern.id), "verification_status": pattern.verification_status}


@app.get("/api/v1/milisa/patterns/{pattern_id}", tags=["milisa"])
def get_milisa_pattern(
    pattern_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    pattern = db.get(MilisaFixPattern, pattern_id)
    if not pattern or pattern.verification_status != "VERIFIED":
        raise HTTPException(status_code=404, detail="Verified pattern not found")
    organization_ids = db.scalars(select(OrganizationMember.organization_id).where(
        OrganizationMember.user_id == user.id,
    )).all()
    if not organization_ids or (
        pattern.organization_id is not None and pattern.organization_id not in organization_ids
    ):
        raise HTTPException(status_code=404, detail="Verified pattern not found")
    return {
        "id": str(pattern.id), "failure_category": pattern.failure_category,
        "language": pattern.language, "framework": pattern.framework,
        "root_cause": pattern.root_cause, "patch_template": pattern.patch_template,
        "regression_reference": pattern.regression_reference,
        "version_constraints": pattern.version_constraints,
        "context_fingerprint": pattern.context_fingerprint,
        "error_signature": pattern.error_signature,
        "validation_metadata": pattern.validation_metadata,
        "source_revision": pattern.source_revision, "target_revision": pattern.target_revision,
        "verification_status": pattern.verification_status,
        "success_count": pattern.success_count, "reuse_count": pattern.reuse_count,
        "created_at": pattern.created_at.isoformat(),
        "last_used_at": pattern.last_used_at.isoformat() if pattern.last_used_at else None,
        "expires_at": pattern.expires_at.isoformat() if pattern.expires_at else None,
    }


@app.get("/api/v1/audit-logs", tags=["operations"])
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    organization_ids = db.scalars(select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)).all()
    if not organization_ids:
        return []
    rows = db.scalars(select(AuditLog).where(
        AuditLog.organization_id.in_(organization_ids),
    ).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return [{
        "id": str(row.id), "action": row.action, "entity_type": row.entity_type,
        "entity_id": row.entity_id, "created_at": row.created_at.isoformat(),
    } for row in rows]
