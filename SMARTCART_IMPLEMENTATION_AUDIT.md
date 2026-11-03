# SMARTCART — Full Implementation & Integration Audit

**Audit date:** 31 October 2026  
**Project root:** `C:\Users\Admin\Desktop\smartcart`  
**Method:** static inspection of every source file, plus actual execution of the application, audit harnesses, mock-transport contract tests, and three read-only live MCP probes.  
**Constraints honoured:** no existing file was rewritten or deleted; no configuration was changed; no dependency was installed; no real payment, capture, purchase or destructive webhook was performed; no credential or secret value appears in this report.

> This is an audit, not a rebuild. Nothing in this document has been implemented.

---

## 0. How to read this report

### Status vocabulary

| Status | Meaning in this audit |
|---|---|
| **WORKING** | I executed the behaviour in this audit and it passed. |
| **PARTIALLY WORKING** | Some behaviour works, but a material part is missing, weak, or fails in a realistic path. |
| **IMPLEMENTED BUT UNVERIFIED** | Source code exists and was inspected; the live path could not be exercised (usually credentials/access). |
| **NOT IMPLEMENTED** | No actual implementation was found. |
| **BROKEN** | Code exists but fails when executed. |
| **BLOCKED EXTERNALLY** | The path requires a credential, account, public URL or service that does not exist in this environment. |

### Evidence vocabulary

- **Inspected** — read the file and traced the call path.
- **Executed** — actually ran it; results are in §10.
- **Not executed** — no credential/access, so not claimed.
- **Audit-only shim** — a temporary in-memory patch in `C:\Users\Admin\AppData\Local\Temp\opencode\audit_smoke.py`, used to get past two shipped bugs and exercise the rest of the flow. **It is not applied to project code.**

### Important limitation

`WORKING` means “works in the local SQLite/mock configuration I tested”, not “production-proven”. Live Channel3 REST, Gemini, PayPal and KERNEL were **not** verified.

---

## 1. Executive Summary

### The short version

Smartcart is a **credible local deterministic prototype**, not a working end-to-end purchasing agent.

The strongest part is the credential-free core: product normalisation, evidence storage, quote snapshots, a 17-rule Purchase Guard, approval binding, hash-chained journal, mock PayPal flow, and honest separation between “paid Smartcart” and “bought from a retailer”. That core was exercised extensively and is substantially real.

The weakest parts are everything that turns a prototype into a product:

- The **shipped approval → revalidation → payment path crashes on SQLite** because stored timestamps are naive and the application compares them with aware UTC.
- **Price-change invalidation crashes** because `Decimal` values are written into a JSON journal column.
- **There are no project tests** (`pytest` collects zero tests) despite the milestone document citing test files.
- **There are no migrations** (`alembic.ini` and `migrations/` absent) despite a `create_all`-only schema.
- **The frontend is an empty directory**, so the advertised demo UI does not exist.
- **No MCP server is configured in OpenCode**, and **no MCP client exists in the backend**. Channel3 MCP is reachable only as an external research channel.
- **No live external integration is verified**: Channel3 REST is blocked by a missing key, PayPal and Gemini have no credentials, KERNEL returns 401.

### What was actually executed

| Check | Result |
|---|---|
| `python -m compileall -q app` | passed, exit 0 (45 Python modules) |
| `pip check` | `No broken requirements found.` |
| `import app.main` | passed |
| `uvicorn app.main:app` + `GET /api/v1/health` | 200, `data_mode=MOCK`, `journal_chain_ok=true` |
| Audit end-to-end harness | **64/64 checks passed — only after two audit-only shims** |
| Audit unit/contract harness | **69/69 checks passed** (after one defect in the audit harness was corrected) |
| Project `pytest` | **0 tests collected, exit 1** |
| Live Channel3 MCP handshake/tools/search | 200; 6 tools; 8 real products |
| Live MCP payload → normaliser | **8/8 products parsed** |
| Live Channel3 REST unauthenticated | 403 (as expected) |
| Live KERNEL MCP | 401 `invalid_token` |
| Live PayPal / Gemini | not attempted — no credentials |

### Judgement

- **Deterministic, credential-free core:** largely implemented and working, with two serious bugs in the approval/persistence path.
- **Live integrations:** adapters exist, but **0 of 4 live backend integrations were verified** (Channel3 REST, Gemini, PayPal, KERNEL).
- **MCP as a Smartcart feature:** **0% implemented**. MCP is configured nowhere and is called by no backend code.
- **Platform readiness:** tests, migrations, auth, frontend, Postgres support and deployment are absent.

A defensible summary is: **a strong local prototype covering roughly half of the intended MVP surface, with none of the live external paths proven and several core paths broken as shipped.**

### The five things that matter most

1. Fix the naive/aware datetime bug before anything else. It blocks every revalidation and therefore every real payment.
2. Fix journal payload serialisation for `Decimal` values.
3. Fix `add_capture` so a duplicate insert does not roll back the whole request transaction.
4. Add a real test suite, then a migration path; both are currently zero.
5. Do not wire MCP into the runtime or enable live payments before auth, tests and persistence are fixed.

---

## 2. Project Structure and Implementation Status

### 2.1 Inventory

```
smartcart/
├─ SMARTCART_MILESTONE_0.md        research/feasibility document
├─ SMARTCART_MILESTONE_1.md        implementation claims
├─ .env.example                    36 lines; all secrets blank
├─ .gitignore
├─ .venv/                          installed virtual environment (not audited as source)
├─ backend/
│  ├─ pyproject.toml
│  ├─ _smoke.db                    leftover SQLite file
│  └─ app/                         45 Python files
│     ├─ main.py                   FastAPI entry point
│     ├─ config.py                 pydantic-settings configuration
│     ├─ container.py              composition root
│     ├─ api/                      deps, routes, schemas, webhooks
│     ├─ application/              discovery, quotes, approvals, payments, common, errors
│     ├─ domain/                   models, money, snapshot, enums, guard/
│     ├─ adapters/                 mock_source, channel3_rest, gemini, paypal, normalize, clock, fixtures/
│     ├─ persistence/              base, tables, repositories
│     ├─ journal/                  events, writer
│     ├─ ports/                    Protocol definitions and typed errors
│     └─ security/                 redaction, untrusted
└─ frontend/                       EMPTY (0 files)
```

### 2.2 What does not exist

| Expected artefact | Found? | Evidence |
|---|---|---|
| Git repository / history | No | `git status` → `fatal: not a git repository` |
| `README.md` | No | milestone 1 line 310 says “see `README.md`” |
| `backend/tests/` | No | `Test-Path tests` → `False`; `pytest` collects 0 |
| `backend/alembic.ini` | No | `Test-Path` → `False` |
| `backend/migrations/` | No | `Test-Path` → `False` |
| Frontend files | No | `frontend/` contains 0 entries |
| OpenCode MCP configuration | No | no `opencode.json` in project, `~/.config/opencode`, `%APPDATA%`, `%LOCALAPPDATA%` |
| `.env` | No | only `.env.example` |
| Backend MCP client dependency | No | no `mcp` package in `pyproject.toml`; zero `mcp` references in `app/` |

### 2.3 Dependencies

`backend/pyproject.toml` pins FastAPI 0.121.2, Uvicorn 0.41.0, Pydantic 2.12.4, pydantic-settings 2.13.0, SQLAlchemy 2.0.44, httpx 0.28.1 and Alembic 1.17.1; `pytest==9.0.1` is an optional dev dependency. The existing virtual environment imports all of them and `pip check` reports no broken requirements.

**Postgres is not usable as shipped.** No `psycopg`, `psycopg2` or `asyncpg` module is installed, and none is declared in `pyproject.toml`. A `postgresql://` URL would fail at engine creation with a missing-driver error. The project constraint says “SQLite/Postgres”; only SQLite is actually supported today.

`alembic` is installed and declared but **never used**: there is no `alembic.ini`, no `migrations/`, and no revision history. Schema creation is `Base.metadata.create_all()` in `backend/app/persistence/base.py:76-80`.

### 2.4 Component status matrix

| Component | Status | Evidence |
|---|---|---|
| Settings and secret handling | **WORKING** | `app/config.py`; empty strings become `None`; `SecretStr`; no literal secrets found |
| Composition root | **WORKING** | `app/container.py:42-85`; mock source and disabled AI/Gateway build cleanly |
| FastAPI startup and health | **WORKING** | uvicorn on port 8123/8124; `/api/v1/health` 200 |
| SQLite schema creation | **WORKING** | `init_db` creates tables and append-only triggers |
| Alembic migrations | **NOT IMPLEMENTED** | no config, no revisions |
| Postgres support | **NOT IMPLEMENTED** | no driver dependency |
| API request validation | **WORKING** | `extra="forbid"`; invalid requests return 422 |
| API user scoping | **PARTIALLY WORKING** | `X-User-Id` enforced in queries, but unauthenticated and spoofable |
| Domain models and money | **WORKING** | Decimal-only, 2-dp enforcement, arithmetic validation |
| Snapshot hashing | **WORKING** | canonical JSON, `sha256:` prefix, quote-id excluded |
| Change detection | **PARTIALLY WORKING** | works, but Decimal journaling is broken |
| Purchase Guard | **WORKING** | 17 pure rules; no override parameter |
| Product discovery (mock) | **WORKING** | deterministic fixture, warnings, evidence |
| Channel3 REST adapter | **IMPLEMENTED BUT UNVERIFIED** | contract-tested; live blocked (no key) |
| Channel3 MCP runtime adapter | **NOT IMPLEMENTED** | zero backend MCP references |
| KERNEL integration | **NOT IMPLEMENTED** | no code, no config; live 401 |
| Gemini adapter | **IMPLEMENTED BUT UNVERIFIED** | contract-tested; never called by application |
| PayPal adapter | **IMPLEMENTED BUT UNVERIFIED** | contract-tested; live blocked |
| Approval workflow | **PARTIALLY WORKING** | revalidation crashes; reject→re-approve gap |
| Payment workflow | **PARTIALLY WORKING** | mock/fake gateway passes; live blocked; SQLite bug blocks revalidation |
| Journal and hash chain | **WORKING** | chain verifies; UPDATE/DELETE triggers block |
| Evidence and provenance | **PARTIALLY WORKING** | evidence stored; digest never re-verified on read |
| Frontend | **NOT IMPLEMENTED** | empty directory; `/` returns 404 |
| Tests | **NOT IMPLEMENTED** | zero project tests |
| MCP integration | **NOT IMPLEMENTED** | see §3 |

### 2.5 Documentation vs reality

`SMARTCART_MILESTONE_1.md` overstates completion in several places:

| Claim | Reality |
|---|---|
| Line 314: “Data models + migrations … `backend/migrations/`” | models exist; **no migrations directory** |
| Line 315–318: cites `backend/tests/unit/test_snapshot.py`, `test_guard.py`, `test_approval_flow.py`, `test_journal.py` | **none of these files exist**; `pytest` collects 0 |
| Line 319: “API + demo UI … `frontend/`” | API exists; **frontend is empty** |
| Line 52: “Search → Offer comparison → Quote review …” screens | **no UI and no comparison feature** |
| Line 85: quotes are “insert-only” | `QuoteRecord` is mutable; `approve_quote` and `reject_quote` update quote status; **no append-only trigger on `quotes`** |
| Line 147: Gemini uses “the Interactions API” | code calls `POST /v1beta/models/{model}:generateContent` in `app/adapters/gemini.py:51` |
| Line 152: “Mock LLM provider is labelled MOCK” | `ai_provider` accepts only `none` or `gemini`; **no mock LLM provider exists** |
| Line 325: “does not claim tests that were not executed” | the cited tests do not exist, so nothing could be executed |

The milestone documents are useful design records, but they are not a reliable statement of implementation status.

---

## 3. MCP Integration Audit

### 3.1 The five levels, kept separate

| Level | Channel3 | KERNEL |
|---|---|---|
| 1. Configured in OpenCode | **NOT IMPLEMENTED** | **NOT IMPLEMENTED** |
| 2. Reachable / tools discoverable | **WORKING** | **BLOCKED EXTERNALLY** (401) |
| 3. Backend MCP client implemented | **NOT IMPLEMENTED** | **NOT IMPLEMENTED** |
| 4. Backend calling MCP tools | **NOT IMPLEMENTED** | **NOT IMPLEMENTED** |
| 5. Returned data integrated into Smartcart | **NOT IMPLEMENTED** | **NOT IMPLEMENTED** |

A success at level 2 is not evidence of level 3, 4 or 5. This report does not conflate them.

### 3.2 Level 1 — OpenCode configuration

- No `opencode.json` exists in the project, `C:\Users\Admin\.config\opencode`, `%APPDATA%\opencode`, `%LOCALAPPDATA%\opencode`, or `.opencode/`.
- `C:\Users\Admin\.config\opencode` contains only `service.json`; it declares no MCP servers.
- `tools.opencode.list_mcp_resources({})` returned `{"resources":[],"templates":[]}`.
- The available tool catalogue contains only OpenCode’s own `opencode` and `browser` namespaces; no `channel3` or `kernel` MCP tools are exposed to the agent.

**Conclusion:** MCP is effectively unconfigured in this environment. This matches Milestone 0’s own conclusion.

### 3.3 Level 2 — reachability and tool discovery

#### Channel3 MCP — live probe (executed)

Endpoint: `https://mcp.trychannel3.com/` (Streamable HTTP). This probe performed a single handshake, a single `tools/list`, and one `search_products` call. A second, separate audit probe repeated the handshake and search only to test normalisation.

| Probe | Result |
|---|---|
| `initialize` | HTTP 200, `text/event-stream`; server `Channel3 4.0.0`; protocol `2025-06-18` |
| `tools/list` | HTTP 200; **6 tools** |
| Tool names | `search_products`, `get_products`, `browse_products`, `get_similar`, `get_details`, `get_price_history` |
| `search_products` | HTTP 200; `isError` absent; keys `content`, `structuredContent` |
| Products | **8** real products |
| Response metadata | `as_of = 2026-10-31T08:51:51.687Z`; `thread_id = thr_...` |
| Product keys | `brands`, `id`, `images`, `offers`, `title` |
| Offer keys | `availability`, `condition`, `dimensions`, `domain`, `price`, `url` |
| Example | Sony WH-CH730N, `bestbuy.com`, price `{price: 128, compare_at_price: 178, currency: USD}` |

No credential was used and no mutation was requested. The server declares its tools read-only, and only read-only calls were made.

#### KERNEL MCP — live probe (executed)

Endpoint: `https://mcp.onkernel.com/mcp`.

| Probe | Result |
|---|---|
| `initialize` without token | HTTP **401** |
| `WWW-Authenticate` | `Bearer realm="OAuth", error="invalid_token", ...` |
| Body | `{"error":"invalid_token","error_description":"Missing or invalid access token"}` |
| Account/OAuth token | none in this environment |
| Tools discovered | none (cannot get past auth) |

**Conclusion:** KERNEL is genuinely blocked externally, not merely unimplemented. It is correctly described as excluded from the MVP.

### 3.4 Level 3 — backend MCP client

**NOT IMPLEMENTED.** Evidence:

- `grep -i 'mcp|jsonrpc|sse|thread_id'` across `backend/app/**/*.py` returns **0 real matches**. (Incidental matches were `dataclasses`, `assert`, and similar substrings.)
- `pyproject.toml` has no MCP dependency.
- `Container` constructs `product_source`, `payment_gateway` and `llm`; there is no MCP client, no session manager, no tool-call layer.
- The only product source implementations are `MockProductSource` and `Channel3RestSource` (`app/adapters/__init__.py:13-25`).

### 3.5 Level 4 — backend tool calls

**NOT IMPLEMENTED.** The backend cannot call MCP tools because it has no MCP transport, no session, and no tool dispatcher. The live MCP calls in §3.3 were made by a temporary audit script outside the application.

### 3.6 Level 5 — data integrated

**NOT IMPLEMENTED as an integration.** However, I did execute one additional audit-only check: feed the live Channel3 MCP `structuredContent` into the project’s own `normalise_product`.

Result: **8/8 products parsed successfully**, for example:

```
PARSED A4216 Wireless Headphones | macys.com | 49.49 USD | new | in_stock | redirect=True
PARSED Wireless Headphones | walmart.com | 49.95 USD | new | in_stock | redirect=True
PARSED Anker Soundcore Space One ... | bestbuy.com | 79.99 USD | new | in_stock | redirect=True
SUMMARY parsed=8 products=8 as_of=2026-10-31T08:54:45.136Z
```

This proves the **normaliser is compatible with the live MCP payload shape**. It does **not** mean the backend uses MCP; it cannot, because there is no client. It also does not prove the REST response shape, which may differ.

### 3.7 Error handling, timeouts and availability

| Concern | Status |
|---|---|
| MCP client timeouts | **NOT IMPLEMENTED** (no client) |
| MCP error mapping | **NOT IMPLEMENTED** |
| MCP retry/backoff | **NOT IMPLEMENTED** |
| MCP per-user session / `thread_id` isolation | **NOT IMPLEMENTED**; live response includes `thread_id`, but it is discarded |
| REST timeout/error mapping | **WORKING** in `Channel3RestSource`; see §4 |
| Graceful degradation when MCP is down | **NOT IMPLEMENTED**; MCP is not in the runtime path at all |

**MCP verdict:** MCP is a working *research and agent* channel. It is not a Smartcart integration. Adding it to the runtime would be a new implementation, not a repair.

---

## 4. Channel3 Integration Audit

### 4.1 Two different Channel3 paths

| Path | Purpose | Status |
|---|---|---|
| Channel3 REST (`POST /v1/search`, `x-api-key`) | production product source | **IMPLEMENTED BUT UNVERIFIED** |
| Channel3 MCP (anonymous, `search_products`) | research/fallback | **WORKING externally; NOT IMPLEMENTED in backend** |

### 4.2 REST adapter — implementation

File: `backend/app/adapters/channel3_rest.py`.

| Behaviour | Implementation | Status |
|---|---|---|
| API key required | constructor raises `SourceNotConfigured` if blank (`:41-42`) | **WORKING** (executed) |
| Auth header | `x-api-key` (`:49`) | **IMPLEMENTED BUT UNVERIFIED** live |
| Search request | `POST /v1/search` with `{query, limit}` (`:65-70`) | **IMPLEMENTED BUT UNVERIFIED** |
| Timeout | `httpx.Client(timeout=...)`, default 20 s | **WORKING** structurally |
| 401/403 | mapped to `SourceAuthError` (`:76-77`) | **WORKING** (mock transport) |
| 429 | mapped to `SourceRateLimited` (`:78-79`) | **WORKING** (mock transport) |
| 5xx | mapped to `SourceUnavailable` (`:80-81`) | **WORKING** (mock transport) |
| Other 4xx | mapped to `SourceMalformed` (`:82-83`) | **WORKING** (mock transport) |
| Transport/timeout failures | mapped to `SourceUnavailable` (`:71-74`) | **WORKING** structurally |
| Response envelope | accepts a list or `products` / `results` / `data` (`:55-63`) | **IMPLEMENTED BUT UNVERIFIED** |
| `as_of` | parsed from response; falls back to clock and warns (`:90-92`, `:126-127`) | **WORKING** (mock transport) |
| Product normalisation | delegates to `normalise_product`, skips malformed entries with warnings (`:98-110`) | **WORKING** |
| `max_price` | applied **locally after fetch** (`:112-113`) | **PARTIALLY WORKING** (wastes results; no server-side filter) |
| `limit` | applied locally (`:121-122`) | **WORKING** |
| Pagination | none | **NOT IMPLEMENTED** |
| URL lookup endpoint | none | **NOT IMPLEMENTED** |
| Retry/backoff | none | **NOT IMPLEMENTED** |
| Client lifecycle | `close()` exists (`:52-53`) but the container never calls it | **PARTIALLY WORKING** |

Contract tests executed against `httpx.MockTransport`:

```
channel3 adapter parses live-shaped payload     PASS
channel3 adapter uses response as_of            PASS
channel3 403 -> SourceAuthError                 PASS
channel3 429 -> SourceRateLimited               PASS
channel3 500 -> SourceUnavailable               PASS
channel3 400 -> SourceMalformed                 PASS
channel3 empty key rejected                     PASS
```

Live REST probe: `POST https://api.trychannel3.com/v1/search` unauthenticated returned **403 `{"message":"Forbidden"}`**. No API key exists in this environment, so the real request/response contract remains **UNVERIFIED**.

### 4.3 Payload handling — offers, prices, merchants, URLs, timestamps

File: `backend/app/adapters/normalize.py`.

| Field | Handling | Status |
|---|---|---|
| Product id/title | required; missing either raises `SourceMalformed` (`:193-196`) | **WORKING** |
| Price | accepts number or `{price|amount, currency}`; rejects zero, negative, >2 dp (`:148-161`) | **WORKING** |
| Currency | per-offer, uppercased, 3-letter check in `money.currency_code` | **WORKING** |
| Merchant domain | `domain` or URL host, normalised; `unknown` warns (`:219-221`) | **WORKING** |
| URL | must be `http(s)`; otherwise offer skipped (`:210-213`) | **WORKING** |
| Redirector | flags hosts under `trychannel3.com` (`:22`, `:222-223`) | **WORKING** |
| Destination URL | `merchant_url` kept as `redirect_url`; **destination not resolved or verified** (`:249`) | **PARTIALLY WORKING** |
| Condition | `null` → `UNKNOWN`, never “new” (`:45-61`) | **WORKING** |
| Availability | unknown values → `UNKNOWN` (`:64-81`) | **WORKING** |
| Shipping | parsed only when currency matches; otherwise ignored with warning (`:227-231`) | **WORKING** |
| `as_of` | per-offer parsed from raw; falls back to response/request time (`:252`) | **WORKING** |
| `observed_at` | request time (`:199`) | **WORKING** |
| `thread_id` | not read anywhere | **NOT IMPLEMENTED** |
| Attribution / affiliate parameters | not parsed or surfaced | **NOT IMPLEMENTED** |

The live MCP probe showed the payload shape is compatible: offers carry `domain`, `price.price`, `price.currency`, `condition`, `availability`, and `url`; products carry `id`, `title`, `brands`, `images`. The normaliser parsed all eight live products.

### 4.4 Data-mode honesty

`Settings.data_mode` returns `LIVE` only when `product_source == "channel3"` **and** a key is configured (`config.py:90-94`). With the default settings, health and search report `MOCK`, and the mock source is explicitly labelled (`mock_source.py:3-5`, `:76-83`). That part is honest.

**Provenance gap:** `POST /quotes` labels the quote with `settings.data_mode` (`api/routes.py:144`), not with the provenance of the stored offers. If the source configuration changes between search and quote (for example, from mock to Channel3), a quote built entirely from mock offers can be labelled `LIVE`. The quote row does store the mode it was created with, but the label is configuration-derived, not evidence-derived. This should be treated as **PARTIALLY WORKING** for the “never present mock as live” requirement.

### 4.5 Channel3 verdict

| Sub-feature | Status |
|---|---|
| REST adapter code | **IMPLEMENTED BUT UNVERIFIED** |
| REST live request/response | **BLOCKED EXTERNALLY** (no API key; 403) |
| REST error mapping | **WORKING** (mock transport) |
| MCP reachability/tools/search | **WORKING** |
| Backend MCP use | **NOT IMPLEMENTED** |
| Live MCP payload normalisation | **WORKING** (audit-only execution) |
| Pagination / lookup / retries / attribution | **NOT IMPLEMENTED** |
| Data-mode provenance | **PARTIALLY WORKING** |

---

## 5. KERNEL Integration Audit

| Question | Answer |
|---|---|
| Configured in OpenCode? | **No** — no MCP config anywhere |
| Connection works? | **No** — unauthenticated `initialize` returns 401 |
| Authentication works? | **No credentials/account** in this environment |
| Tools discoverable? | **No** — blocked before `tools/list` |
| Functionality integrated into Smartcart? | **No** — zero `kernel` references in `backend/app` |
| Unavailable auth handled? | **Not applicable / NOT IMPLEMENTED** — there is no client to handle it |
| Health/reporting mentions KERNEL? | **No** — `/api/v1/health` reports only Channel3, AI and PayPal |

Exact live response:

```
HTTP 401
WWW-Authenticate: Bearer realm="OAuth", error="invalid_token",
  error_description="Missing or invalid access token",
  resource_metadata="https://mcp.onkernel.com/.well-known/oauth-protected-resource/mcp"
{"error":"invalid_token","error_description":"Missing or invalid access token"}
```

**Status:** **BLOCKED EXTERNALLY** for connection; **NOT IMPLEMENTED** for integration. Excluding KERNEL from the MVP is the correct decision; no code falsely claims otherwise.

---

## 6. PayPal Integration Audit

### 6.1 Implementation

Files: `backend/app/adapters/paypal.py`, `backend/app/application/payments.py`, `backend/app/api/webhooks.py`.

| Behaviour | Implementation | Status |
|---|---|---|
| OAuth client credentials | `POST /v1/oauth2/token`, Basic auth (`paypal.py:137-164`) | **IMPLEMENTED BUT UNVERIFIED** live; contract-tested |
| Token cache / early refresh | in-memory token with monotonic expiry (`:138-139`, `:161-163`) | **WORKING** structurally |
| Create order | `POST /v2/checkout/orders`, `intent=CAPTURE` (`:188-231`) | **IMPLEMENTED BUT UNVERIFIED** live |
| Server-side amount | body uses `quote.grand_total`, not client input (`:197-200`) | **WORKING** |
| Idempotency | `PayPal-Request-Id` header (`:166-174`) plus local idempotency key (`payments.py:119-144`) | **WORKING** (contract + fake-gateway e2e) |
| Get order | `GET /v2/checkout/orders/{id}` (`:233-254`) | **IMPLEMENTED BUT UNVERIFIED** live |
| Capture | `POST /v2/checkout/orders/{id}/capture` (`:256-282`) | **IMPLEMENTED BUT UNVERIFIED** live |
| Capture response validation | requires capture id, amount and currency (`:273-274`) | **WORKING** |
| Amount mismatch | rejected before storing (`payments.py:161-172`) | **WORKING** (e2e fake gateway) |
| Webhook signature | PayPal verify-webhook-signature API, requires `webhook_id` (`:285-330`) | **IMPLEMENTED BUT UNVERIFIED** live; contract-tested |
| Webhook replay dedupe | unique `(provider, provider_event_id)`; duplicate returns `applied=false` (`payments.py:486-514`) | **WORKING** (e2e) |
| Webhook amount check | compares PayPal capture amount to quote total (`:522-533`) | **WORKING** |
| Error mapping | auth / rejected / unavailable / ambiguous (`:24-47`) | **WORKING** (contract) |
| Retry/backoff | none | **NOT IMPLEMENTED** |
| Sandbox vs live selection | `paypal_env` label, separate `paypal_base_url` | **PARTIALLY WORKING**; see hazards below |

### 6.2 Executed contract and flow tests

Contract tests against `httpx.MockTransport`:

```
paypal create order parsed                     PASS
paypal request id sent                         PASS
paypal amount server-side                      PASS
paypal description says not retailer purchase  PASS
paypal basic auth used                         PASS
paypal get order                               PASS
paypal capture parsed                          PASS
paypal webhook verified                        PASS
paypal webhook missing headers rejected        PASS
paypal missing credentials rejected            PASS
paypal bad credentials -> auth error           PASS
```

End-to-end with a fake gateway (no network, no real money):

```
payment order created with fake gateway        PASS
payment amount server-side                     PASS
sandbox disclosure present                     PASS
payment creation idempotent                    PASS
capture succeeds                               PASS
capture amount matches                         PASS
duplicate capture replayed                     PASS
webhook accepted and deduped                   PASS
provider amount mismatch rejected              PASS
```

These results are meaningful for the application logic, but they do **not** prove PayPal’s live API behaviour, live response shapes, or webhook delivery.

### 6.3 Honesty check: payment vs retailer purchase

This is implemented correctly.

- `Captures.retailer_purchase_state` defaults to `"unknown"` (`persistence/tables.py:191`).
- `_mark_captured` sets `retailer_purchase_state=UNKNOWN` (`payments.py:251`).
- `CaptureOutcome.disclaimer` is an explicit statement that the payment is not proof of a retailer purchase (`payments.py:280-281`).
- The create-order response includes: “This PayPal payment is a demo payment to Smartcart, not a purchase from a retailer. The retailer purchase remains yours to complete.” (`api/routes.py:313-317`).
- The PayPal order description says “not a retailer purchase” (`paypal.py:196`).

I verified the capture response returned `retailer_purchase_state="unknown"` and the disclaimer text. There is no code path that claims a retailer order was placed.

### 6.4 Sandbox/live configuration hazards

1. **`SMARTCART_PAYPAL_WEBHOOK_ID` is missing from `.env.example`.** The setting exists (`config.py:49`) and webhook verification refuses to run without it (`paypal.py:286-287`), so an operator following `.env.example` gets a gateway that can create orders but cannot verify webhooks. The webhook endpoint fails closed, which is safe, but the deployment is incomplete. The same file also omits `SMARTCART_GUARD_APPROVAL_TTL_SECONDS`, `SMARTCART_GUARD_MERCHANT_ALLOWLIST` and `SMARTCART_GUARD_MERCHANT_DENYLIST`, although all three are supported settings (`config.py:51-59`).
2. **`paypal_env=live` does not change `paypal_base_url`.** The default base URL is sandbox (`config.py:46`). Setting only the environment label to `live` would keep calling the sandbox host while labelling records “live”. There is no startup validation tying the two together.
3. **The G-014 environment check is effectively tautological for the real gateway.** At capture, `order_environment=provider.environment` and `expected_environment=gateway.environment` (`payments.py:390-391`). For `PayPalGateway`, both values originate from the same `self.environment` attribute (`paypal.py:125`, `:229`, `:252`), so the rule cannot detect a sandbox/live host mismatch. It only catches a mismatched injected gateway. This is a weak control, not a working safety net.
4. **Webhook capture does not create a `Capture` row or consume the approval.** `PAYMENT.CAPTURE.COMPLETED` sets `payment.status="captured"` (`payments.py:530`) but does not call `_mark_captured`. A later `/capture` call can reconcile and create the row, but the intermediate state is inconsistent with the capture endpoint’s model. This should be made explicit and tested.

### 6.5 PayPal verdict

| Area | Status |
|---|---|
| Adapter and application logic | **PARTIALLY WORKING** (contract/fake-gateway tested) |
| Live sandbox order/capture | **BLOCKED EXTERNALLY** — no `client_id`/`client_secret` |
| Live webhook delivery | **BLOCKED EXTERNALLY** — no public HTTPS URL |
| Payment/retailer distinction | **WORKING** |
| Production configuration safety | **PARTIALLY WORKING** (webhook id missing from example; env/base URL can diverge) |

---

## 7. Gemini Integration Audit

File: `backend/app/adapters/gemini.py`; factory: `backend/app/adapters/__init__.py:28-35`.

| Behaviour | Implementation | Status |
|---|---|---|
| API client | `httpx.Client` with `x-goog-api-key` (`gemini.py:21-39`) | **IMPLEMENTED BUT UNVERIFIED** |
| Model configuration | `SMARTCART_GEMINI_MODEL`, default `gemini-2.5-flash` (`config.py:40`) | **WORKING** structurally |
| Endpoint | `POST /v1beta/models/{model}:generateContent` (`gemini.py:51`) | **IMPLEMENTED BUT UNVERIFIED** |
| Structured output | `responseMimeType=application/json`, `responseSchema` (`:44-48`) | **WORKING** (contract) |
| Response parsing | joins text parts, `json.loads`, requires an object (`:64-74`) | **WORKING** (contract) |
| Pydantic/schema validation of the result | **none** — returns a plain `dict`; the caller must validate | **NOT IMPLEMENTED** |
| Retry/backoff | none | **NOT IMPLEMENTED** |
| Timeout | 30 s client timeout | **WORKING** structurally |
| 429 handling | `LlmError("rate limit reached")` (`:57-58`) | **WORKING** (contract) |
| 401/403 handling | `LlmError("rejected the API key")` (`:59-60`) | **WORKING** (contract) |
| Other 4xx/5xx | generic `LlmError` (`:61-62`) | **WORKING** (contract) |
| Unparseable output | `LlmError` (`:70-71`) | **WORKING** (contract) |
| Provider abstraction | `LlmClient` Protocol + factory; only `none` and `gemini` | **PARTIALLY WORKING** |
| Missing key fails safely | factory returns `None`; app runs | **WORKING** (executed) |
| AI use case in the application | **none** — `container.llm` is never read; `structured()` is never called | **NOT IMPLEMENTED** |
| Mock LLM provider | does not exist | **NOT IMPLEMENTED** |

Executed contract tests:

```
gemini structured output parsed     PASS
gemini non-JSON rejected            PASS
gemini 429 mapped to LlmError       PASS
gemini empty key rejected           PASS
gemini without key disables AI      PASS
```

Static proof that AI is never invoked:

```
grep 'container\.llm|\.llm\b|structured\(' backend/app
  -> only ports/__init__.py (Protocol), adapters/gemini.py (implementation),
     container.py:36 (assignment)
```

No application module calls the LLM. Therefore:

- AI-generated claims cannot become verified facts, because no AI output enters the system.
- AI cannot authorize a payment, because it has no code path to Guard, approval or payment.
- The **architectural boundary** is correct, but it is **untested against a real model** and the **advisory feature is not implemented**.

**Gemini verdict:** **IMPLEMENTED BUT UNVERIFIED** as an adapter; **NOT IMPLEMENTED** as a product feature. Live use is **BLOCKED EXTERNALLY** by the absence of a key and a data-policy decision.

---

## 8. Deterministic Core Audit

### 8.1 Feature status

| Feature | Status | Evidence / notes |
|---|---|---|
| Product and offer data models | **WORKING** | `domain/models.py`; Decimal prices, normalised domains, UTC coercion |
| Evidence and provenance | **PARTIALLY WORKING** | evidence rows for products/offers with `payload_sha256`; digest is never re-verified on read |
| Product/offer snapshots | **PARTIALLY WORKING** | quote-level snapshot hash; offer history only via evidence; no snapshot entity or time-travel API |
| Data hashing | **WORKING** | canonical JSON, sorted keys, fixed 2-dp Decimals (`snapshot.py:25-52`) |
| Change detection | **PARTIALLY WORKING** | detects price/quantity/merchant changes; Decimal journaling crashes |
| Price comparison | **NOT IMPLEMENTED** | search lists offers per product; no compare/rank endpoint or UI |
| Cost calculation | **WORKING** | line subtotal, shipping total, grand total recomputed exactly (`models.py:213`, `snapshot.py:83-85`) |
| Purchase Guard | **WORKING** | 17 pure rules, no override parameter |
| Purchase intent validation | **PARTIALLY WORKING** | intent created and status-tracked; no intent-level validation rules |
| Duplicate intent detection | **NOT IMPLEMENTED** | only duplicate offer lines (G-010) and duplicate captures (G-012) |
| Expired intent detection | **NOT IMPLEMENTED** | `PurchaseIntent` has no `expires_at` (`tables.py:126-135`) |
| User approval workflow | **PARTIALLY WORKING** | consent, snapshot binding, expiry, supersede; revalidation crashes; reject→re-approve gap |
| Purchase journal / audit history | **WORKING** | hash chain verifies; append-only triggers block UPDATE/DELETE |
| AI vs deterministic separation | **WORKING structurally** | LLM never called; Guard has no AI input; AI trust level is never a fact |

### 8.2 Guard rule matrix

`RULES` contains 17 rules (`domain/guard/rules.py:369-387`). All 17 were exercised directly with `evaluate(quote, context)`.

| Code | Rule | Tested behaviour | Status |
|---|---|---|---|
| G-001 | Currency consistency | mixed currencies denied | **WORKING** |
| G-002 | Per-item ceiling | user limit tightens, denial | **WORKING** |
| G-003 | Total ceiling | user limit tightens, denial | **WORKING** |
| G-004 | Quantity ceiling | over-limit denied | **WORKING** |
| G-005 | Availability | out-of-stock DENY; unknown NEEDS_INFO | **WORKING** |
| G-006 | Evidence freshness | stale and future timestamps denied | **WORKING** |
| G-007 | Condition | used DENY; unknown NEEDS_INFO | **WORKING** |
| G-008 | Merchant policy | allowlist and denylist denial | **WORKING** |
| G-009 | Evidence completeness | missing evidence NEEDS_INFO | **WORKING** |
| G-010 | Product/variant identity | identity mismatch and duplicate lines denied | **WORKING** |
| G-011 | Snapshot binding | tampered hash and foreign approval denied | **WORKING** |
| G-012 | Duplicate capture | existing capture denied | **WORKING** |
| G-013 | Amount match | provider amount mismatch denied | **WORKING** |
| G-014 | Environment consistency | mismatch denied **when two independent values are supplied** | **PARTIALLY WORKING** (tautological for the real gateway) |
| G-015 | Policy version | old policy denied | **WORKING** |
| G-016 | Approval validity | missing, consumed, expired, foreign-user approvals denied | **WORKING** |
| G-017 | Arithmetic | tampered totals denied | **WORKING** |

Structural guarantees verified:

- `evaluate(quote, context)` has exactly two parameters; there is no override, force, or skip argument (`engine.py:29-66`).
- Any `FAIL` → `DENY`; otherwise any `INSUFFICIENT` → `NEEDS_INFO`; only a clean run → `ALLOW`.
- The Guard API takes a quote and context, not an AI response. There is no AI entry point.
- Unknown condition is never promoted to “new” (`normalize.py:45-61`; `schemas.py:56-59`).

### 8.3 Snapshot and change-detection tests

Executed:

```
same content hashes equally                         PASS
quote id excluded from hash                         PASS
price change detected                               PASS
snapshot hash changes with price                    PASS
provenance-only change ignored when requested       PASS
provenance change visible by default                PASS
```

The projection excludes database row ids and quote ids, so two quotes with identical purchase content hash identically (`snapshot.py:55-98`). Revalidation ignores `evidence` and `as_of` differences deliberately (`approvals.py:371`), while still detecting price, quantity, merchant, condition and availability changes.

### 8.4 AI boundary

- `evidence_is_fact("ai_statement", "ai_unverified")` → `False` (`repositories.py:710-718`).
- `TrustLevel.AI_UNVERIFIED.is_fact` → `False` (executed).
- The redaction helper removes secret keys and bearer values (executed).
- No AI output is persisted, trusted or acted on, because no AI output is produced.

**Verdict:** separation is correct **by construction**, but the AI feature itself is absent, so the boundary has not been tested against real model output or prompt injection. The unused `security/untrusted.py` helpers suggest an intended projection layer that was never wired in.

### 8.5 Deterministic core verdict

**WORKING** for: models, money, quoting, Guard, snapshot hashing, journal, evidence creation, mock discovery, and the AI/deterministic boundary.

**PARTIALLY WORKING** for: evidence verification, snapshots as historical objects, change detection under the shipped SQLite configuration, approval lifecycle, and intent validation.

**NOT IMPLEMENTED** for: price comparison, basket optimisation, product ranking/evaluation, duplicate intent detection, expired intent detection, and any AI-assisted recommendation.

---

## 9. API and Database Audit

### 9.1 Endpoint status (executed)

| Method | Path | Result | Status |
|---|---|---|---|
| GET | `/api/v1/health` | 200; `status=ok`, `data_mode=MOCK`, `journal_chain_ok=true` | **WORKING** |
| GET | `/health` | 404 | **NOT IMPLEMENTED** (path differs) |
| GET | `/` | 404 | **NOT IMPLEMENTED** (no frontend) |
| GET | `/docs` | 200 | **WORKING** |
| POST | `/api/v1/search` | 200; products, offers, evidence, warnings | **WORKING** (mock) |
| GET | `/api/v1/offers` | 200 | **WORKING** |
| POST | `/api/v1/quotes` | 201; ALLOW, total 128.00, `sha256:` snapshot | **WORKING** |
| GET | `/api/v1/quotes/{id}` | 200; preserves hash | **WORKING** |
| POST | `/api/v1/quotes/{id}/approve` | 200; consent required; snapshot bound; expiry | **WORKING** |
| POST | `/api/v1/quotes/{id}/reject` | 200 | **WORKING** |
| GET | `/api/v1/approvals/{id}` | 200 | **WORKING** |
| POST | `/api/v1/approvals/{id}/revalidate` | **crashes on shipped SQLite** | **BROKEN** |
| POST | `/api/v1/payments/orders` | 503 without credentials; 201 with fake gateway after shim | **PARTIALLY WORKING** |
| POST | `/api/v1/payments/orders/{id}/capture` | 200 with fake gateway after shim | **PARTIALLY WORKING** |
| POST | `/webhooks/paypal` | 503 when unconfigured; dedupe with fake gateway | **PARTIALLY WORKING** |
| GET | `/api/v1/journal` | 200; chain ok; user-filtered | **WORKING** |
| GET | `/api/v1/evidence/{id}` | 200; payload returned | **WORKING** |

Health response observed from a real uvicorn process:

```json
{"status":"ok","environment":"dev","data_mode":"MOCK","product_source":"mock",
 "channel3_configured":false,"ai_provider":"none","paypal_configured":false,
 "paypal_environment":"sandbox","guard_policy_version":"1.0.0",
 "journal_chain_ok":true,"journal_broken_at":null}
```

**Documentation mismatch:** the health endpoint is `/api/v1/health`, not `/health`. The milestone documents sometimes write `/health`. A `/health` probe returns 404. If a deployment expects `/health`, it will fail.

### 9.2 Validation behaviour (executed)

| Case | Result |
|---|---|
| Client-supplied `price` in quote request | 422 (extra field forbidden) |
| Empty `query` | 422 (min length) |
| User limit above policy ceiling | accepted but can only tighten; G-003 denies |
| Missing `explicit_consent` | 400 `EXPLICIT_CONSENT_REQUIRED` |
| Wrong snapshot hash | 409 `SNAPSHOT_MISMATCH` |
| Guard-blocked approval | 409 `GUARD_DENIED` |
| Unknown condition approval | 409 `GUARD_DENIED` |
| Cross-user quote access | 404 |
| Cross-user approval | 404 `NOT_FOUND` |
| Payment without credentials | 503 `PAYPAL_NOT_CONFIGURED` |
| Webhook without credentials | 503 |
| Channel3 source without key | fails fast at startup with `SourceNotConfigured` |
| Gemini without key | AI disabled; app starts |

Request schemas use `extra="forbid"`, which prevents clients from injecting prices, totals or guard fields (`api/schemas.py:20-115`). The server prices everything from persisted offers (`application/quotes.py:64-85`).

### 9.3 Database schema

| Table | `user_id`? | Notes |
|---|---|---|
| `users` | n/a | identity table |
| `products` | yes | unique `(user_id, source, source_product_id)` |
| `offers` | yes | unique `(user_id, source, source_offer_id)`; latest observation overwrites |
| `evidence` | yes | append-only trigger; `payload_sha256` stored |
| `quotes` | yes | mutable row; no append-only trigger |
| `purchase_intents` | yes | no TTL, no duplicate constraint |
| `approvals` | yes | status lifecycle |
| `payment_orders` | yes | unique `(provider, provider_order_id)` |
| `captures` | yes | unique `(quote_id, snapshot_hash)`; append-only trigger |
| `transaction_events` | yes (nullable) | unique `(provider, provider_event_id)`; append-only trigger |
| `idempotency_keys` | no | keyed by a string that includes `user_id` |
| `journal_events` | yes (nullable) | append-only trigger; hash chain |

All user-facing entities carry `user_id`, and repository reads filter on it. Cross-user tests returned 404 for quotes and approvals, and the `other_user` journal did not contain the demo user’s approval events.

### 9.4 Append-only and integrity

Installed SQLite triggers (`persistence/base.py:49-73`) block UPDATE/DELETE on:

- `journal_events`
- `evidence`
- `transaction_events`
- `captures`

Executed:

```
journal UPDATE blocked by trigger     PASS
journal DELETE blocked by trigger     PASS
evidence UPDATE blocked by trigger    PASS
```

**Gaps:**

- `quotes` is **not** append-only, despite Milestone 1 describing it as insert-only. `approve_quote` and `reject_quote` update `row.status` (`approvals.py:114`, `:128`, `:268`).
- `evidence.payload_sha256` is written (`repositories.py:276`) and returned (`api/schemas.py:190`) but never recomputed on read. A corrupted payload would not be detected.
- `JournalRepository.verify_chain()` reads the entire journal on every health and journal request (`repositories.py:698-707`; `api/routes.py:53`, `:368`). That is acceptable for a demo and a scalability problem later.
- `journal.append` selects the last row and appends; concurrent writers could race. SQLite serialises writes, but this is not a portable concurrency design.

### 9.5 Bugs found in the shipped path

#### Bug 1 — naive vs aware datetimes (critical)

`revalidate_approval` compares `now` (aware UTC from `SystemClock` or `FixedClock`) with `approval.expires_at` read back from SQLite (`approvals.py:331`):

```python
if now >= approval.expires_at:
```

SQLite’s `DateTime(timezone=True)` does **not** preserve tzinfo. The stored value comes back naive, so the comparison raises:

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

Observed on the first call to `POST /api/v1/approvals/{id}/revalidate`. This blocks:

- `/approvals/{id}/revalidate`
- `/payments/orders` (it calls revalidation first, `payments.py:103-111`)
- `/payments/orders/{id}/capture` (it calls revalidation, `payments.py:316-324`)

This is the **single most important defect** in the project. It means the advertised approval → payment path does not run to completion in the default configuration.

#### Bug 2 — Decimal in journal JSON (critical on price change)

`_change_payload` returns raw `MaterialChange` values (`approvals.py:63-67`), and the journal JSON column receives them when a price changes (`approvals.py:381`). `Decimal` is not JSON-serialisable, so invalidation raises:

```
TypeError: Object of type Decimal is not JSON serializable
```

Observed when changing an offer price after approval and revalidating. The change is detected correctly, but the journal write crashes, so the request fails instead of returning a clean `DENY` with `offer_changed`.

#### Bug 3 — `add_capture` rolls back the whole transaction

`PaymentRepository.add_capture` catches `IntegrityError` and calls `self.session.rollback()` (`repositories.py:537-539`). That rolls back **the entire request transaction**, including any journal entries, approval consumption, payment status change, and webhook event written earlier in the same unit of work.

A duplicate capture is supposed to be a handled conflict. Instead it can discard audit evidence. The fix should use a savepoint (`begin_nested`) or a pre-check, not a full rollback.

#### Bug 4 — reject → re-approve gap

`approve_quote` does not check whether the quote or intent was previously rejected. `reject_quote` sets the quote status to `rejected` and marks active approvals rejected (`approvals.py:250-289`), but a subsequent `POST /quotes/{id}/approve` returns **200** and creates a new approval. Executed:

```
reject_status                       200
approve_after_reject_status         200
```

This is a workflow integrity gap. A rejected quote should require a fresh quote or an explicit un-reject transition, not silently become approvable again.

#### Bug 5 — dead and broken digest helper

`digests_equal` calls `hashlib.compare_digest`, which does not exist (`snapshot.py:160-161`). The `hasattr` fallback means it silently degrades to `a == b`. It is currently unused, so there is no live impact, but it is wrong code.

### 9.6 Postgres

**NOT IMPLEMENTED.** No Postgres driver is installed or declared. `create_engine_for` would raise a missing-driver error for a `postgresql://` URL. The schema uses SQLite-specific append-only triggers that are skipped on non-SQLite dialects (`base.py:57-59`), so even with a driver, the append-only guarantees would silently disappear on Postgres. A production Postgres design needs equivalent constraints/triggers and a migration history.

---

## 10. Test Results

### 10.1 Project tests

There is no project test suite.

```
$ .venv\Scripts\python.exe -m pytest
PytestConfigWarning: No files were found in testpaths; ...
1 warning in 0.09s
exit code 1
```

`pyproject.toml` sets `testpaths = ["tests"]`, but `backend/tests/` does not exist. The milestone document cites four test files that are not present. **No project test has been run because no project test exists.**

### 10.2 Audit harnesses (executed, not part of the project)

| Script | Purpose | Checks | Result |
|---|---|---|---|
| `%TEMP%\opencode\audit_smoke.py` | end-to-end API, DB, journal, fake PayPal | 64 | **64/64 passed after two audit-only shims** |
| `%TEMP%\opencode\audit_units.py` | Guard matrix, snapshots, normalisation, adapters | 69 | **69/69 passed** |
| `%TEMP%\opencode\audit_edge.py` | reject/re-approve, supersede, cross-user isolation | 5 observations | gap found (see §9.5) |
| `%TEMP%\opencode\audit_mcp.py` | live Channel3/KERNEL probes | handshake/list/search | Channel3 200; KERNEL 401 |
| `%TEMP%\opencode\audit_mcp_normalize.py` | live MCP payload → normaliser | 8 products | **8/8 parsed** |

**Important caveat:** the 64/64 end-to-end result required two in-memory shims:

1. Normalise naive datetimes on `ApprovalRepository.get` (works around Bug 1).
2. Stringify `Decimal` values in `_change_payload` (works around Bug 2).

Without the shims, the run stops at the first revalidation with the `TypeError`. The shims are **not** applied to the project; they exist only to let the rest of the flow be audited. Therefore the correct statement is:

> The surrounding logic passes 64 checks once the two persistence bugs are bypassed; the shipped code does not pass those checks unaided.

The first unit-harness run had two failures caused by a defect in the audit harness (it did not propagate `as_of` into the test offer). After correcting the harness, all 69 checks passed. That was not an application failure.

### 10.3 What was not executed

| Path | Reason |
|---|---|
| Live Channel3 REST search | no API key; unauthenticated 403 |
| Live Gemini generation | no key; no accepted data-policy decision |
| Live PayPal sandbox/live order, capture, webhook | no client credentials; no public HTTPS URL |
| KERNEL handshake/tools | no account/token; 401 |
| Postgres engine/schema | no driver, no server |
| Frontend/browser flow | no frontend |
| Concurrency/load tests | out of scope for a single-user audit |

No real payment, capture, purchase or destructive webhook was performed.

### 10.4 Reproduction pointers

The audit used:

```
C:\Users\Admin\Desktop\smartcart\.venv\Scripts\python.exe
working directory: C:\Users\Admin\Desktop\smartcart\backend
```

Key commands:

```
python -m compileall -q app
python -m pip check
python -c "import app.main"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8123
python -m pytest
python %TEMP%\opencode\audit_smoke.py
python %TEMP%\opencode\audit_units.py
```

The temporary scripts are available under `C:\Users\Admin\AppData\Local\Temp\opencode\` if you want to re-run them. They are audit artefacts, not project tests.

---

## 11. Security and Reliability Issues

| # | Severity | Issue | Evidence | Impact |
|---|---|---|---|---|
| S1 | **High** | `X-User-Id` is unauthenticated and spoofable | `api/deps.py:48-55` | Anyone who can reach the API can act as any user by setting a header. Local demo only. |
| S2 | **High** | No authentication, sessions, API keys or rate limiting | no auth middleware anywhere | Cannot be exposed beyond localhost safely. |
| S3 | **High** | SQLite datetime bug breaks approval/payment | `approvals.py:331` | Core flow 500s; blocks payment and capture. |
| S4 | **High** | Decimal journal serialisation breaks price-change invalidation | `approvals.py:63-67`, `:381` | Fails instead of cleanly invalidating; audit entry lost. |
| S5 | **High** | `add_capture` full rollback on duplicate | `repositories.py:537-539` | Can discard journal/approval/payment state written earlier in the request. |
| S6 | **Medium** | Quote rows are mutable; no DB-level append-only | `tables.py:103-123`; `base.py:49-54` | Audit claim “immutable basket” is not enforced at the database. |
| S7 | **Medium** | Evidence digest is stored but never verified | `repositories.py:276`; only read in `schemas.py:190` | Tampered payload would not be detected. |
| S8 | **Medium** | Reject → re-approve is allowed | `approvals.py:70-247`; executed | A rejected intent can silently become active again. |
| S9 | **Medium** | G-014 environment check is tautological for the real gateway | `payments.py:390-391`; `paypal.py:125`, `:229` | Cannot detect sandbox/live host mismatch. |
| S10 | **Medium** | `paypal_env=live` does not switch the base URL | `config.py:43-49` | Operator can believe they are live while calling sandbox. |
| S11 | **Medium** | `SMARTCART_PAYPAL_WEBHOOK_ID` missing from `.env.example` | `.env.example:23-29`; `config.py:49` | Webhooks fail closed after a by-the-book deployment. |
| S12 | **Medium** | Data-mode label comes from settings, not offer provenance | `api/routes.py:144` | A mock-sourced quote can be labelled LIVE after a config change. |
| S13 | **Medium** | No retries/backoff for external calls | Channel3/Gemini/PayPal adapters | Transient failures become user-visible errors; PayPal create/capture are especially fragile. |
| S14 | **Medium** | No Postgres driver or append-only equivalent | `pyproject.toml`; `base.py:57-59` | “SQLite/Postgres” is not true; a Postgres deployment loses append-only guarantees. |
| S15 | **Low** | HTTP clients are never closed | `container.py`; adapters | Resource leak over long-running processes. |
| S16 | **Low** | Health verifies the whole journal on every call | `api/routes.py:53`; `repositories.py:698-707` | O(n) latency as the journal grows. |
| S17 | **Low** | Webhook dedupe has a check-then-insert race | `repositories.py:560-583` | Concurrent duplicates can raise instead of returning `duplicate=true`. |
| S18 | **Low** | Dead code | `security/untrusted.py`; `snapshot.digests_equal`; `schemas.guard_outcome_value`; `schemas.availability_enum`; `common.default_limits` | Maintenance risk; one helper is outright broken. |
| S19 | **Low** | `Redirector` destination is not resolved or verified | `normalize.py:249` | A user is sent to an unverified destination; the UI must warn. |
| S20 | **Info** | No secrets found in source | scan of `*.py`, `*.json`, `*.md`, `*.toml`, `.env.example` | Good. `.env` is absent and ignored. |
| S21 | **Low** | Importing `app.main` constructs the app and initialises the database | `main.py:7`; `container.py:60-66` | Import-time side effects create DB files and clients; complicate tests, CLIs and tooling. |

### Secret handling — verified positive

- Settings use `SecretStr`; empty strings are normalised to `None` (`config.py:61-76`).
- No `sk-...`, PayPal client secret, private key or similar literal was found in project files.
- Journal payloads are passed through `redact()` (`journal/writer.py:39`, `:61`).
- Contract test confirmed `client_secret`, `api_key` and bearer values are redacted.

The OpenCode config directory contains a non-project service file; its contents are not reproduced here.

### Reliability positives

- External adapters map transport errors to typed exceptions instead of leaking raw provider errors (`ports/__init__.py`, `channel3_rest.py:71-83`, `paypal.py:24-47`, `gemini.py:50-62`).
- Payment amounts are always derived server-side from the quote; the create-order contract test confirmed the client cannot set the amount.
- Webhook signature verification fails closed when credentials or the webhook id are missing.
- Capture is refused if the provider amount or currency differs from the quote.
- The journal chain was verified repeatedly; the chain remains valid after the exercised operations.
- The mock source is clearly labelled `MOCK` and cannot accidentally claim live data unless the configuration is changed.

---

## 12. Missing or Incomplete Features

### 12.1 Integrations and agent layer

| Feature | Status |
|---|---|
| OpenCode MCP configuration for Channel3 | **NOT IMPLEMENTED** |
| OpenCode MCP configuration for KERNEL | **NOT IMPLEMENTED** |
| Backend MCP client / transport | **NOT IMPLEMENTED** |
| Backend calls to `search_products` etc. | **NOT IMPLEMENTED** |
| MCP per-user session / `thread_id` isolation | **NOT IMPLEMENTED** |
| Channel3 REST live verification | **BLOCKED EXTERNALLY** |
| Channel3 REST pagination, lookup, retries | **NOT IMPLEMENTED** |
| KERNEL account, OAuth, tools, use case | **BLOCKED EXTERNALLY / NOT IMPLEMENTED** |
| Gemini live verification | **BLOCKED EXTERNALLY** |
| Gemini use case (intent parsing, summarisation, ranking) | **NOT IMPLEMENTED** |
| Gemini response validation into typed models | **NOT IMPLEMENTED** |
| PayPal live sandbox verification | **BLOCKED EXTERNALLY** |
| PayPal public webhook endpoint | **BLOCKED EXTERNALLY** |

### 12.2 Product features

| Feature | Status |
|---|---|
| Frontend / demo UI | **NOT IMPLEMENTED** |
| Offer comparison UI or endpoint | **NOT IMPLEMENTED** |
| Basket optimisation | **NOT IMPLEMENTED** |
| Product ranking / evaluation | **NOT IMPLEMENTED** |
| Duplicate intent detection | **NOT IMPLEMENTED** |
| Intent expiry | **NOT IMPLEMENTED** |
| Retailer checkout / purchase completion | **NOT IMPLEMENTED** (and intentionally out of scope) |
| Notifications / background reconciliation | **NOT IMPLEMENTED** |
| Order tracking after payment | **NOT IMPLEMENTED** |

### 12.3 Platform and operations

| Feature | Status |
|---|---|
| Project test suite | **NOT IMPLEMENTED** |
| Alembic migrations | **NOT IMPLEMENTED** |
| Postgres driver and dialect support | **NOT IMPLEMENTED** |
| Auth / sessions / API keys | **NOT IMPLEMENTED** |
| Rate limiting | **NOT IMPLEMENTED** |
| CORS policy | **NOT IMPLEMENTED** (safe default for same-origin only) |
| Structured logging / metrics / tracing | **NOT IMPLEMENTED** |
| Retry/backoff/circuit breakers | **NOT IMPLEMENTED** |
| CI pipeline | **NOT IMPLEMENTED** |
| README / runbook | **NOT IMPLEMENTED** |
| `/health` compatibility alias | **NOT IMPLEMENTED** |

### 12.4 Documentation accuracy

The milestone documents are partly aspirational. The most important corrections are:

- No `backend/migrations/`.
- No `backend/tests/`.
- No frontend.
- No mock LLM provider.
- Gemini uses `generateContent`, not the “Interactions API”.
- Quotes are not insert-only at the database level.
- Health is `/api/v1/health`, not `/health`.

---

## 13. Critical Blockers

Ordered by impact on continuing development.

### B1 — Revalidation crashes on SQLite (critical)

**Evidence:** `backend/app/application/approvals.py:331`; observed `TypeError: can't compare offset-naive and offset-aware datetimes`.

**Why it blocks:** every payment order and capture calls revalidation first. Without this fix, the shipped approval → payment path cannot succeed.

**Minimum fix:** normalise all datetimes at the persistence boundary (read and write), or use a timezone-aware type decorator, and add a regression test that runs on SQLite.

### B2 — Decimal journal serialisation (critical)

**Evidence:** `approvals.py:63-67`, `:381`; observed `TypeError: Object of type Decimal is not JSON serializable`.

**Why it blocks:** a legitimate price change after approval fails instead of producing a clean invalidation and audit record.

**Minimum fix:** canonicalise `MaterialChange` values before journaling (Decimal → fixed string, datetime → ISO), and test the price-change path.

### B3 — No tests (critical for safe progress)

**Evidence:** `pytest` collects 0; cited test files do not exist.

**Why it blocks:** an approval/revalidation integration test would have caught the first bug, and a price-change test the second. Every future change is currently unguarded.

**Minimum fix:** add a small but real suite: Guard matrix, snapshot determinism, approval/revalidation on SQLite, payment idempotency with a fake gateway, journal append-only and chain verification, cross-user isolation.

### B4 — No migrations / no Postgres path (high)

**Evidence:** no `alembic.ini`, no `migrations/`, no Postgres driver.

**Why it blocks:** there is no safe schema evolution and no deployable database story.

**Minimum fix:** initialise Alembic, baseline the current schema, add the Postgres driver, and decide whether append-only is enforced by triggers, permissions or application policy on Postgres.

### B5 — No authentication (high)

**Evidence:** `X-User-Id` header; no auth middleware; no rate limiting.

**Why it blocks:** the service cannot be exposed beyond localhost, and “users control their own purchases” is not enforceable against a hostile client.

**Minimum fix:** add real identity (session or API key), derive `user_id` from it, and add rate limiting. If the product remains local-only, document that explicitly and keep the service bound to loopback.

### B6 — Live product source unavailable (high)

**Evidence:** no Channel3 API key; REST 403; only mock data works.

**Why it blocks:** the product cannot demonstrate real prices or real merchants.

**Minimum fix:** obtain a key and verify REST request/response, pagination, error handling and data-mode labelling against live data. If a key is not available, decide whether the anonymous MCP tier is an acceptable prototype source and implement a proper adapter with timeouts and per-user session handling.

### B7 — Live payment unavailable (high)

**Evidence:** no PayPal credentials; no public HTTPS URL.

**Why it blocks:** the payment rail is contract-tested only. Sandbox behaviour, webhook delivery and live response shapes are unknown.

**Minimum fix:** create a sandbox app, add `SMARTCART_PAYPAL_WEBHOOK_ID` to the example env, validate that `paypal_env` and `paypal_base_url` agree at startup, and run one sandbox order/capture/webhook cycle.

### B8 — No MCP runtime integration (medium)

**Evidence:** zero MCP references in backend; no MCP config in OpenCode.

**Why it blocks:** if MCP is a product requirement, it is entirely unbuilt. If it is only a research channel, this is not a blocker; the current architecture correctly prefers REST for runtime.

**Minimum fix:** make an explicit architectural decision. Do not add MCP to the runtime merely because it is reachable.

### B9 — KERNEL unavailable (medium)

**Evidence:** 401; no account.

**Why it blocks:** any KERNEL-dependent feature (remote browser execution, etc.) cannot be built or tested.

**Minimum fix:** obtain an account/token or formally drop KERNEL from the roadmap.

### B10 — No frontend (medium)

**Evidence:** `frontend/` is empty; `/` 404.

**Why it blocks:** there is no user-facing demo, and no way to exercise the approval UX in a browser.

**Minimum fix:** build the minimal Search → Offer comparison → Quote review → Approve flow described in Milestone 1, with explicit sandbox/MOCK banners.

---

## 14. Recommended Implementation Order

This is a recommendation only. Nothing below has been implemented.

### Phase 0 — Stop the bleeding (do first)

1. Fix naive/aware datetime handling at the persistence boundary.
2. Fix `_change_payload` serialisation.
3. Fix `add_capture` to use a savepoint, not a full rollback.
4. Add regression tests for all three.
5. Add a `/health` alias or update the documentation to `/api/v1/health`.

**Exit criterion:** an approval can be created, revalidated, priced, ordered with a fake gateway, captured once, replayed safely, and invalidated on price change — all on shipped SQLite with no shims.

### Phase 1 — Make the core trustworthy

1. Build the real test suite (unit + integration) and run it in CI.
2. Add Alembic with a baseline revision.
3. Add the Postgres driver and a Postgres test job.
4. Decide and enforce append-only semantics for `quotes` and `evidence`.
5. Re-verify `evidence.payload_sha256` on read (or document that it is write-only).

**Exit criterion:** schema can be created from zero by migration on SQLite and Postgres; tests cover the Guard matrix, snapshots, approval lifecycle, payment idempotency, journal chain, and cross-user isolation.

### Phase 2 — Close workflow and security gaps

1. Enforce a quote/intent state machine: a rejected quote cannot be approved without a fresh quote or an explicit transition.
2. Add duplicate and expiry semantics for purchase intents.
3. Replace `X-User-Id` with real authentication, or bind the service to loopback and document it as a local-only demo.
4. Add rate limiting and a CORS decision.
5. Make the PayPal environment and base URL consistent at startup; require the webhook id when credentials are present.

**Exit criterion:** no unauthenticated cross-user action; no silent re-approval; configuration cannot silently mix sandbox and live.

### Phase 3 — Verify the product source

1. Obtain a Channel3 key (or make an explicit decision to prototype on MCP).
2. Verify REST request/response against live data.
3. Add pagination, retry/backoff and `max_price` filtering at the provider where supported.
4. Fix data-mode provenance so a quote’s label comes from its offers/evidence, not from current settings.
5. If MCP is chosen, implement a proper adapter with timeouts, per-user sessions, and no shared `thread_id`.

**Exit criterion:** a live search produces real offers, every offer has a verifiable evidence row, and the quote accurately labels its data source.

### Phase 4 — Verify payments

1. Create a PayPal sandbox app and set credentials plus webhook id.
2. Run one sandbox order → approval → capture → webhook cycle.
3. Test duplicate capture, amount mismatch, expired approval, and stale price.
4. Decide how webhook-captured payments reconcile with the `captures` table.
5. Add retries/backoff and a reconciliation job.

**Exit criterion:** sandbox payment works end to end, webhooks are verified and deduplicated, and the retailer-purchase disclaimer is present on every response.

### Phase 5 — Add AI, if still wanted

1. Define one narrow advisory use case (for example, parse a natural-language request into search filters).
2. Validate the model response into a typed Pydantic model.
3. Keep the result clearly labelled `AI_UNVERIFIED`.
4. Add prompt-injection tests using the existing `security/untrusted.py` ideas.
5. Never pass AI output to Guard, approval or payment.

**Exit criterion:** AI can be disabled with one setting; the deterministic core behaves identically with AI on or off.

### Phase 6 — Product surface and operations

1. Build the minimal frontend.
2. Add structured logging, metrics and a runbook.
3. Add CI, dependency pinning checks and a release checklist.
4. Decide whether KERNEL is in scope; if not, remove it from the roadmap.

**Exit criterion:** a new developer can clone, migrate, run, test and demo the product from the README.

---

## 15. Final Verdict

**How much of Smartcart is genuinely implemented and working?**

The **deterministic, credential-free core is real and substantially works**: product/offer models, cost maths, a 17-rule Guard, snapshot hashing, approval binding, a hash-chained append-only journal, mock discovery, and an honest payment/retailer distinction. That is the strongest part of the project, and it is worth keeping.

But **as shipped, the approval → revalidation → payment path does not complete on SQLite**. The datetime bug blocks every revalidation, and the Decimal bug blocks price-change invalidation. These are not edge cases; they are on the main path.

Everything beyond the local core is either unverified or absent:

- **Channel3 REST**: code exists, live path blocked by a missing key.
- **Channel3 MCP**: reachable and returns real data, but **not configured in OpenCode and not used by the backend**.
- **KERNEL**: blocked by 401; no integration.
- **PayPal**: contract-tested with mocks, live/sandbox blocked by credentials and a public webhook URL.
- **Gemini**: adapter exists, never called; live blocked by a key and a data-policy decision.
- **Tests, migrations, Postgres, auth, frontend**: absent.

**The most important things to fix before continuing development:**

1. **Fix the SQLite datetime comparison** — it blocks the entire payment path.
2. **Fix Decimal journal serialisation** — price-change invalidation currently crashes.
3. **Fix `add_capture` rollback scope** — a duplicate must not erase audit state.
4. **Add a real test suite** — the two critical bugs would have been caught immediately.
5. **Add migrations and a Postgres story** before the schema grows.
6. **Add authentication** before exposing the API beyond localhost.
7. **Do not wire MCP into the runtime or enable live payments** until the above are done; verify Channel3 REST and PayPal sandbox with real credentials first.

In one sentence: **Smartcart is a promising local deterministic prototype with a sound safety architecture, but it is not yet a working end-to-end purchasing agent, and its main payment path is broken in the default configuration.**
