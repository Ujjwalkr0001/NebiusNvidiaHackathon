"""Redis-backed API rate limits for unauthenticated and expensive actions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import re

from fastapi import Request
from redis import Redis

from app.settings import settings


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: int


RULES = {
    ("POST", "/api/v1/auth/login"): RateLimitRule("login", 10, 300),
    ("POST", "/api/v1/auth/register"): RateLimitRule("register", 5, 3600),
    ("POST", "/api/v1/github/installations/start"): RateLimitRule("github-install", 20, 3600),
    ("GET", "/api/v1/milisa/gateway/models"): RateLimitRule("millisa-model-list", 30, 60),
    ("GET", "/api/v1/milisa/gateway/usage"): RateLimitRule("millisa-usage", 10, 60),
}
JOB_ACTION = re.compile(r"/api/v1/repair-jobs/[0-9a-fA-F-]{36}/(?:retry-queue|retry-pull-request|cancel)\Z")
MANUAL_REPAIR = re.compile(r"/api/v1/github/repositories/[0-9a-fA-F-]{36}/repair\Z")
WEBHOOK_RULE = RateLimitRule("github-webhook", 600, 60)
JOB_ACTION_RULE = RateLimitRule("repair-job-action", 20, 900)
MANUAL_REPAIR_RULE = RateLimitRule("manual-repair", 10, 900)
REDIS_CLIENT = Redis.from_url(
    settings.redis_url,
    socket_connect_timeout=0.25,
    socket_timeout=0.25,
)

_INCREMENT_AND_EXPIRE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


def rate_limit_rule(method: str, path: str) -> RateLimitRule | None:
    exact = RULES.get((method.upper(), path))
    if exact:
        return exact
    if method.upper() == "POST" and path == "/api/v1/webhooks/github":
        return WEBHOOK_RULE
    if method.upper() == "POST" and JOB_ACTION.fullmatch(path):
        return JOB_ACTION_RULE
    if method.upper() == "POST" and MANUAL_REPAIR.fullmatch(path):
        return MANUAL_REPAIR_RULE
    return None


def _rate_limit_key(request: Request, rule: RateLimitRule) -> str:
    peer = request.client.host if request.client else "unknown"
    identity_digest = sha256(peer.encode("utf-8", errors="replace")).hexdigest()[:32]
    environment = settings.app_env.lower()
    return f"self-healing:{environment}:rate-limit:{rule.name}:{identity_digest}"


async def consume_rate_limit(request: Request, rule: RateLimitRule) -> tuple[int, int]:
    result = await asyncio.to_thread(
        REDIS_CLIENT.eval,
        _INCREMENT_AND_EXPIRE,
        1,
        _rate_limit_key(request, rule),
        rule.window_seconds,
    )
    count, ttl = result
    return int(count), max(1, int(ttl))
