import base64
import io
import json
import zipfile

import pytest
import httpx

from app.github_repo import GitHubRepositoryClient, GitHubRepositoryError, _extract_failing_test_selector


def test_context_fetch_is_targeted_bounded_and_redacts_secrets() -> None:
    contents = base64.b64encode(
        b"API_KEY=ghp_abcdefghijklmnopqrstuvwxyz0123456789\nimport os\n\ndef load_value():\n    return 1\n"
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits/" + "a" * 40):
            return httpx.Response(200, json={
                "commit": {"message": "Fix configuration"},
                "files": [
                    {"filename": "src/app.py", "status": "modified", "additions": 2, "deletions": 1, "patch": "@@ -1 +1 @@"},
                    {"filename": ".env", "status": "modified", "additions": 1, "deletions": 0},
                ],
            })
        if path.endswith("/contents/src/app.py"):
            return httpx.Response(200, json={"encoding": "base64", "content": contents})
        if path.endswith("/languages"):
            return httpx.Response(200, json={"Python": 1000})
        raise AssertionError(f"Unexpected GitHub API request: {request.url}")

    client = GitHubRepositoryClient("installation-token", transport=httpx.MockTransport(handler))
    context = client.gather_context(
        "acme/project", "a" * 40,
        "pytest failed\nFAILED tests/test_app.py::test_value - AssertionError", None,
    )
    assert list(context["source_files"]) == ["src/app.py"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in context["source_files"]["src/app.py"]
    assert context["languages"] == {"Python": 1000}
    assert context["python_analysis"]["src/app.py"]["imports"] == ["os"]
    assert context["python_analysis"]["src/app.py"]["definitions"] == ["load_value"]
    assert context["failing_test_selector"] == "tests/test_app.py::test_value"
    assert ".env" not in {item["path"] for item in context["changed_files"]}


def test_docs_only_failure_context_includes_tested_local_source() -> None:
    files = {
        "README.md": "Updated demo instructions\n",
        "tests/test_greetings.py": "from src.greetings import greeting\n\ndef test_greeting():\n    assert greeting('sam') == 'Hello, Sam!'\n",
        "src/greetings.py": "from .formatting import format_greeting\n\ndef greeting(name):\n    return format_greeting(name)\n",
        "src/formatting.py": "def format_greeting(name):\n    return f'Hello, {name.title()}!'\n",
        "requirements-dev.txt": "pytest==8.0.0\n",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits/" + "c" * 40):
            return httpx.Response(200, json={
                "commit": {"message": "Update documentation", "tree": {"sha": "d" * 40}},
                "files": [{"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0}],
            })
        if path.endswith("/git/trees/" + "d" * 40):
            return httpx.Response(200, json={"tree": [
                {"type": "blob", "path": name} for name in files
            ]})
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            content = files.get(name)
            if content is not None:
                return httpx.Response(200, json={
                    "encoding": "base64", "content": base64.b64encode(content.encode()).decode(),
                })
        if path.endswith("/languages"):
            return httpx.Response(200, json={"Python": 1000})
        raise AssertionError(f"Unexpected GitHub API request: {request.url}")

    client = GitHubRepositoryClient("installation-token", transport=httpx.MockTransport(handler))
    context = client.gather_context("acme/project", "c" * 40, "pytest failed", None)
    assert context["source_files"]["src/greetings.py"] == files["src/greetings.py"]
    assert context["source_files"]["src/formatting.py"] == files["src/formatting.py"]
    assert context["source_files"]["tests/test_greetings.py"] == files["tests/test_greetings.py"]
    assert context["source_files"]["requirements-dev.txt"] == files["requirements-dev.txt"]


def test_context_rejects_invalid_repository_and_commit() -> None:
    client = GitHubRepositoryClient("installation-token", transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    for repo, sha in (("../../secret", "a" * 40), ("acme/project", "bad-sha")):
        try:
            client.gather_context(repo, sha, "failure", None)
        except Exception as exc:
            assert "invalid" in str(exc).lower()
        else:
            raise AssertionError("Invalid repository context should be rejected")


def test_failing_pytest_node_is_extracted_only_when_safe() -> None:
    assert _extract_failing_test_selector(
        "FAILED tests/test_math.py::TestSum::test_adds[param-1] - AssertionError"
    ) == "tests/test_math.py::TestSum::test_adds[param-1]"
    assert _extract_failing_test_selector("\x1b[31mFAILED tests/test_math.py::test_value - AssertionError\x1b[0m") == "tests/test_math.py::test_value"
    assert _extract_failing_test_selector("FAILED ../../evil.py::test_run;touch - failed") is None
    assert _extract_failing_test_selector("FAILED tests/test_math.py - collection error") is None


def test_github_api_rate_limit_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"Python": 100})

    monkeypatch.setattr("app.github_repo.time.sleep", lambda delay: sleeps.append(delay))
    client = GitHubRepositoryClient("installation-token", transport=httpx.MockTransport(handler))
    assert client.get_json("/repos/acme/project/languages") == {"Python": 100}
    assert calls == 2
    assert sleeps == [0.0]


def test_github_api_server_errors_stop_after_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    monkeypatch.setattr("app.github_repo.time.sleep", lambda _delay: None)
    client = GitHubRepositoryClient("installation-token", transport=httpx.MockTransport(handler))
    with pytest.raises(GitHubRepositoryError, match="HTTP 503"):
        client.get_json("/repos/acme/project/languages")
    assert calls == 3


def test_source_archive_extraction_rejects_traversal_and_skips_env_files(tmp_path) -> None:
    safe_buffer = io.BytesIO()
    with zipfile.ZipFile(safe_buffer, "w") as zipped:
        zipped.writestr("acme-project-main/src/app.py", "print('ok')\n")
        zipped.writestr("acme-project-main/.env.production", "SECRET=value\n")
    output = tmp_path / "safe"
    GitHubRepositoryClient._extract_archive(safe_buffer.getvalue(), output)
    assert (output / "src" / "app.py").read_text() == "print('ok')\n"
    assert not (output / ".env.production").exists()

    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as zipped:
        zipped.writestr("acme-project-main/../../outside.txt", "no")
    with pytest.raises(Exception, match="unsafe path"):
        GitHubRepositoryClient._extract_archive(malicious.getvalue(), tmp_path / "unsafe")


def test_pull_request_writer_creates_scoped_blob_tree_commit_and_draft(tmp_path) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.method == "GET" and request.url.path.endswith("/git/commits/" + "a" * 40):
            return httpx.Response(200, json={"tree": {"sha": "b" * 40}})
        if request.method == "GET" and request.url.path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and "/git/ref/heads/" in request.url.path:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.url.path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "c" * 40})
        if request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "d" * 40})
        if request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "e" * 40})
        if request.url.path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/patchwork/repair-123"})
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            body = json.loads(request.content)
            assert body["draft"] is True and body["base"] == "main"
            assert "Validation results" in body["body"] and "An incorrect default" in body["body"]
            return httpx.Response(201, json={"id": 789, "number": 12, "html_url": "https://github.com/acme/project/pull/12", "head": {"sha": "e" * 40}})
        raise AssertionError(f"Unexpected GitHub API request: {request.url}")

    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('repaired')\n")
    client = GitHubRepositoryClient("write-scoped-token", transport=httpx.MockTransport(handler))
    result = client.create_repair_pull_request(
        full_name="acme/project", base_sha="a" * 40, base_branch="main",
        repair_id="12345678-1234-1234-1234-123456789abc",
        changed_paths=["src/app.py"], worktree=tmp_path, failure_summary="tests failed",
        root_cause="An incorrect default caused the failure.",
        explanation="Updated the default and retained the test coverage.",
        validation_checks=[{"name": "Pytest", "outcome": "PASSED"}],
        validation_environment={"runtime": "docker", "network": "disabled"}, retry_count=0,
    )
    assert result["number"] == 12 and result["branch"].startswith("patchwork/repair-")
    assert result["commit_sha"] == "e" * 40
    assert all(auth == "Bearer write-scoped-token" for _, _, auth in seen)


def test_pull_request_writer_resumes_existing_repair_branch_without_force_push(tmp_path) -> None:
    repair_head = "f" * 40
    seen_parents = []
    seen_ref_update = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/git/commits/" + "a" * 40):
            return httpx.Response(200, json={"tree": {"sha": "b" * 40}})
        if request.method == "GET" and "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"type": "commit", "sha": repair_head}})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "c" * 40})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "d" * 40})
        if path.endswith("/git/commits"):
            seen_parents.extend(json.loads(request.content)["parents"])
            return httpx.Response(201, json={"sha": "e" * 40})
        if request.method == "PATCH" and "/git/refs/heads/" in path:
            seen_ref_update.append(json.loads(request.content))
            return httpx.Response(200, json={"ref": path.rsplit("/", 1)[-1]})
        if request.method == "POST" and path.endswith("/pulls"):
            return httpx.Response(201, json={
                "id": 789, "number": 12,
                "html_url": "https://github.com/acme/project/pull/12",
                "head": {"sha": "e" * 40},
            })
        raise AssertionError(f"Unexpected GitHub API request: {request.method} {path}")

    source = tmp_path / "app.py"
    source.write_text("print('repaired')\n")
    client = GitHubRepositoryClient("write-scoped-token", transport=httpx.MockTransport(handler))
    result = client.create_repair_pull_request(
        full_name="acme/project", base_sha="a" * 40, base_branch="main",
        repair_id="12345678-1234-1234-1234-123456789abc",
        changed_paths=["app.py"], worktree=tmp_path, failure_summary="tests failed",
        root_cause="A bad value caused the failure.", explanation="Corrected the value.",
        validation_checks=[{"name": "Pytest", "outcome": "PASSED"}],
        validation_environment={"runtime": "docker"}, retry_count=0,
    )

    assert result["number"] == 12
    assert seen_parents == [repair_head]
    assert seen_ref_update == [{"sha": "e" * 40, "force": False}]
