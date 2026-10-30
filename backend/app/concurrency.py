from __future__ import annotations

import threading
import time
from uuid import uuid4

from redis import Redis

ACQUIRE_SLOT = """
local now = tonumber(ARGV[1])
local expires = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local token = ARGV[4]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], token) then
  redis.call('ZADD', KEYS[1], expires, token)
  redis.call('EXPIRE', KEYS[1], math.ceil((expires - now) / 1000) + 30)
  return 1
end
if redis.call('ZCARD', KEYS[1]) >= limit then return 0 end
redis.call('ZADD', KEYS[1], expires, token)
redis.call('EXPIRE', KEYS[1], math.ceil((expires - now) / 1000) + 30)
return 1
"""

RENEW_SLOT = """
local now = tonumber(ARGV[1])
local expires = tonumber(ARGV[2])
local token = ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if not redis.call('ZSCORE', KEYS[1], token) then return 0 end
redis.call('ZADD', KEYS[1], expires, token)
redis.call('EXPIRE', KEYS[1], math.ceil((expires - now) / 1000) + 30)
return 1
"""

RELEASE_SLOT = "return redis.call('ZREM', KEYS[1], ARGV[1])"


class OrganizationRepairSlot:
    """A renewable Redis lease that caps active repair pipelines per organization."""

    def __init__(self, client: Redis, organization_id: str, limit: int, lease_seconds: int) -> None:
        self.client = client
        self.key = f"self-healing:repair-slots:{organization_id}"
        self.limit = limit
        self.lease_seconds = lease_seconds
        self.token = uuid4().hex
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def acquire(self) -> bool:
        now = int(time.time() * 1000)
        expires = now + self.lease_seconds * 1000
        acquired = self.client.eval(
            ACQUIRE_SLOT, 1, self.key, now, expires, self.limit, self.token,
        ) == 1
        if acquired:
            self._heartbeat = threading.Thread(target=self._renew_loop, name="repair-slot-heartbeat", daemon=True)
            self._heartbeat.start()
        return acquired

    def release(self) -> None:
        self._stop.set()
        if self._heartbeat:
            self._heartbeat.join(timeout=2)
        try:
            self.client.eval(RELEASE_SLOT, 1, self.key, self.token)
        except Exception:
            # The lease expires automatically if Redis cannot accept release.
            pass

    def _renew_loop(self) -> None:
        interval = max(5, self.lease_seconds // 3)
        while not self._stop.wait(interval):
            now = int(time.time() * 1000)
            expires = now + self.lease_seconds * 1000
            try:
                renewed = self.client.eval(RENEW_SLOT, 1, self.key, now, expires, self.token)
            except Exception:
                self.lost.set()
                return
            if renewed != 1:
                self.lost.set()
                return
