"""KERNEL browser-research adapter (REST API, official documented endpoints).

Purpose: targeted, read-only investigation of a specific product page when
Channel3 evidence is insufficient. Security properties, all enforced here:

* Public research only. The only JavaScript this adapter ever executes is
  navigation plus read-only document inspection (title, URL, visible text).
  There is no fill/click/type/key-press code path at all, so carts, logins,
  OTPs and checkout are unreachable by construction.
* Page content is data, never instructions. Pages are returned as text
  snippets; nothing on a page can change Smartcart behaviour (the interpreter
  lives in the deterministic shopping layer and does not execute page text).
* URL validation before any navigation: https/http only, public DNS-style
  hosts, no userinfo, no loopback/private/link-local targets, no redirects
  into non-public addresses (re-checked at navigation time via final URL).
* Bounded: fixed JS navigation timeout, fixed text clip length, one page per
  ``visit`` call; the caller deletes the session when finished.

Live-verified contract (Milestone 1 audit, rotated key):
* ``GET  /auth/context``                     -> 200
* ``POST /browsers``                         -> 200 {"session_id": "...", ...}
* ``POST /browsers/{id}/playwright/execute`` -> 200 {"success": true, "result": ...}
* ``DELETE /browsers/{id}``                  -> 204

Session ids, CDP urls and API keys are never logged or stored past the call.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from app.ports import (
    KernelAuthError,
    KernelBrowserSession,
    KernelError,
    KernelMalformed,
    KernelPage,
    KernelRateLimited,
    KernelTimeout,
    KernelUnavailable,
    KernelUnsafeUrl,
)
from app.security.redaction import redact_text

# Hostnames/schemes that are never legitimate research targets.
_LOCAL_HOST = re.compile(
    r"^(localhost|127\..+|0\.0\.0\.0|10\..+|192\.168\..+|172\.(1[6-9]|2\d|3[01])\..+|169\.254\..+|::1|\[::1\])$"
)

# The adapter emits ONLY these playwright verbs. Tests assert this.
_FORBIDDEN_JS_ACTIONS = (
    "fill(",
    "click(",
    "check(",
    "uncheck(",
    "press(",
    "type(",
    "select_option(",
    "set_input",
    "keyboard.",
    "mouse.",
    "focus(",
)


def validate_public_url(url: str) -> str:
    """Return the normalised URL to visit, or raise ``KernelUnsafeUrl``.
    """
    if not isinstance(url, str) or not url.strip():
        raise KernelUnsafeUrl("URL is empty")
    candidate = url.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in ("http", "https"):
        raise KernelUnsafeUrl("URL scheme must be http or https")
    if parsed.username or parsed.password:
        raise KernelUnsafeUrl("URL must not contain credentials (userinfo)")
    host = parsed.hostname or ""
    if not host:
        raise KernelUnsafeUrl("URL has no host")
    host = host.strip().lower()
    if _LOCAL_HOST.match(host):
        raise KernelUnsafeUrl("URL points at a local or private network host")
    if "." not in host:
        raise KernelUnsafeUrl("URL host is not a public DNS name")
    if any(char in host for char in (" ", "\t", "\\", "|", "\"", "'")):
        raise KernelUnsafeUrl("URL host contains invalid characters")
    return candidate


def assert_research_safe_js(code: str) -> str:
    """Guard: the adapter refuses to emit action-capable playwright code."""
    lowered = code.lower()
    for action in _FORBIDDEN_JS_ACTIONS:
        if action in lowered:
            raise KernelError("internal error: non-read playwright action was constructed")
    return code


def _is_public_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    return not _LOCAL_HOST.match(host) and "." in host


def _jstr(value: str) -> str:
    """Escape a Python string for safe embedding in the JS snippet."""
    return json.dumps(value)


class KernelBrowserClient:
    """REST research client for the KERNEL public cloud browsers API."""

    name = "kernel"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float = 90.0,
        js_timeout_ms: int = 20000,
        max_page_text_length: int = 20000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise KernelError("KERNEL API key is not configured")
        self._api_key = api_key
        self._js_timeout_ms = js_timeout_ms
        self._max_text = max_page_text_length
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ HTTP

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.status_code in (401, 403):
            raise KernelAuthError(f"KERNEL rejected the API key (HTTP {response.status_code})")
        if response.status_code == 429:
            raise KernelRateLimited("KERNEL rate limit reached (HTTP 429)")
        if response.status_code >= 500:
            raise KernelUnavailable(f"KERNEL is unavailable (HTTP {response.status_code})")
        if response.status_code >= 400:
            detail = redact_text(response.text[:200])
            raise KernelError(f"KERNEL {action} failed (HTTP {response.status_code}): {detail}")

    # --------------------------------------------------------------- session

    def create_session(self) -> KernelBrowserSession:
        """Create one isolated headless browser. No navigation happens here."""
        try:
            response = self._client.post(
                "/browsers",
                json={"headless": True, "timeout_seconds": 600},
            )
        except httpx.TimeoutException as exc:
            raise KernelTimeout("KERNEL browser creation timed out") from exc
        except httpx.HTTPError as exc:
            raise KernelUnavailable("KERNEL transport error while creating a browser") from exc

        self._raise_for_status(response, "browser creation")
        try:
            payload = response.json()
        except ValueError as exc:
            raise KernelMalformed("KERNEL returned non-JSON on browser creation") from exc
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not session_id or not isinstance(session_id, str):
            raise KernelMalformed("KERNEL browser creation returned no session_id")
        return KernelBrowserSession(session_id=session_id)

    def delete_session(self, session_id: str) -> None:
        """Delete a browser session. Deleting an already-gone session is ok."""
        try:
            response = self._client.delete(f"/browsers/{session_id}")
        except httpx.TimeoutException as exc:
            raise KernelTimeout("KERNEL session deletion timed out") from exc
        except httpx.HTTPError as exc:
            raise KernelUnavailable("KERNEL transport error while deleting a session") from exc
        if response.status_code == 204:
            return
        if response.status_code == 404:
            return  # already terminated; idempotent cleanup
        self._raise_for_status(response, "session deletion")

    # ------------------------------------------------------------- research

    def _execute(self, session_id: str, code: str) -> dict:
        try:
            response = self._client.post(
                f"/browsers/{session_id}/playwright/execute",
                json={"code": code},
            )
        except httpx.TimeoutException as exc:
            raise KernelTimeout("KERNEL page operation timed out") from exc
        except httpx.HTTPError as exc:
            raise KernelUnavailable("KERNEL transport error during page operation") from exc
        self._raise_for_status(response, "playwright execute")
        try:
            payload = response.json()
        except ValueError as exc:
            raise KernelMalformed("KERNEL returned non-JSON for playwright execute") from exc
        if not isinstance(payload, dict):
            raise KernelMalformed("KERNEL playwright response was not an object")
        if payload.get("success") is False or "error" in payload:
            detail = redact_text(str(payload.get("error") or payload.get("stderr") or "")[:300])
            raise KernelMalformed(f"KERNEL page operation failed: {detail}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise KernelMalformed("KERNEL playwright response had no result object")
        return result

    def visit(self, session_id: str, url: str) -> KernelPage:
        """Navigate (read-only) to one validated public URL and read the page.

        The JS always returns what it managed to observe even when parts fail,
        so a partially-rendered or blocked page still yields provable
        limitations instead of fabricated data.
        """
        validated = validate_public_url(url)
        code = (
            f"let out = {{}};\n"
            f"try {{\n"
            f"  await page.goto({_jstr(validated)}, {{ waitUntil: 'domcontentloaded', "
            f"timeout: {self._js_timeout_ms} }});\n"
            f"}} catch (e) {{ out.goto_error = String(e).slice(0, 300); }}\n"
            f"try {{ out.url = page.url(); }} catch (e) {{}}\n"
            f"try {{ out.title = await page.title(); }} catch (e) {{ out.title_error = String(e).slice(0, 200); }}\n"
            f"try {{\n"
            f"  out.text = await page.evaluate(() => document.body ? document.body.innerText : '');\n"
            f"  out.text = String(out.text).slice(0, {self._max_text});\n"
            f"}} catch (e) {{ out.text_error = String(e).slice(0, 200); }}\n"
            f"return out;\n"
        )
        result = self._execute(session_id, assert_research_safe_js(code))

        goto_error = redact_text(str(result.get("goto_error") or ""))
        final_url = result.get("url") or validated
        title = redact_text(str(result.get("title") or ""))
        # Redirect target must also be public; a private redirect target is a
        # limitation, and the page content is discarded.
        redirected = bool(goto_error == "" and _different_host(validated, str(final_url)))
        if redirected and not _is_public_host(_host_of(str(final_url))):
            raise KernelUnsafeUrl(
                f"KERNEL redirected {validated} to a non-public host; page discarded"
            )
        text = str(result.get("text") or "")
        return KernelPage(
            url=validated,
            final_url=str(final_url),
            title=title,
            text=text[: self._max_text],
            redirected=redirected,
        )


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _different_host(a: str, b: str) -> bool:
    return _host_of(a) != _host_of(b)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
