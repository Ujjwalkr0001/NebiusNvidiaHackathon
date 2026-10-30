"""Millisa OpenAI-compatible gateway client used for Coder agent calls."""

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.agents.nebius import InferenceError, InferenceResult, NebiusClient
from app.settings import settings


class MillisaClient(NebiusClient):
    """Small, bounded client for Millisa's OpenAI-compatible API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.millisa_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.millisa_api_key
        self.model_id = model_id if model_id is not None else settings.millisa_model
        self.transport = transport
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InferenceError("MILLISA_BASE_URL must be an HTTPS API base URL")
        if not self.api_key or not self.model_id:
            raise InferenceError("Millisa API key and model identifier are not configured")

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4000,
    ) -> InferenceResult:
        if len(system_prompt) + len(user_prompt) > 160_000:
            raise InferenceError("Agent context exceeds the configured input limit")
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        timeout = httpx.Timeout(60.0, connect=5.0, write=15.0, pool=5.0)
        last_error = "Millisa inference request failed"
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=self.transport) as client:
            for attempt in range(3):
                try:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        json=payload,
                    )
                except httpx.TimeoutException as exc:
                    last_error = "Millisa inference request timed out"
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise InferenceError(last_error) from exc
                except httpx.RequestError as exc:
                    raise InferenceError("Millisa inference endpoint could not be reached") from exc

                if response.status_code in {429, 500, 502, 503, 504}:
                    if attempt < 2:
                        time.sleep(self._retry_delay(response, attempt))
                        continue
                    last_error = "Millisa is temporarily unavailable or its request quota was reached"
                    break
                if response.status_code in {401, 403}:
                    raise InferenceError("Millisa rejected the configured API key")
                if response.status_code == 402:
                    raise InferenceError("Millisa reports insufficient wallet balance")
                if response.is_error:
                    raise InferenceError(f"Millisa rejected the completion request (HTTP {response.status_code})")
                return self._parse_completion(response)
        raise InferenceError(last_error)

    def list_models(self) -> list[dict[str, Any]]:
        data = self._get_json("/models")
        models = data.get("data")
        if not isinstance(models, list) or any(not isinstance(row, dict) for row in models):
            raise InferenceError("Millisa returned an invalid model catalog")
        return models

    def get_usage(self) -> dict[str, Any]:
        return self._get_json("/usage")

    def _get_json(self, path: str) -> dict[str, Any]:
        timeout = httpx.Timeout(20.0, connect=5.0, write=10.0, pool=5.0)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, transport=self.transport) as client:
                response = client.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                )
        except httpx.RequestError as exc:
            raise InferenceError("Millisa API could not be reached") from exc
        if response.status_code in {401, 403}:
            raise InferenceError("Millisa rejected the configured API key")
        if response.status_code == 402:
            raise InferenceError("Millisa reports insufficient wallet balance")
        if response.status_code == 429:
            raise InferenceError("Millisa request quota was reached")
        if response.is_error:
            raise InferenceError(f"Millisa API request failed (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise InferenceError("Millisa returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise InferenceError("Millisa returned an invalid response")
        return payload

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        try:
            return min(3.0, max(0.0, float(response.headers.get("retry-after", ""))))
        except ValueError:
            return 0.5 * (attempt + 1)

    def _parse_completion(self, response: httpx.Response) -> InferenceResult:
        try:
            data: dict[str, Any] = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty response content")
            if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
                raise ValueError("missing token usage")
            return InferenceResult(
                content=content,
                model_id=str(data.get("model") or self.model_id),
                prompt_tokens=max(0, prompt_tokens),
                completion_tokens=max(0, completion_tokens),
                provider="millisa-gateway",
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InferenceError("Millisa returned an invalid chat completion response") from exc
