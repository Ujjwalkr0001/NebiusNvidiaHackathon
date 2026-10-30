from __future__ import annotations

import subprocess
import threading
import time
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from app.github_webhooks import redact_text
from app.settings import settings

MAX_VALIDATION_OUTPUT_BYTES = 1_000_000
VALIDATION_COMMAND = (
    "cp -R /repair-source/. /workspace/ && "
    "git apply --check /repair-input/patch.diff && git apply /repair-input/patch.diff; "
    "apply_status=$?; echo __PATCH_STATUS__=$apply_status; "
    "compile_status=125; focused_status=125; test_status=125; "
    "ruff_status=125; ruff_gate=0; mypy_status=125; mypy_gate=0; build_status=125; build_gate=0; "
    "if [ \"$apply_status\" -eq 0 ]; then "
    "python -m compileall -q .; compile_status=$?; "
    "if [ -n \"$1\" ]; then python -m pytest -q \"$1\"; focused_status=$?; "
    "if [ \"$focused_status\" -eq 125 ]; then focused_status=124; fi; "
    "else focused_status=125; fi; "
    "python -m pytest -q; test_status=$?; "
    "case \",$2,\" in *,ruff,*) python -m ruff check .; ruff_status=$?; ruff_gate=$ruff_status;; esac; "
    "case \",$2,\" in *,mypy,*) python -m mypy .; mypy_status=$?; mypy_gate=$mypy_status;; esac; "
    "case \",$2,\" in *,build,*) python -m build --wheel; build_status=$?; build_gate=$build_status;; esac; "
    "fi; "
    "echo __COMPILE_STATUS__=$compile_status; echo __FOCUSED_TEST_STATUS__=$focused_status; echo __TEST_STATUS__=$test_status; "
    "echo __RUFF_STATUS__=$ruff_status; echo __MYPY_STATUS__=$mypy_status; echo __BUILD_STATUS__=$build_status; "
    "if [ \"$apply_status\" -eq 0 ] && [ \"$compile_status\" -eq 0 ] && "
    "{ [ \"$focused_status\" -eq 0 ] || [ \"$focused_status\" -eq 125 ]; } && [ \"$test_status\" -eq 0 ] && "
    "[ \"$ruff_gate\" -eq 0 ] && [ \"$mypy_gate\" -eq 0 ] && [ \"$build_gate\" -eq 0 ]; then exit 0; else exit 1; fi"
)


@dataclass(frozen=True)
class ValidationOutcome:
    status: str
    log_excerpt: str
    duration_ms: int
    environment: dict[str, object]
    checks: list[dict[str, str]]


def run_python_validation(
    repository_dir: Path,
    patch_path: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
    test_selector: str | None = None,
    required_checks: list[str] | None = None,
) -> ValidationOutcome:
    """Run fixed Python checks in a networkless, resource-limited Docker container."""
    if not settings.validation_image:
        return ValidationOutcome(
            status="NOT_CONFIGURED", log_excerpt="A trusted validation image is not configured.",
            duration_ms=0, environment={}, checks=[],
        )
    repository_dir = repository_dir.resolve(strict=True)
    patch_path = patch_path.resolve(strict=True)
    input_root = patch_path.parent.resolve(strict=True)
    if repository_dir == input_root or input_root not in repository_dir.parents:
        raise ValueError("Validation workspace paths are invalid")
    image = _safe_image(settings.validation_image)
    safe_selector = _safe_pytest_selector(test_selector)
    safe_checks = _safe_required_checks(required_checks or [])
    container_name = f"repair-validation-{uuid.uuid4().hex[:16]}"
    cpu_limit = _safe_resource_value(settings.validation_cpu_limit, {"1", "1.5", "2", "2.5", "3", "4"})
    memory_limit = _safe_memory_value(settings.validation_memory_limit)
    workspace_limit = _safe_tmpfs_size(settings.validation_workspace_limit, {"256m", "512m", "1g"})
    temp_limit = _safe_tmpfs_size(settings.validation_temp_limit, {"128m", "256m", "512m"})
    timeout_seconds = min(900, max(30, int(settings.validation_timeout_seconds)))
    command = [
        settings.validation_docker_bin, "run", "--rm", "--name", container_name,
        "--network", "none", "--cpus", cpu_limit,
        "--memory", memory_limit, "--memory-swap", memory_limit,
        "--pids-limit", "128", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={temp_limit},mode=1777",
        "--tmpfs", f"/workspace:rw,noexec,nosuid,size={workspace_limit},mode=1777",
        "--shm-size", "64m",
        "--mount", f"type=bind,src={_docker_path(repository_dir)},dst=/repair-source,readonly",
        "--mount", f"type=bind,src={_docker_path(input_root)},dst=/repair-input,readonly",
        "--workdir", "/workspace", image,
        "sh", "-c", VALIDATION_COMMAND, "validator", safe_selector, ",".join(safe_checks),
    ]
    output = bytearray()
    output_limited = threading.Event()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False)
    except OSError:
        return ValidationOutcome(
            status="ERROR", log_excerpt="Docker validation could not be started.", duration_ms=0,
            environment=_environment(
                cpu_limit, memory_limit, timeout_seconds, safe_selector, safe_checks,
                workspace_limit, temp_limit,
            ), checks=[],
        )

    def collect_output() -> None:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            if len(output) + len(chunk) > MAX_VALIDATION_OUTPUT_BYTES:
                output.extend(chunk[: max(0, MAX_VALIDATION_OUTPUT_BYTES - len(output))])
                output_limited.set()
                try:
                    process.kill()
                except OSError:
                    pass
                continue
            output.extend(chunk)

    reader = threading.Thread(target=collect_output, name="validation-output-reader", daemon=True)
    reader.start()
    started_at = time.monotonic()
    status = "ERROR"
    try:
        deadline = started_at + timeout_seconds
        while process.poll() is None:
            try:
                should_cancel = bool(cancelled and cancelled())
            except Exception:
                should_cancel = False
            if should_cancel:
                status = "CANCELLED"
                process.kill()
                _kill_container(container_name)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "TIMED_OUT"
                process.kill()
                _kill_container(container_name)
                break
            try:
                process.wait(timeout=min(1.0, remaining))
            except subprocess.TimeoutExpired:
                continue
        if status == "ERROR":
            status = "OUTPUT_LIMIT" if output_limited.is_set() else ("PASSED" if process.returncode == 0 else "FAILED")
            if process.returncode != 0 and any(
                message in output.decode("utf-8", errors="replace").lower()
                for message in ("cannot connect to the docker daemon", "error during connect", "docker engine")
            ):
                status = "ERROR"
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        status = "ERROR"
        try:
            process.kill()
        except OSError:
            pass
        _kill_container(container_name)
    finally:
        reader.join(timeout=10)
        if process.stdout:
            process.stdout.close()
    duration_ms = int((time.monotonic() - started_at) * 1000)
    log = redact_text(output.decode("utf-8", errors="replace"))[-20_000:]
    if output_limited.is_set():
        log += "\n[Validation stopped after reaching the output limit.]"
    if status == "TIMED_OUT":
        log += "\n[Validation stopped after reaching the time limit.]"
    _remove_container(container_name)
    checks = _parse_checks(output.decode("utf-8", errors="replace"))
    return ValidationOutcome(
        status=status, log_excerpt=log, duration_ms=duration_ms,
        environment=_environment(
            cpu_limit, memory_limit, timeout_seconds, safe_selector, safe_checks,
            workspace_limit, temp_limit,
        ), checks=checks,
    )


def _parse_checks(output: str) -> list[dict[str, str]]:
    names = {
        "PATCH": "Apply patch", "COMPILE": "Python compileall",
        "FOCUSED_TEST": "Original failing test", "TEST": "Pytest suite",
        "RUFF": "Ruff lint", "MYPY": "Mypy static analysis", "BUILD": "Python package build",
    }
    results: list[dict[str, str]] = []
    for key, name in names.items():
        matches = list(re.finditer(rf"(?m)^__{key}_STATUS__=(0|[1-9][0-9]*)\s*$", output))
        if matches:
            code = int(matches[-1].group(1))
            results.append({"name": name, "outcome": "PASSED" if code == 0 else ("SKIPPED" if code == 125 else "FAILED")})
    return results


def _safe_pytest_selector(value: str | None) -> str:
    if value is None or not value:
        return ""
    if len(value) > 300 or "\\" in value or value.startswith("/"):
        raise ValueError("Original failing test selector is invalid")
    path, separator, node_id = value.partition("::")
    if (
        not separator or not node_id or not path.endswith(".py")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or path.split("/", 1)[0].startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
        or not all(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*(?:\[[A-Za-z0-9_.,=:+-]{1,100}\])?", part) for part in node_id.split("::"))
    ):
        raise ValueError("Original failing test selector is invalid")
    return value


def _safe_required_checks(values: list[str]) -> list[str]:
    allowed = {"ruff", "mypy", "build"}
    if len(values) > len(allowed) or any(not isinstance(value, str) or value not in allowed for value in values):
        raise ValueError("Configured validation checks are not supported")
    return [value for value in ("ruff", "mypy", "build") if value in values]


def _safe_resource_value(value: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ValueError("Configured validation CPU limit is not supported")
    return value


def _safe_memory_value(value: str) -> str:
    if value not in {"512m", "1g", "2g", "4g"}:
        raise ValueError("Configured validation memory limit is not supported")
    return value


def _safe_tmpfs_size(value: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ValueError("Configured validation temporary storage limit is not supported")
    return value


def _safe_image(value: str) -> str:
    if (
        not value
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", value)
        or (value.lower().startswith("sha256:") and not re.fullmatch(r"sha256:[0-9a-f]{64}", value.lower()))
    ):
        raise ValueError("Configured validation image is invalid")
    return value


def _docker_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if "," in value:
        raise ValueError("Validation workspace path contains an unsupported character")
    return value


def _environment(
    cpu: str, memory: str, timeout: int, test_selector: str = "", required_checks: list[str] | None = None,
    workspace_limit: str = "512m", temp_limit: str = "256m",
) -> dict[str, object]:
    return {
        "runtime": "docker",
        "profile": "python",
        "network": "disabled",
        "cpu_limit": cpu,
        "memory_limit": memory,
        "workspace_limit": workspace_limit,
        "temporary_storage_limit": temp_limit,
        "shared_memory_limit": "64m",
        "pids_limit": 128,
        "output_limit_bytes": MAX_VALIDATION_OUTPUT_BYTES,
        "timeout_seconds": timeout,
        "image": settings.validation_image,
        "failing_test_selector": test_selector or None,
        "required_checks": required_checks or [],
    }


def _kill_container(name: str) -> None:
    try:
        subprocess.run(
            [settings.validation_docker_bin, "kill", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _remove_container(name: str) -> None:
    try:
        subprocess.run(
            [settings.validation_docker_bin, "rm", "--force", name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
