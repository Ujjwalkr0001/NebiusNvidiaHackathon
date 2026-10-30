import json

import httpx
import pytest

from app.agents.agents import _validate_diff_shape, run_coder, run_repo_architect, validate_diff_policy, validate_repository_patch_paths
from app.agents.millisa import MillisaClient
from app.agents.nebius import InferenceError, InferenceResult, NebiusClient
from app.agents.schemas import RepoArchitectOutput


def response(content: str) -> dict:
    return {
        "model": "nvidia/nemotron-test",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 25, "completion_tokens": 14},
    }


def test_nebius_client_uses_configured_model_and_records_actual_usage() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=response('{"ok":true}'))

    client = NebiusClient(
        base_url="https://tokenfactory.example/v1",
        api_key="server-secret",
        model_id="nvidia/nemotron-configured",
        transport=httpx.MockTransport(handler),
    )
    result = client.complete(system_prompt="system", user_prompt="task")
    assert seen["url"] == "https://tokenfactory.example/v1/chat/completions"
    assert seen["auth"] == "Bearer server-secret"
    assert seen["body"]["model"] == "nvidia/nemotron-configured"
    assert result.prompt_tokens == 25 and result.completion_tokens == 14


def test_nebius_client_retries_rate_limit_with_a_small_bound() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=response('{"ok":true}'))

    client = NebiusClient(
        base_url="https://tokenfactory.example/v1", api_key="key", model_id="model",
        transport=httpx.MockTransport(handler),
    )
    client.complete(system_prompt="system", user_prompt="task")
    assert calls == 2


def test_nebius_rejects_missing_usage_and_unsafe_base_urls() -> None:
    client = NebiusClient(
        base_url="https://tokenfactory.example/v1", api_key="key", model_id="model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})),
    )
    with pytest.raises(InferenceError):
        client.complete(system_prompt="system", user_prompt="task")
    with pytest.raises(InferenceError):
        NebiusClient(base_url="http://localhost:8000/v1", api_key="key", model_id="model")


def test_millisa_client_uses_documented_openai_gateway_contract() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "millisa-gemini-flash",
            "choices": [{"message": {"content": '{"root_cause":"bad value"}'}}],
            "usage": {"prompt_tokens": 18, "completion_tokens": 7, "total_tokens": 25},
        })

    client = MillisaClient(
        base_url="https://millisa.codly.in/api/v1",
        api_key="test-key",
        model_id="millisa-auto",
        transport=httpx.MockTransport(handler),
    )
    result = client.complete(system_prompt="system", user_prompt="task", max_tokens=1500)
    assert seen["url"] == "https://millisa.codly.in/api/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["model"] == "millisa-auto"
    assert seen["body"]["stream"] is False
    assert result.provider == "millisa-gateway"
    assert result.prompt_tokens == 18 and result.completion_tokens == 7


def test_millisa_catalog_and_quota_use_bearer_auth() -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        paths.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "millisa-auto"}]})
        return httpx.Response(200, json={"rpm_limit": 60, "tokens_remaining": 9000})

    client = MillisaClient(
        base_url="https://millisa.codly.in/api/v1",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    assert client.list_models() == [{"id": "millisa-auto"}]
    assert client.get_usage()["tokens_remaining"] == 9000
    assert paths == ["/api/v1/models", "/api/v1/usage"]


def test_millisa_reports_insufficient_wallet_without_leaking_response() -> None:
    client = MillisaClient(
        base_url="https://millisa.codly.in/api/v1", api_key="test-key", model_id="millisa-auto",
        transport=httpx.MockTransport(lambda request: httpx.Response(402, json={"error": {"message": "private billing detail"}})),
    )
    with pytest.raises(InferenceError, match="insufficient wallet balance") as error:
        client.complete(system_prompt="system", user_prompt="task")
    assert "private billing detail" not in str(error.value)


def test_agent_schemas_reject_malformed_model_output() -> None:
    class FakeClient:
        def complete(self, **kwargs):
            return InferenceResult(content="not json", model_id="model", prompt_tokens=1, completion_tokens=1)

    with pytest.raises(ValueError, match="required output schema"):
        run_repo_architect(FakeClient(), {"failure": "test failed"})


def test_coder_accepts_only_repo_relative_unified_diff() -> None:
    architect = RepoArchitectOutput(
        failure_summary="A test failed",
        root_cause_hypotheses=["bad default"],
        supporting_evidence=["assertion output"],
        relevant_files=["src/app.py"],
        relevant_symbols=["load_config"],
        recommended_patch_scope=["src/app.py", "tests/test_app.py"],
        tests_to_run=["pytest"],
        confidence=0.8,
        risk_assessment="Low",
    )
    good_diff = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new"

    class FakeClient:
        def complete(self, **kwargs):
            return InferenceResult(
                content=json.dumps({
                    "root_cause": "bad default", "explanation": "Use the configured default.",
                    "unified_diff": good_diff, "regression_test_plan": ["add case"],
                    "tests_to_run": ["pytest"], "risk_notes": [],
                }),
                model_id="model", prompt_tokens=10, completion_tokens=20,
            )

    result, _ = run_coder(FakeClient(), failure_context={"summary": "test failed"}, architect=architect, source_files={"src/app.py": "old"})
    assert result.unified_diff == good_diff
    with pytest.raises(ValueError, match="unsafe file path"):
        _validate_diff_shape("diff --git a/../secret b/../secret\n--- a/../secret\n+++ b/../secret\n")


def test_patch_policy_rejects_protected_or_out_of_scope_files() -> None:
    protected = "diff --git a/.env.production b/.env.production\n--- a/.env.production\n+++ b/.env.production\n@@ -1 +1 @@\n-secret\n+value"
    with pytest.raises(ValueError, match="protected"):
        validate_diff_policy(protected, allowed_paths=[".env.production"], existing_paths=[".env.production"])

    outside = "diff --git a/src/other.py b/src/other.py\n--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-old\n+new"
    with pytest.raises(ValueError, match="outside"):
        validate_diff_policy(outside, allowed_paths=["src/app.py"], existing_paths=["src/app.py"])


def test_repository_patch_scope_is_exact_and_only_allows_safe_python_paths() -> None:
    assert validate_repository_patch_paths(["src/app.py", "tests/test_app.py"]) == ["src/app.py", "tests/test_app.py"]
    for unsafe in (
        "../app.py", "/app.py", "src\\app.py", "src/*.py", "src/app.txt",
        ".github/workflows/ci.py", "src/.env.local.py", "src/key.pem.py", "src//app.py",
    ):
        with pytest.raises(ValueError):
            validate_repository_patch_paths([unsafe])
    with pytest.raises(ValueError, match="duplicate"):
        validate_repository_patch_paths(["src/app.py", "src/app.py"])


def test_repository_patch_scope_also_constrains_regression_test_changes() -> None:
    test_diff = "diff --git a/tests/test_extra.py b/tests/test_extra.py\n--- a/tests/test_extra.py\n+++ b/tests/test_extra.py\n@@ -1 +1 @@\n-old\n+new"
    with pytest.raises(ValueError, match="repository patch allowlist"):
        validate_diff_policy(
            test_diff, allowed_paths=None, existing_paths=None,
            repository_allowed_paths=["src/app.py"],
        )
