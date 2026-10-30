from dataclasses import dataclass
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.settings import settings


class InferenceError(Exception):
    pass


@dataclass(frozen=True)
class InferenceResult:
    content: str
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    provider: str = "nebius-token-factory"


class NebiusClient:
    """Small OpenAI-compatible Token Factory client with bounded retries."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model_id: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url if base_url is not None else settings.nebius_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.nebius_api_key
        self.model_id = model_id if model_id is not None else settings.nebius_model
        self.transport = transport
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InferenceError("NEBIUS_BASE_URL must be a configured HTTPS API base URL")
        if not self.api_key or not self.model_id:
            raise InferenceError("Nebius API key and model identifier are not configured")

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
        endpoint = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            # Keep the model's private reasoning within a bounded budget so
            # structured agent output is returned in message.content.
            "reasoning_effort": "low",
        }
        timeout = httpx.Timeout(60.0, connect=5.0, write=15.0, pool=5.0)
        last_error = "Nebius inference request failed"
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=self.transport) as client:
            for attempt in range(3):
                try:
                    response = client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                except httpx.TimeoutException as exc:
                    last_error = "Nebius inference request timed out"
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    raise InferenceError(last_error) from exc
                except httpx.RequestError as exc:
                    raise InferenceError("Nebius inference endpoint could not be reached") from exc

                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        delay = self._retry_delay(response, attempt)
                        time.sleep(delay)
                        continue
                    last_error = "Nebius inference is temporarily unavailable"
                    break
                if response.status_code in {401, 403}:
                    raise InferenceError("Nebius rejected the configured API key or model access")
                if response.is_error:
                    raise InferenceError(f"Nebius rejected the request (HTTP {response.status_code})")
                return self._parse_response(response)
        raise InferenceError(last_error)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after", "")
        try:
            return min(3.0, max(0.0, float(retry_after)))
        except ValueError:
            return 0.5 * (attempt + 1)

    def _parse_response(self, response: httpx.Response) -> InferenceResult:
        try:
            data: dict[str, Any] = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data.get("usage") or {}
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty response content")
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
                raise ValueError("missing token usage")
            return InferenceResult(
                content=content,
                model_id=str(data.get("model") or self.model_id),
                prompt_tokens=max(0, prompt_tokens),
                completion_tokens=max(0, completion_tokens),
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InferenceError("Nebius returned an invalid chat completion response") from exc
