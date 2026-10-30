from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.milisa import cache_identity, lookup_verified_fix, record_verified_fix
from app.models import Base, MilisaCacheLookup, MilisaFixPattern, Organization, RepairJob
from app.settings import settings


def test_milisa_identity_ignores_docs_but_tracks_python_and_dependencies() -> None:
    job = RepairJob(failure_category="test", failure_summary="AssertionError: got 42")
    base = {
        "languages": {"Python": 100},
        "source_files": {
            "README.md": "First docs version",
            "tests/test_app.py": "def test_value(): assert value() == 1",
            "src/app.py": "def value(): return 1",
            "requirements-dev.txt": "pytest==8.0.0",
        },
    }
    changed_docs = {**base, "source_files": {**base["source_files"], "README.md": "Different docs"}}
    assert cache_identity(job, base) == cache_identity(job, changed_docs)

    changed_source = {**base, "source_files": {**base["source_files"], "src/app.py": "def value(): return 2"}}
    assert cache_identity(job, base)["context_fingerprint"] != cache_identity(job, changed_source)["context_fingerprint"]

    changed_dependencies = {**base, "source_files": {**base["source_files"], "requirements-dev.txt": "pytest==9.0.0"}}
    assert cache_identity(job, base)["version_constraints"] != cache_identity(job, changed_dependencies)["version_constraints"]


def test_milisa_lookup_requires_verified_exact_compatible_context() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with sessions() as db:
        organization = Organization(name="Acme")
        db.add(organization)
        db.flush()
        job = RepairJob(
            organization_id=organization.id, repository_id=uuid4(),
            idempotency_key="delivery", status="CACHE_LOOKUP", commit_sha="a" * 40,
            base_branch="main", failure_category="test", failure_summary="AssertionError: got 42",
        )
        db.add(job)
        db.flush()
        context = {"languages": {"Python": 100}, "source_files": {"app.py": "def value(): return 1"}}
        identity = cache_identity(job, context)
        pattern = MilisaFixPattern(
            organization_id=organization.id,
            error_signature=identity["error_signature"],
            failure_category="test", language=identity["language"], framework=None,
            version_constraints=identity["version_constraints"],
            context_fingerprint=identity["context_fingerprint"],
            root_cause="Wrong value", patch_template="verified diff",
            validation_metadata={"agent_tokens": 900},
            verification_status="VERIFIED", success_count=1,
        )
        db.add(pattern)
        db.commit()

        match = lookup_verified_fix(db, job, context)
        db.refresh(pattern)
        assert match is not None and match.id == pattern.id
        lookup = db.scalar(select(MilisaCacheLookup).where(MilisaCacheLookup.repair_job_id == job.id))
        assert lookup.outcome == "HIT" and lookup.estimated_tokens_saved == 900

        pattern.verification_status = "PENDING"
        db.commit()
        assert lookup_verified_fix(db, job, context) is None

        pattern.verification_status = "VERIFIED"
        pattern.version_constraints = {"dependency_fingerprint": "incompatible"}
        db.commit()
        assert lookup_verified_fix(db, job, context) is None

        different_context = {"languages": {"Python": 100}, "source_files": {"app.py": "def value(): return 2"}}
        assert lookup_verified_fix(db, job, different_context) is None
        pattern.version_constraints = identity["version_constraints"]
        pattern.context_fingerprint = identity["context_fingerprint"]
        pattern.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert lookup_verified_fix(db, job, context) is None
        lookups = db.scalars(select(MilisaCacheLookup).where(MilisaCacheLookup.repair_job_id == job.id)).all()
        assert len(lookups) == 5
        assert sum(item.outcome == "HIT" for item in lookups) == 1
        assert sum(item.outcome == "MISS" for item in lookups) == 4
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_milisa_expiry_can_be_disabled_by_policy(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "milisa_fix_ttl_days", 0)
    with sessions() as db:
        organization = Organization(name="TTL disabled")
        db.add(organization)
        db.flush()
        job = RepairJob(
            organization_id=organization.id, repository_id=uuid4(),
            idempotency_key="ttl-disabled", status="POLICY_CHECK", commit_sha="b" * 40,
            base_branch="main", failure_category="test", failure_summary="AssertionError: failed",
        )
        db.add(job)
        db.flush()
        pattern = record_verified_fix(
            db, job, {"source_files": {"app.py": "def value(): return 1"}, "languages": {"Python": 100}},
            diff="diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new",
            root_cause="wrong value", checks=[], validation_environment={"runtime": "docker"},
        )
        assert pattern.verification_status == "VERIFIED"
        assert pattern.expires_at is None
    Base.metadata.drop_all(engine)
    engine.dispose()

