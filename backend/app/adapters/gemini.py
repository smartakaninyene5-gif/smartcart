"""Gemini adapter — advisory only.

Live verification is BLOCKED (no API key, no accepted data policy). The client
returns plain validated-by-caller dicts; it has no access to Guard, payment or
persistence objects. Structured output is requested with a JSON schema.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.ports import LlmError, LlmNotConfigured


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
            raise LlmError("Gemini request timed out") from exc
        except httpx.HTTPError as exc:
            raise LlmError("Gemini transport error") from exc

        if response.status_code == 429:
            raise LlmError("Gemini rate limit reached")
        if response.status_code in (401, 403):
            raise LlmError("Gemini rejected the API key")
        if response.status_code >= 400:
            raise LlmError(f"Gemini request failed (HTTP {response.status_code})")

        try:
            payload = response.json()
            candidates = payload.get("candidates") or []
            parts = candidates[0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
            parsed = json.loads(text)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmError("Gemini returned an unparseable structured response") from exc
        if not isinstance(parsed, dict):
            raise LlmError("Gemini structured response was not an object")
        return parsed
