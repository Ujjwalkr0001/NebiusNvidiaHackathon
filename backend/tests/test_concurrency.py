from datetime import timedelta
from threading import Event
from uuid import uuid4
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.worker as worker
from app.concurrency import ACQUIRE_SLOT, RELEASE_SLOT, RENEW_SLOT, OrganizationRepairSlot
from app.models import Base, Organization, RepairJob, RepairJobEvent, Repository
from app.settings import settings
import app.queueing as queueing


class FakeRedis:
    def __init__(self):
        self.members: dict[str, dict[str, int]] = {}

    def eval(self, script, _key_count, key, *args):
        members = self.members.setdefault(key, {})
        if script == ACQUIRE_SLOT:
            now, expires, limit, token = int(args[0]), int(args[1]), int(args[2]), args[3]
            for name, score in list(members.items()):
                if score <= now:
                    members.pop(name)
            if token not in members and len(members) >= limit:
                return 0
            members[token] = expires
            return 1
        if script == RENEW_SLOT:
            now, expires, token = int(args[0]), int(args[1]), args[2]
            for name, score in list(members.items()):
                if score <= now:
                    members.pop(name)
            if token not in members:
                return 0
            members[token] = expires
            return 1
        if script == RELEASE_SLOT:
            return int(members.pop(args[0], None) is not None)
        raise AssertionError("unexpected Redis script")


def test_organization_repair_slots_are_atomic_and_expiring() -> None:
    redis = FakeRedis()
    first = OrganizationRepairSlot(redis, "org-a", limit=1, lease_seconds=30)
    second = OrganizationRepairSlot(redis, "org-a", limit=1, lease_seconds=30)
    other_org = OrganizationRepairSlot(redis, "org-b", limit=1, lease_seconds=30)
    assert first.acquire()
    assert not second.acquire()
    assert other_org.acquire()
    assert first.client.eval(RENEW_SLOT, 1, first.key, 0, 60_000, first.token) == 1
    first.release()
    assert second.acquire()
    second.release()
    other_org.release()
    expiry_slot = OrganizationRepairSlot(redis, "org-expiry", limit=1, lease_seconds=30)
    redis.members[expiry_slot.key] = {"crashed-worker": 1}
    assert expiry_slot.acquire()
    expiry_slot.release()


def test_repair_waiting_for_org_slot_is_delayed_and_persisted(monkeypatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(worker, "SessionLocal", sessions)

    class FakeConnection:
        def close(self):
            pass

    class FullSlot:
        def __init__(self, *_args, **_kwargs):
            self.lost = Event()

        def acquire(self):
            return False

        def release(self):
            raise AssertionError("a busy slot must not be released")

    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(worker, "OrganizationRepairSlot", FullSlot)
    deferred = []
    monkeypatch.setattr(worker, "enqueue_repair_after_capacity_wait", lambda job_id: deferred.append(job_id) or "delayed-job-1")

    with sessions() as db:
        org = Organization(name="Capacity org")
        db.add(org)
        db.flush()
        repo = Repository(
            organization_id=org.id, installation_id=42, github_repo_id=77,
            full_name="capacity/repo", default_branch="main", private=True, monitored=True,
        )
        db.add(repo)
        db.flush()
        job = RepairJob(
            organization_id=org.id, repository_id=repo.id,
            idempotency_key="capacity-job", status="QUEUED", commit_sha="a" * 40,
            base_branch="main", failure_category="test", failure_summary="AssertionError",
            queue_job_id="original-job",
        )
        db.add(job)
        db.commit()
        repair_id = job.id

    worker.process_repair_job(str(repair_id))

    with sessions() as db:
        job = db.get(RepairJob, repair_id)
        event = db.scalar(select(RepairJobEvent).where(RepairJobEvent.repair_job_id == repair_id))
        assert job is not None and job.status == "QUEUED" and job.queue_job_id == "delayed-job-1"
        assert deferred == [repair_id]
        assert event is not None and event.from_state == event.to_state == "QUEUED"
        assert event.detail["reason"] == "organization_concurrency_limit"
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_capacity_requeue_uses_scheduler_delay_and_worker_enables_scheduler(monkeypatch) -> None:
    class FakeConnection:
        def ping(self):
            pass

        def close(self):
            pass

    calls = {}

    class FakeQueue:
        def __init__(self, name, *, connection, default_timeout=None):
            calls.update(name=name, connection=connection, default_timeout=default_timeout)

        def enqueue_in(self, delay, function, *args, **kwargs):
            calls.update(delay=delay, function=function, args=args, kwargs=kwargs)
            return type("Job", (), {"id": kwargs["job_id"]})()

    connection = FakeConnection()
    monkeypatch.setattr(queueing.Redis, "from_url", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(queueing, "Queue", FakeQueue)
    monkeypatch.setattr(settings, "repair_queue_wait_seconds", 17)
    monkeypatch.setattr(settings, "repair_job_timeout_seconds", 1200)
    repair_id = uuid4()
    deferred_id = queueing.enqueue_repair_after_capacity_wait(repair_id)
    assert deferred_id == calls["kwargs"]["job_id"]
    assert calls["delay"] == timedelta(seconds=17)
    assert calls["function"] == "app.worker.process_repair_job"
    assert calls["args"] == (str(repair_id),)
    assert calls["kwargs"]["job_timeout"] == 1200

    scheduler = {}

    class FakeWorker:
        def __init__(self, queues, *, connection):
            scheduler.update(queues=queues, connection=connection)

        def work(self, **kwargs):
            scheduler["work_options"] = kwargs

    monkeypatch.setattr(worker, "recover_stale_jobs", lambda: 0)
    monkeypatch.setattr(worker.Redis, "from_url", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(worker, "Queue", FakeQueue)
    monkeypatch.setattr(worker, "Worker", FakeWorker)
    worker.run_worker()
    assert scheduler["work_options"]["with_scheduler"] is True
