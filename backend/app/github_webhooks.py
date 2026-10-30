import hashlib
import hmac
import json
import re


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def payload_digest(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def parse_payload(raw_body: bytes) -> dict:
    value = json.loads(raw_body)
    if not isinstance(value, dict):
        raise ValueError("Webhook payload must be an object")
    return value


def actionable_failure(event_type: str, payload: dict) -> tuple[bool, str, str, str]:
    """Return actionable, sha, summary, category for supported failed checks."""
    if event_type == "workflow_run":
        run = payload.get("workflow_run") or {}
        if payload.get("action") != "completed" or run.get("conclusion") not in {"failure", "timed_out"}:
            return False, "", "", ""
        sha = str(run.get("head_sha") or "")
        summary = redact_text(f"Workflow {run.get('name') or run.get('display_title') or 'run'} failed ({run.get('conclusion')})")
        category = classify_failure(summary)
    elif event_type == "check_run":
        check = payload.get("check_run") or {}
        if payload.get("action") != "completed" or check.get("conclusion") not in {"failure", "timed_out"}:
            return False, "", "", ""
        sha = str(check.get("head_sha") or "")
        output = check.get("output") or {}
        title = str(output.get("title") or check.get("name") or "Check failed")
        message = str(output.get("summary") or "")
        summary = redact_text(f"{title}: {message} ({check.get('conclusion')})")[:3000]
        category = classify_failure(summary)
    else:
        return False, "", "", ""
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", sha):
        return False, "", "", ""
    return True, sha.lower(), summary, category


def classify_failure(text: str) -> str:
    message = text.lower()
    if any(token in message for token in ("flaky", "flakiness", "non-deterministic", "nondeterministic", "passed on retry", "intermittent failure")):
        return "flaky"
    if any(token in message for token in (
        "timed out", "timeout", "rate limit", "permission denied", "service unavailable",
        "runner lost", "network error", "connection reset", "connection refused",
        "no space left", "out of memory", "unable to download", "checkout failed",
        "resource temporarily unavailable", "api rate limit exceeded",
    )):
        return "infrastructure"
    if any(token in message for token in (
        "modulenotfounderror", "no module named", "could not find a version that satisfies",
        "dependency conflict", "resolutionimpossible", "failed to resolve dependencies",
    )):
        return "dependency"
    if any(token in message for token in (
        "invalid configuration", "configuration error", "yamlerror", "tomldecodeerror",
        "missing required environment variable", "field required",
    )):
        return "configuration"
    if any(token in message for token in ("assertionerror", "test failed", "tests failed", "pytest", "unittest")):
        return "test"
    if any(token in message for token in ("syntaxerror", "compile error", "compilation failed", "build failed", "cannot find module")):
        return "build"
    if any(token in message for token in ("lint", "flake8", "ruff", "mypy", "static analysis")):
        return "lint"
    if any(token in message for token in (
        "traceback (most recent call last)", "nameerror", "typeerror", "runtimeerror",
        "attributeerror", "keyerror", "indexerror", "valueerror",
    )):
        return "runtime"
    return "unknown"


def redact_text(text: str) -> str:
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer\s+)\S+", r"\1[REDACTED]"),
        (r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]"),
        (r"gh[pousr]_[A-Za-z0-9_]{20,}", "[REDACTED_GITHUB_TOKEN]"),
        (r"github_pat_[A-Za-z0-9_]{20,}", "[REDACTED_GITHUB_TOKEN]"),
        (r"AKIA[0-9A-Z]{16}", "[REDACTED_AWS_KEY]"),
        (r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]"),
    )
    safe = text
    for pattern, replacement in patterns:
        safe = re.sub(pattern, replacement, safe)
    return safe
