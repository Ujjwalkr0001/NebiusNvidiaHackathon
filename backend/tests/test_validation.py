import pytest
import subprocess

import app.validation as validation_module
from app.settings import settings
from app.validation import _parse_checks, _safe_image, _safe_memory_value, _safe_pytest_selector, _safe_required_checks, _safe_resource_value, _safe_tmpfs_size, run_python_validation


def test_validation_records_structured_check_outcomes() -> None:
    checks = _parse_checks(
        "__TEST_STATUS__=0\n__PATCH_STATUS__=0\n__COMPILE_STATUS__=1\n"
        "__FOCUSED_TEST_STATUS__=125\n__TEST_STATUS__=125\n"
        "__RUFF_STATUS__=0\n__MYPY_STATUS__=125\n__BUILD_STATUS__=1\n"
    )
    assert checks == [
        {"name": "Apply patch", "outcome": "PASSED"},
        {"name": "Python compileall", "outcome": "FAILED"},
        {"name": "Original failing test", "outcome": "SKIPPED"},
        {"name": "Pytest suite", "outcome": "SKIPPED"},
        {"name": "Ruff lint", "outcome": "PASSED"},
        {"name": "Mypy static analysis", "outcome": "SKIPPED"},
        {"name": "Python package build", "outcome": "FAILED"},
    ]


def test_validation_resource_settings_are_allowlisted() -> None:
    assert _safe_image("registry.example/repair/python:3.12") == "registry.example/repair/python:3.12"
    assert _safe_resource_value("2", {"1", "2"}) == "2"
    assert _safe_memory_value("2g") == "2g"
    assert _safe_tmpfs_size("512m", {"256m", "512m"}) == "512m"
    for invalid in ("--privileged", "image name", "", "sha256:bad"):
        with pytest.raises(ValueError):
            _safe_image(invalid)
    with pytest.raises(ValueError):
        _safe_resource_value("10; --privileged", {"1", "2"})
    with pytest.raises(ValueError):
        _safe_memory_value("100g")
    with pytest.raises(ValueError):
        _safe_tmpfs_size("10g", {"256m", "512m"})
    assert _safe_required_checks(["build", "ruff"]) == ["ruff", "build"]
    with pytest.raises(ValueError):
        _safe_required_checks(["ruff; touch bad"])


def test_failing_test_selector_is_bounded_and_repository_relative() -> None:
    assert _safe_pytest_selector("tests/test_math.py::TestSum::test_adds[case-1]") == "tests/test_math.py::TestSum::test_adds[case-1]"
    for selector in (
        "../../outside.py::test_run", "-danger.py::test_run", "tests/test_math.py",
        "tests\\test_math.py::test_run", "tests/test_math.py::test_run;touch",
    ):
        with pytest.raises(ValueError):
            _safe_pytest_selector(selector)


class FakePipe:
    def __init__(self, data: bytes):
        self.data = data

    def read1(self, _size: int) -> bytes:
        data, self.data = self.data, b""
        return data

    def close(self) -> None:
        pass


class FakeProcess:
    def __init__(self, data: bytes, *, timeout: bool = False):
        self.stdout = FakePipe(data)
        self.returncode = None if timeout else 0
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: int | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_runner_applies_resource_flags_and_records_passed_checks(tmp_path, monkeypatch) -> None:
    root = tmp_path / "job"
    source = root / "repository"
    source.mkdir(parents=True)
    patch = root / "patch.diff"
    patch.write_text("diff\n")
    monkeypatch.setattr(settings, "validation_image", "trusted/python:3.12")
    monkeypatch.setattr(settings, "validation_cpu_limit", "2")
    monkeypatch.setattr(settings, "validation_memory_limit", "1g")
    monkeypatch.setattr(settings, "validation_workspace_limit", "512m")
    monkeypatch.setattr(settings, "validation_temp_limit", "256m")
    monkeypatch.setattr(settings, "validation_timeout_seconds", 300)
    process = FakeProcess(b"__PATCH_STATUS__=0\n__COMPILE_STATUS__=0\n__FOCUSED_TEST_STATUS__=0\n__TEST_STATUS__=0\n__RUFF_STATUS__=0\n")
    seen = {}

    def popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr(validation_module.subprocess, "Popen", popen)
    monkeypatch.setattr(validation_module, "_remove_container", lambda name: None)
    result = run_python_validation(source, patch, test_selector="tests/test_app.py::test_value", required_checks=["ruff"])
    assert result.status == "PASSED"
    assert [item["outcome"] for item in result.checks] == ["PASSED", "PASSED", "PASSED", "PASSED", "PASSED"]
    assert result.environment["failing_test_selector"] == "tests/test_app.py::test_value"
    assert result.environment["required_checks"] == ["ruff"]
    assert result.environment["workspace_limit"] == "512m"
    assert result.environment["temporary_storage_limit"] == "256m"
    assert result.environment["pids_limit"] == 128
    assert result.environment["output_limit_bytes"] == validation_module.MAX_VALIDATION_OUTPUT_BYTES
    assert "none" in seen["command"] and "--memory" in seen["command"] and "--shm-size" in seen["command"]
    assert not any("chmod -R" in item for item in seen["command"])
    assert "/workspace:rw,noexec,nosuid,size=512m,mode=1777" in seen["command"]
    assert "/tmp:rw,noexec,nosuid,size=256m,mode=1777" in seen["command"]
    assert any("python -m ruff check ." in item for item in seen["command"])
    assert seen["command"][-2:] == ["tests/test_app.py::test_value", "ruff"]
    assert seen["kwargs"]["shell"] is False


def test_runner_stops_and_kills_container_on_timeout(tmp_path, monkeypatch) -> None:
    root = tmp_path / "job"
    source = root / "repository"
    source.mkdir(parents=True)
    patch = root / "patch.diff"
    patch.write_text("diff\n")
    monkeypatch.setattr(settings, "validation_image", "trusted/python:3.12")
    process = FakeProcess(b"", timeout=True)
    monkeypatch.setattr(settings, "validation_timeout_seconds", 30)
    monkeypatch.setattr(validation_module.subprocess, "Popen", lambda *args, **kwargs: process)
    ticks = iter([0.0, 31.0, 32.0])
    monkeypatch.setattr(validation_module.time, "monotonic", lambda: next(ticks))
    killed = []
    monkeypatch.setattr(validation_module, "_kill_container", lambda name: killed.append(name))
    monkeypatch.setattr(validation_module, "_remove_container", lambda name: None)
    result = run_python_validation(source, patch)
    assert result.status == "TIMED_OUT"
    assert process.killed and killed


def test_runner_stops_container_when_job_is_cancelled(tmp_path, monkeypatch) -> None:
    root = tmp_path / "job"
    source = root / "repository"
    source.mkdir(parents=True)
    patch = root / "patch.diff"
    patch.write_text("diff\n")
    monkeypatch.setattr(settings, "validation_image", "trusted/python:3.12")
    process = FakeProcess(b"", timeout=True)
    monkeypatch.setattr(validation_module.subprocess, "Popen", lambda *args, **kwargs: process)
    killed = []
    monkeypatch.setattr(validation_module, "_kill_container", lambda name: killed.append(name))
    monkeypatch.setattr(validation_module, "_remove_container", lambda name: None)
    result = run_python_validation(source, patch, cancelled=lambda: True)
    assert result.status == "CANCELLED" and process.killed and killed
