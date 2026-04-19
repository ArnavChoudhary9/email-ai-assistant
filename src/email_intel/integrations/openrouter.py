from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(self, api_key: str, timeout: float = 60.0) -> None:
        self._api_key = api_key
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/local/email-intel",
                "X-Title": "email-intel",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenRouterClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type((httpx.HTTPError, OpenRouterError)),
    )
    def complete_json(self, model: str, system: str, user: str) -> dict[str, Any]:
        """Call the chat API and return a parsed JSON object.

        Raises OpenRouterError on non-2xx or unparseable content.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }
        log.debug("OpenRouter request: model=%s user_len=%d", model, len(user))
        resp = self._client.post(OPENROUTER_URL, json=payload)
        if resp.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise OpenRouterError(f"Malformed response: {data!r}") from e
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            raise OpenRouterError(f"Non-JSON content: {content[:500]}") from e
        if not isinstance(parsed, dict):
            raise OpenRouterError(f"Expected JSON object, got {type(parsed).__name__}")
        return parsed


def complete_with_fallback(
    client: OpenRouterClient,
    primary_model: str,
    fallback_model: str | None,
    system: str,
    user: str,
) -> dict[str, Any]:
    try:
        return client.complete_json(primary_model, system, user)
    except Exception as e:
        if not fallback_model or fallback_model == primary_model:
            raise
        log.warning("Primary model %s failed (%s); trying fallback %s", primary_model, e, fallback_model)
        return client.complete_json(fallback_model, system, user)
