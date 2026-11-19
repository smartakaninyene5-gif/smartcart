"""Gemini adapter contract tests over httpx.MockTransport (no live key).

Verifies request construction, structured parsing and the full error taxonomy:
auth, rate limit, model unavailable, server failure, timeout, transport error
and malformed/blocked output. Synthetic credentials only.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.gemini import GeminiLlmClient
from app.ports import (
    LlmAuthError,
    LlmError,
    LlmMalformed,
    LlmModelUnavailable,
    LlmNotConfigured,
    LlmRateLimited,
    LlmTimeout,
    LlmUnavailable,
)

KEY = "synthetic-gemini-key"  # never a real credential
MODEL = "gemini-test-model"
PROMPT = "understand this request"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def make_client(handler) -> GeminiLlmClient:
    return GeminiLlmClient(
        KEY,
        model=MODEL,
        base_url="https://gemini.test",
        transport=httpx.MockTransport(handler),
    )


def gemini_ok(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}]}}
            ]
        },
    )


def test_request_construction_uses_configured_model_and_schema():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["api_key"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content)
        return gemini_ok({"answer": "ok"})

    result = make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)

    assert result == {"answer": "ok"}
    assert captured["method"] == "POST"
    assert captured["path"] == f"/v1beta/models/{MODEL}:generateContent"
    assert captured["api_key"] == KEY
    body = captured["body"]
    assert body["contents"][0]["role"] == "user"
    assert body["contents"][0]["parts"][0]["text"] == PROMPT
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseSchema"] == SCHEMA


def test_empty_key_is_not_configured():
    with pytest.raises(LlmNotConfigured):
        GeminiLlmClient("   ", model=MODEL, base_url="https://gemini.test")


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_maps_to_auth_error(status):
    def handler(_request):
        return httpx.Response(status, json={"error": {"message": "bad key"}})

    with pytest.raises(LlmAuthError):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_rate_limit_maps_to_rate_limited():
    def handler(_request):
        return httpx.Response(429, json={})

    with pytest.raises(LlmRateLimited):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_missing_model_maps_to_model_unavailable():
    def handler(_request):
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    with pytest.raises(LlmModelUnavailable):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_server_error_maps_to_unavailable():
    def handler(_request):
        return httpx.Response(503, json={})

    with pytest.raises(LlmUnavailable):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_other_client_error_maps_to_generic_llm_error():
    def handler(_request):
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(LlmError) as excinfo:
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)
    assert not isinstance(excinfo.value, (LlmAuthError, LlmMalformed))


def test_timeout_maps_to_timeout():
    def handler(_request):
        raise httpx.TimeoutException("slow")

    with pytest.raises(LlmTimeout):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_transport_error_maps_to_unavailable():
    def handler(_request):
        raise httpx.ConnectError("offline")

    with pytest.raises(LlmUnavailable):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_non_json_body_is_malformed():
    def handler(_request):
        return httpx.Response(200, text="<html>oops</html>")

    with pytest.raises(LlmMalformed):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_non_json_candidate_text_is_malformed():
    def handler(_request):
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "not json at all"}]}}]},
        )

    with pytest.raises(LlmMalformed):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_empty_candidate_text_is_malformed():
    def handler(_request):
        return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})

    with pytest.raises(LlmMalformed):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_safety_block_is_malformed():
    def handler(_request):
        return httpx.Response(
            200,
            json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []},
        )

    with pytest.raises(LlmMalformed):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_non_object_json_payload_is_malformed():
    def handler(_request):
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "[1, 2]"}]}}]},
        )

    with pytest.raises(LlmMalformed):
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)


def test_api_key_never_appears_in_error_messages():
    def handler(_request):
        return httpx.Response(401, json={"error": {"message": f"invalid {KEY}"}})

    with pytest.raises(LlmAuthError) as excinfo:
        make_client(handler).structured(prompt=PROMPT, schema=SCHEMA)
    assert KEY not in str(excinfo.value)
