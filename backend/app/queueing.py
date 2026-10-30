from datetime import timedelta
from uuid import UUID, uuid4

from redis import Redis
from rq import Queue

from app.settings import settings


def enqueue_repair(job_id: UUID) -> str:
    connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    connection.ping()
    queue = Queue("repairs", connection=connection, default_timeout=settings.repair_job_timeout_seconds)
    job = queue.enqueue(
        "app.worker.process_repair_job", str(job_id), job_id=str(job_id),
        job_timeout=settings.repair_job_timeout_seconds, result_ttl=86400,
    )
    return job.id


def enqueue_repair_after_capacity_wait(job_id: UUID) -> str:
    connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        connection.ping()
        queue = Queue("repairs", connection=connection, default_timeout=settings.repair_job_timeout_seconds)
        deferred = queue.enqueue_in(
            timedelta(seconds=settings.repair_queue_wait_seconds),
            "app.worker.process_repair_job", str(job_id),
            job_id=f"{job_id}-capacity-{uuid4().hex}",
            job_timeout=settings.repair_job_timeout_seconds, result_ttl=86400,
        )
        return deferred.id
    finally:
        connection.close()


def enqueue_pull_request_retry(job_id: UUID, request_id: str) -> str:
    connection = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    connection.ping()
    queue = Queue("repairs", connection=connection, default_timeout=settings.repair_job_timeout_seconds)
    job = queue.enqueue(
        "app.worker.process_pull_request_retry", str(job_id),
        job_id=f"{job_id}-pr-{request_id}",
        job_timeout=settings.repair_job_timeout_seconds, result_ttl=86400,
    )
    return job.id
