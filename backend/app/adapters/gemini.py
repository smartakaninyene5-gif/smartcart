"""Gemini adapter — request understanding and evidence interpretation only.

Structured output is requested with a JSON schema and validated by the caller.
The client has no access to Guard, payment or persistence objects and can
never approve a purchase; its output is advisory evidence, tagged AI_*.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

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


class GeminiLlmClient:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise LlmNotConfigured("Gemini API key is not configured")
        self._api_key = api_key
        self._model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        )

    def structured(self, *, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
                "temperature": 0.2,
            },
        }
        try:
            response = self._client.post(f"/v1beta/models/{self._model}:generateContent", json=body)
        except httpx.TimeoutException as exc:
            raise LlmTimeout("Gemini request timed out") from exc
        except httpx.HTTPError as exc:
            raise LlmUnavailable("Gemini transport error") from exc

        if response.status_code in (401, 403):
            raise LlmAuthError("Gemini rejected the API key (HTTP %s)" % response.status_code)
        if response.status_code == 429:
            raise LlmRateLimited("Gemini rate limit reached (HTTP 429)")
        if response.status_code == 404:
            raise LlmModelUnavailable(
                "Gemini model is not available for this API key (HTTP 404); "
                "check GEMINI_MODEL against the key's model access"
            )
        if response.status_code >= 500:
            raise LlmUnavailable(f"Gemini is unavailable (HTTP {response.status_code})")
        if response.status_code >= 400:
            raise LlmError(f"Gemini request failed (HTTP {response.status_code})")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmMalformed("Gemini returned a non-JSON body") from exc

        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        if payload.get("promptFeedback", {}).get("blockReason"):
            raise LlmMalformed("Gemini blocked the prompt (safety filter)")
        try:
            parts = candidates[0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmMalformed("Gemini response had no candidate content") from exc
        if not text.strip():
            raise LlmMalformed("Gemini returned an empty structured response")
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise LlmMalformed("Gemini returned an unparseable structured response") from exc
        if not isinstance(parsed, dict):
            raise LlmMalformed("Gemini structured response was not an object")
        return parsed
