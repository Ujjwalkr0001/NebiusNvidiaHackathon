from collections.abc import Generator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.job_state import transition_job
from app.models import Base, Organization, PullRequest, RepairJob, RepairJobEvent, Repository


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def make_job(db: Session) -> RepairJob:
    organization = Organization(name="Org")
    db.add(organization)
    db.flush()
    repository = Repository(
        organization_id=organization.id,
        installation_id=1,
        github_repo_id=1,
        full_name="org/repo",
        default_branch="main",
    )
    db.add(repository)
    db.flush()
    job = RepairJob(
        organization_id=organization.id,
        repository_id=repository.id,
        idempotency_key=str(uuid4()),
        status="RECEIVED",
        commit_sha="d" * 40,
        base_branch="main",
        failure_category="test",
    )
    db.add(job)
    db.commit()
    return job


def test_job_states_are_persisted_and_must_follow_the_graph(db: Session) -> None:
    job = make_job(db)
    transition_job(db, job.id, "QUEUED", actor="test")
    db.commit()
    assert job.status == "QUEUED"
    event = db.scalar(select(RepairJobEvent).where(RepairJobEvent.repair_job_id == job.id))
    assert event is not None and event.from_state == "RECEIVED" and event.to_state == "QUEUED"
    with pytest.raises(HTTPException) as error:
        transition_job(db, job.id, "COMPLETED", actor="test")
    assert error.value.status_code == 409


def test_completion_requires_a_persisted_pull_request(db: Session) -> None:
    job = make_job(db)
    steps = [
        "QUEUED", "TRIAGING", "CONTEXT_GATHERING", "CACHE_LOOKUP", "PLANNING",
        "PATCH_GENERATION", "SANDBOX_VALIDATION", "POLICY_CHECK", "PULL_REQUEST_CREATION",
    ]
    for step in steps:
        transition_job(db, job.id, step, actor="test")
    with pytest.raises(HTTPException) as error:
        transition_job(db, job.id, "COMPLETED", actor="test")
    assert error.value.status_code == 409

    db.add(PullRequest(
        repair_job_id=job.id,
        github_pr_id=123,
        number=5,
        url="https://github.com/org/repo/pull/5",
        branch_name="repair/fix-test",
    ))
    job.pr_url = "https://github.com/org/repo/pull/5"
    transition_job(db, job.id, "COMPLETED", actor="test")
    db.commit()
    assert job.status == "COMPLETED"


def test_human_review_can_retry_pull_request_creation(db: Session) -> None:
    job = make_job(db)
    transition_job(db, job.id, "NEEDS_HUMAN_REVIEW", actor="test")
    transition_job(db, job.id, "PULL_REQUEST_CREATION", actor="test", detail={"retry_requested": True})
    db.commit()
    assert job.status == "PULL_REQUEST_CREATION"
