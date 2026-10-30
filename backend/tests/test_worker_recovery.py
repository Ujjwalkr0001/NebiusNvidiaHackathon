from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.models import AgentRun, AuditLog, Base, Organization, RepairJob


def test_stale_worker_recovery_parks_job_and_closes_agent_run(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)
    with sessions() as db:
        organization = Organization(name="Acme")
        db.add(organization)
        db.flush()
        job = RepairJob(
            organization_id=organization.id, repository_id=uuid4(), idempotency_key="delivery",
            status="PLANNING", commit_sha="a" * 40, base_branch="main",
            failure_category="test", failure_summary="failed",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(job)
        db.flush()
        run = AgentRun(
            repair_job_id=job.id, agent_name="repo_architect", status="RUNNING",
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(run)
        db.commit()

    assert worker.recover_stale_jobs() == 1

    with sessions() as db:
        job = db.scalar(select(RepairJob))
        run = db.scalar(select(AgentRun))
        assert job is not None and job.status == "NEEDS_HUMAN_REVIEW"
        assert run is not None and run.status == "ABANDONED"
        assert db.scalar(select(AuditLog)) is not None
    Base.metadata.drop_all(engine)
    engine.dispose()
