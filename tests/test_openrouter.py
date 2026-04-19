from __future__ import annotations

import httpx
import pytest
import respx

from email_intel.integrations.openrouter import (
    OPENROUTER_URL,
    OpenRouterClient,
    OpenRouterError,
    complete_with_fallback,
)


def _ok(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


@respx.mock
def test_complete_json_parses_content():
    respx.post(OPENROUTER_URL).mock(return_value=_ok('{"summary": "ok", "importance": "normal"}'))

    with OpenRouterClient("sk-test") as client:
        data = client.complete_json("model-x", "sys", "user")

    assert data == {"summary": "ok", "importance": "normal"}


@respx.mock
def test_complete_json_raises_on_non_json():
    respx.post(OPENROUTER_URL).mock(return_value=_ok("not json"))

    with OpenRouterClient("sk-test") as client, pytest.raises(OpenRouterError):
        client.complete_json("model-x", "sys", "user")


@respx.mock
def test_fallback_used_when_primary_fails():
    route = respx.post(OPENROUTER_URL)
    route.side_effect = [
        httpx.Response(500, text="boom"),
        httpx.Response(500, text="boom"),
        httpx.Response(500, text="boom"),  # exhausts primary retries (3)
        _ok('{"summary": "fallback worked", "importance": "normal"}'),
    ]

    with OpenRouterClient("sk-test") as client:
        data = complete_with_fallback(
            client=client,
            primary_model="primary",
            fallback_model="fallback",
            system="sys",
            user="u",
        )

    assert data["summary"] == "fallback worked"
