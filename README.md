# SmartCart

**An evidence-first AI shopping agent with a deterministic purchase guard.**

SmartCart turns a natural-language shopping request into explicit, user-confirmed
criteria, discovers offers, verifies product claims against provider data and
targeted read-only page research, and recommends only products that pass
deterministic checks. AI is advisory. A versioned Purchase Guard, explicit user
approval, and server-side revalidation remain the only authority over purchasing.

> SmartCart does **not** autonomously buy from retailers. It includes a PayPal
> sandbox payment-rail demo, but a captured PayPal payment is not a retailer
> purchase, and SmartCart has no retailer checkout integration.

---

## Table of contents

- [Overview](#overview)
- [Problem statement and motivation](#problem-statement-and-motivation)
- [Core features and capabilities](#core-features-and-capabilities)
- [How SmartCart works](#how-smartcart-works)
- [Architecture](#architecture)
- [Technology stack](#technology-stack)
- [Repository structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Installation and setup](#installation-and-setup)
- [Environment variable configuration](#environment-variable-configuration)
- [Starting the backend and frontend](#starting-the-backend-and-frontend)
- [Running tests](#running-tests)
- [Security and purchase-safety principles](#security-and-purchase-safety-principles)
- [Current implementation status](#current-implementation-status)
- [Known limitations](#known-limitations)
- [Future development roadmap](#future-development-roadmap)
- [Contributing and license](#contributing-and-license)
- [Documentation](#documentation)

---

## Overview

Online shopping agents are useful precisely when they can act on incomplete,
noisy product data. That is also when they are most dangerous: an LLM can
misread a specification, a marketplace page can contain prompt-injection text,
and a confident recommendation can hide the fact that a mandatory requirement
was never verified.

SmartCart is built around a different contract:

- **AI extracts and explains; it never decides.**
- **Every product claim is evidence-tagged.**
- **Unknown mandatory information is never presented as verified.**
- **No payment can move without a valid approval bound to an immutable quote
  snapshot.**
- **Every decision and state change is written to an append-only, hash-chained
  journal.**

The project is a working local prototype of that design. The deterministic
purchase core runs without any external credentials; Gemini, Channel3, and
KERNEL add live understanding, discovery, and research when configured.

---

## Problem statement and motivation

A typical "AI shopping assistant" demo looks like this:

1. Ask an LLM for a product recommendation.
2. Show the answer.
3. Add a checkout button.

That pattern fails in the ways that matter:

| Risk | Why it matters | SmartCart response |
| --- | --- | --- |
| Hallucinated product facts | The model may invent a specification, price, or availability | AI statements are stored as `AI_UNVERIFIED` and excluded from fact queries |
| Prompt injection in page content | A product page can contain text that looks like instructions | Page text is treated as inert data; it cannot become an instruction or action |
| Unverified mandatory requirements | "Noise cancelling" may never have been confirmed | Missing or conflicting mandatory evidence blocks certification |
| Silent budget or currency drift | A cheap-looking offer may not be the real price | Budget and currency are hard, deterministic checks |
| AI-controlled payments | A model must never be able to approve or capture money | The LLM has no access to the guard, approval, or payment paths |
| Irreversible actions | "Buy it" should not mean "buy anything the model finds" | Approval is bound to a quote snapshot with a TTL and revalidation |
| No audit trail | Disputes need proof of what was observed and decided | Evidence, journal entries, and captures are append-only |

SmartCart explores a more defensible model: **separate the language model from
the authority to transact**, make evidence explicit, and require a human
decision at the point where money moves.

---

## Core features and capabilities

### Shopping intelligence

- **Natural-language understanding** — Gemini converts a request into structured
  requirements, preferences, budget, currency, exclusions, and clarification
  questions when information is genuinely ambiguous.
- **User-confirmed criteria** — The user can correct the model's reading before
  any search runs. The confirmed criteria are authoritative; the model does not
  get a second chance to reinterpret the request silently.
- **Offer discovery** — Channel3 REST discovery when configured, or a
  deterministic mock source for credential-free development. Every offer keeps
  its source, observation time, and evidence identifier.
- **Deterministic evidence evaluation** — Claims are classified as `VERIFIED`,
  `UNVERIFIED`, `MISSING`, `CONFLICTING`, or `AI_ASSESSMENT`. Product decisions
  are `COMPLIANT`, `DISQUALIFIED`, or `INSUFFICIENT_EVIDENCE`.
- **Targeted read-only research** — When a purchase-decision evidence gap exists,
  KERNEL is used to visit the exact offer page, read its title and visible text,
  and extract only the fields under investigation. Research is bounded, not a
  general browsing loop.
- **Transparent recommendations** — `cheapest_compliant`, `best_value`, and
  `best_match` are selected deterministically from compliant products. Categories
  are omitted rather than filled with an ineligible product.
- **Advisory explanations** — Gemini can explain fixed outcomes in prose, with a
  deterministic fallback if the AI call fails.

### Purchase safety and audit

- **17-rule Purchase Guard** — Pure, deterministic checks for currency, item and
  total ceilings, quantity, availability, freshness, condition, merchant policy,
  evidence completeness, variant match, snapshot match, duplicate capture,
  amount match, environment consistency, policy version, approval validity, and
  arithmetic.
- **Snapshot-bound approval** — An approval is tied to a SHA-256 hash of the
  material purchase projection. Any material change requires a new approval.
- **Revalidation before money moves** — Payment creation and capture re-check the
  quote, approval, policy, amount, and provider state.
- **PayPal sandbox payment-rail demo** — Order creation, capture, idempotency
  keys, and webhook signature verification are implemented. This is a demo
  payment to SmartCart, not a retailer purchase.
- **Append-only audit trail** — Evidence, journal events, transaction events, and
  captures are protected by SQLite triggers; the journal is hash-chained and
  verifiable through `GET /api/v1/journal` and `GET /api/v1/health`.
- **Hermetic test suite** — 243 tests run against isolated SQLite databases and
  fake external services. No live credential is required.

### Credential-free development

With no credentials, the default setup uses the mock product source and leaves
AI and PayPal disabled. The deterministic search, quote, guard, approval,
journal, and health paths work immediately. Live integrations are opt-in.

---

## How SmartCart works

### Shopping workflow

```
Natural-language request
        │
        ▼
POST /api/v1/shopping/understand
        │  Gemini extracts structured understanding (advisory)
        ▼
POST /api/v1/shopping/confirm
        │  User confirms or corrects the reading
        │  Confirmed criteria are authoritative
        ▼
POST /api/v1/shopping/search
        │
        ├─ Channel3 (or mock source) discovery
        ├─ Deterministic evaluation of provider evidence
        ├─ Research decision: is there a purchase-decision evidence gap?
        ├─ KERNEL session: create → visit exact offer URL →
        │  read title/visible text → delete session
        ├─ Findings persisted as evidence with source URL and timestamp
        ├─ Re-evaluation using provider + researched claims
        ├─ Deterministic recommendations
        └─ Gemini explanation of fixed outcomes (advisory prose only)
        ▼
GET  /api/v1/shopping/searches/{search_id}
POST /api/v1/shopping/searches/{search_id}/research
```

The research endpoint is not browser control. It accepts only optional product
IDs that already belong to the stored search, applies the same deterministic
research decision, and returns the re-evaluated result. No arbitrary URL,
action, or credential can be submitted.

### Purchase workflow

```
Offers
  │
  ▼
POST /api/v1/quotes              server prices the basket from persisted offers
  │                              creates an immutable snapshot hash
  │                              evaluates the 17-rule guard
  ▼
Guard outcome
  ├─ DENY        → quote cannot be approved
  ├─ NEEDS_INFO  → resolve blocking checks first
  └─ ALLOW       → user may review evidence
        │
        ▼
POST /api/v1/quotes/{id}/approve
  │  explicit_consent = true
  │  approval bound to snapshot hash + TTL
  ▼
POST /api/v1/approvals/{id}/revalidate
  │  compare current offers and active policy
  ▼
POST /api/v1/payments/orders
  │  revalidate, then create PayPal order (server-side amount only)
  ▼
POST /api/v1/payments/orders/{id}/capture
  │  re-fetch provider order, verify APPROVED status and amount,
  │  check duplicate-capture protection, then capture
  ▼
PAYMENT_STATE = CAPTURED
RETAILER_PURCHASE_STATE = UNKNOWN
```

A captured PayPal payment proves that money moved to the PayPal payee. It does
not prove that a retailer shipped anything. Only retailer evidence could promote
the retailer purchase state, and that integration is not implemented.

### Key design rules

1. The `domain/` package is pure: no I/O, no clock, no LLM, no third-party SDKs.
2. Payment amounts are read from a persisted quote; the API never accepts a
   client-supplied price or total.
3. Guard evaluation is a pure function of quote, policy, limits, and context.
4. AI output is evidence of what the model said, never evidence of a product
   fact.
5. Unknown mandatory information remains unknown. It is not rounded up to
   compliance.
6. Money is handled as `Decimal`, not `float`.

---

## Architecture

SmartCart is a modular monolith with hexagonal edges.

```
┌────────────────────────────────────────────────────────────────────┐
│                         HTTP / FastAPI                            │
│  api/  routes, schemas, dependencies, PayPal webhook              │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│                     Application use cases                           │
│  discovery, quotes, approvals, payments, shopping orchestration    │
└───────────────┬──────────────────────────────┬─────────────────────┘
                │                              │
┌───────────────▼──────────────┐  ┌────────────▼─────────────────────┐
│      Pure domain             │  │  Ports and adapters              │
│  money, models, snapshots,   │  │  Channel3, mock source, Gemini,  │
│  Purchase Guard rules        │  │  KERNEL, PayPal, clock           │
└───────────────┬──────────────┘  └────────────┬─────────────────────┘
                │                              │
┌───────────────▼──────────────┐  ┌────────────▼─────────────────────┐
│      Persistence             │  │  Journal and security            │
│  SQLAlchemy tables + repos   │  │  hash chain, redaction,          │
│  SQLite by default           │  │  idempotency, untrusted input    │
└──────────────────────────────┘  └──────────────────────────────────┘
```

### Layer responsibilities

| Layer | Responsibility |
| --- | --- |
| `app/api/` | FastAPI routes, request/response schemas, dependencies, error mapping, PayPal webhook |
| `app/application/` | Use cases: discovery, quote preparation, approval, revalidation, payments, shopping orchestration |
| `app/domain/` | Pure models, money handling, canonical snapshots, Purchase Guard rules and policy |
| `app/ports/` | Protocol definitions for product sources, LLM, KERNEL, and payment gateway |
| `app/adapters/` | Concrete HTTP clients and local sources; normalization and clock |
| `app/persistence/` | SQLAlchemy 2.x tables, repositories, transaction/session setup |
| `app/journal/` | Append-only event writer and hash-chain verification |
| `app/security/` | Secret redaction, idempotency helpers, untrusted-content boundaries |
| `app/shopping/` | Understanding, deterministic evaluation, research decisions, interpretation, contracts |

### External integrations

| Integration | Role | Runtime requirement |
| --- | --- | --- |
| Channel3 REST | Product and offer discovery | `SMARTCART_PRODUCT_SOURCE=channel3` plus an API key |
| Gemini | Request understanding and advisory explanations | `SMARTCART_AI_PROVIDER` not `none` plus an API key |
| KERNEL | Read-only targeted product-page research | API key |
| PayPal | Sandbox payment-rail demo | Client ID, secret, and webhook ID for webhook verification |
| Mock source | Deterministic offline discovery | None |

MCP is **not** part of the application runtime. The backend uses REST adapters
directly.

---

## Technology stack

| Area | Technology | Version / notes |
| --- | --- | --- |
| Language | Python | Requires `>=3.12`; verified locally with `3.13.15` |
| Web framework | FastAPI | `0.121.2` |
| ASGI server | Uvicorn | `0.41.0` with `standard` extras |
| Data validation | Pydantic | `2.12.4` |
| Settings | pydantic-settings | `2.13.0` |
| ORM | SQLAlchemy | `2.0.44` |
| Database | SQLite | Default local database; schema uses portable SQLAlchemy types |
| Migrations | Alembic | `1.17.1` is a dependency, but no migration scripts are included; schema is created with `create_all` |
| HTTP client | httpx | `0.28.1` |
| Testing | pytest | `9.0.1` |
| Product discovery | Channel3 REST | Optional; mock source by default |
| AI | Google Gemini | `generateContent` REST API, JSON-schema structured output |
| Browser research | KERNEL REST | Read-only Playwright execution |
| Payments | PayPal REST | Sandbox order/capture/webhook flow |
| Frontend | None included | API-only repository; interactive OpenAPI docs at `/docs` |

No Node.js, bundler, or frontend build step is required.

---

## Repository structure

```
smartcart/
├── backend/
│   ├── app/
│   │   ├── api/                 # FastAPI routes, schemas, dependencies, webhook
│   │   ├── application/         # Discovery, quotes, approvals, payments, shopping
│   │   ├── adapters/            # Channel3, Gemini, KERNEL, PayPal, mock, clock
│   │   ├── domain/              # Pure models, money, snapshots, Purchase Guard
│   │   ├── journal/             # Hash-chained append-only audit log
│   │   ├── persistence/         # SQLAlchemy tables and repositories
│   │   ├── ports/               # Outbound protocol definitions
│   │   ├── security/            # Redaction, idempotency, untrusted content
│   │   ├── shopping/            # Understanding, evaluation, research, interpretation
│   │   ├── config.py            # Typed settings and secret handling
│   │   ├── container.py         # Composition root and FastAPI app factory
│   │   └── main.py              # Uvicorn entry point
│   ├── tests/                   # 18 test modules, 243 hermetic tests
│   └── pyproject.toml           # Dependencies and pytest configuration
├── .env.example                 # Placeholder environment template
├── .gitignore                   # Excludes secrets, databases, caches, build output
├── README.md
└── SMARTCART_*.md               # Milestone and audit reports
```

The local working copy may contain an empty `frontend/` directory. Empty
directories are not tracked by Git, and no frontend is published.

---

## Prerequisites

- **Python 3.12 or newer** (3.13 is verified locally).
- **Git**.
- A shell: PowerShell examples are provided below; macOS/Linux equivalents are
  straightforward.
- Optional external accounts and keys:
  - Channel3 API key for live product discovery.
  - Gemini API key for natural-language understanding and AI explanations.
  - KERNEL API key for targeted page research.
  - PayPal sandbox app for the payment-rail demo, including a webhook ID if you
    want webhook verification.

The deterministic core and the full test suite require none of the optional
credentials.

---

## Installation and setup

### 1. Clone the repository

```powershell
git clone https://github.com/smartakaninyene5-gif/smartcart.git
Set-Location smartcart
```

### 2. Create and activate a virtual environment

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

If PowerShell blocks activation, use the interpreter directly as shown in the
commands below instead of changing the execution policy.

### 3. Install the backend and development dependencies

From the repository root:

```powershell
python -m pip install -e "backend[dev]"
```

This installs the runtime dependencies plus pytest.

### 4. Create your local environment file

```powershell
Copy-Item .env.example .env
```

On macOS/Linux:

```bash
cp .env.example .env
```

Edit `.env` locally. **Never commit it.** The repository `.gitignore` excludes
`.env`, `.env.*`, keys, databases, logs, and caches.

### 5. Verify the environment

```powershell
python -m pip check
```

---

## Environment variable configuration

SmartCart reads settings from the process environment and from `.env` files. The
settings model looks for `.env` and `../.env`, so running commands from
`backend/` still finds the repository-root `.env`.

All documented settings use the `SMARTCART_` prefix. The Channel3, Gemini, and
KERNEL keys also accept their bare names (`CHANNEL3_API_KEY`, `GEMINI_API_KEY`,
`KERNEL_API_KEY`) as aliases.

Use placeholders locally. Never commit real values.

### Core settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMARTCART_ENV` | `dev` | Environment label: `dev`, `test`, `sandbox`, or `production` |
| `SMARTCART_DATABASE_URL` | `sqlite+pysqlite:///./smartcart.db` | SQLAlchemy database URL |
| `SMARTCART_DEMO_USER_ID` | `user_demo` | Fallback identity when `X-User-Id` is absent |

### Product discovery

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMARTCART_PRODUCT_SOURCE` | `mock` | `mock` or `channel3` |
| `SMARTCART_CHANNEL3_API_KEY` | empty | Channel3 API key; `CHANNEL3_API_KEY` is accepted as an alias |
| `SMARTCART_CHANNEL3_BASE_URL` | `https://api.trychannel3.com` | Channel3 REST base URL |
| `SMARTCART_CHANNEL3_TIMEOUT_SECONDS` | `20` | Discovery request timeout |

### AI

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMARTCART_AI_PROVIDER` | `auto` | `none`, `auto`, or `gemini`; `auto` enables Gemini when a key is present |
| `SMARTCART_GEMINI_API_KEY` | empty | Gemini API key; `GEMINI_API_KEY` is accepted as an alias |
| `SMARTCART_GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model name |
| `SMARTCART_GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com` | Gemini API base URL |
| `SMARTCART_GEMINI_TIMEOUT_SECONDS` | `60` | Gemini request timeout |

### KERNEL research

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMARTCART_KERNEL_API_KEY` | empty | KERNEL API key; `KERNEL_API_KEY` is accepted as an alias |
| `SMARTCART_KERNEL_BASE_URL` | `https://api.onkernel.com` | KERNEL REST base URL |
| `SMARTCART_KERNEL_TIMEOUT_SECONDS` | `90` | KERNEL request timeout |
| `SMARTCART_MAX_RESEARCH_PRODUCTS` | `3` | Maximum products researched per search |
| `SMARTCART_MAX_PAGE_TEXT_LENGTH` | `20000` | Visible page text clipped before extraction |

### PayPal

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMARTCART_PAYPAL_ENV` | `sandbox` | Environment label: `sandbox` or `live` |
| `SMARTCART_PAYPAL_CLIENT_ID` | empty | PayPal client ID |
| `SMARTCART_PAYPAL_CLIENT_SECRET` | empty | PayPal client secret |
| `SMARTCART_PAYPAL_BASE_URL` | `https://api-m.sandbox.paypal.com` | PayPal REST base URL; set explicitly when switching environments |
| `SMARTCART_PAYPAL_WEBHOOK_ID` | empty | Required for webhook signature verification |
| `SMARTCART_PAYPAL_RETURN_URL` | `http://localhost:8000/?paypal=return` | Return URL for the payment flow |
| `SMARTCART_PAYPAL_CANCEL_URL` | `http://localhost:8000/?paypal=cancel` | Cancel URL for the payment flow |

> `SMARTCART_PAYPAL_ENV` is a label. Changing it to `live` does not change
> `SMARTCART_PAYPAL_BASE_URL`; set both deliberately.

### Purchase Guard

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMARTCART_GUARD_POLICY_VERSION` | `1.0.0` | Version stored on every quote |
| `SMARTCART_GUARD_MAX_TOTAL` | `1000.00` | Global basket ceiling |
| `SMARTCART_GUARD_MAX_ITEM` | `500.00` | Global per-item ceiling |
| `SMARTCART_GUARD_MAX_QUANTITY` | `5` | Maximum quantity per line |
| `SMARTCART_GUARD_MAX_EVIDENCE_AGE_SECONDS` | `900` | Maximum evidence age |
| `SMARTCART_GUARD_APPROVAL_TTL_SECONDS` | `1800` | Approval time-to-live |
| `SMARTCART_GUARD_MERCHANT_ALLOWLIST` | empty | Comma-separated allowed merchant domains |
| `SMARTCART_GUARD_MERCHANT_DENYLIST` | empty | Comma-separated denied merchant domains |

User-supplied limits can only tighten policy limits, never loosen them.

### Example local `.env`

```dotenv
SMARTCART_ENV=dev
SMARTCART_DATABASE_URL=sqlite+pysqlite:///./smartcart.db
SMARTCART_DEMO_USER_ID=user_demo

# Start offline and credential-free
SMARTCART_PRODUCT_SOURCE=mock
SMARTCART_AI_PROVIDER=none

# Uncomment and fill only when you want live integrations
# SMARTCART_PRODUCT_SOURCE=channel3
# SMARTCART_CHANNEL3_API_KEY=<your-channel3-api-key>
# SMARTCART_AI_PROVIDER=auto
# SMARTCART_GEMINI_API_KEY=<your-gemini-api-key>
# SMARTCART_GEMINI_MODEL=gemini-2.5-flash
# SMARTCART_KERNEL_API_KEY=<your-kernel-api-key>

# Leave empty to keep the payment flow disabled
SMARTCART_PAYPAL_ENV=sandbox
SMARTCART_PAYPAL_CLIENT_ID=
SMARTCART_PAYPAL_CLIENT_SECRET=
SMARTCART_PAYPAL_WEBHOOK_ID=
```

---

## Starting the backend and frontend

### Backend

Run from the `backend/` directory so that the relative default SQLite path and
Python module resolution behave as documented:

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

If your virtual environment is activated, the equivalent is:

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Check health:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

The health response reports the environment, data mode, which integrations are
configured (booleans only), the guard policy version, and whether the journal
hash chain is intact. It never returns secrets.

Interactive API documentation is available at:

- `http://127.0.0.1:8000/docs` — Swagger UI
- `http://127.0.0.1:8000/redoc` — ReDoc

### Frontend

**No frontend is included in this repository.** There is no `index.html`, no
JavaScript application, and no Node build step. The published repository is an
API-only backend.

Use the Swagger UI at `/docs` for manual exploration. If a `frontend/` directory
is added later, the application factory will mount it at `/`; that behavior exists
in `container.py`, but no frontend is currently shipped.

### A minimal API check

The following works with the default mock source and no credentials:

```powershell
$search = Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/search `
  -ContentType "application/json" `
  -Body '{"query":"wireless headphones","limit":5,"currency":"USD"}'

$search.data_mode
$search.products.Count
```

The full shopping workflow (`/shopping/understand` → `/shopping/confirm` →
`/shopping/search`) requires Gemini. With `SMARTCART_AI_PROVIDER=none`,
`/shopping/understand` correctly returns `503 AI_NOT_CONFIGURED`; deterministic
search, quotes, guard evaluation, approval, and journaling still work.

A live-workflow example, once Gemini is configured:

```powershell
$u = Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/shopping/understand `
  -ContentType "application/json" `
  -Body '{"text":"I must have wireless noise cancelling headphones under $100"}'

$c = Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/shopping/confirm `
  -ContentType "application/json" `
  -Body (@{ request_id = $u.request_id; understanding = $u.understanding } | ConvertTo-Json -Depth 10)

Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/v1/shopping/search `
  -ContentType "application/json" `
  -Body (@{ request_id = $u.request_id; research = $true } | ConvertTo-Json)
```

Requests may include an `X-User-Id` header. Without one, the configured demo user
is used. This header is a lightweight demo identity convention, not production
authentication.

---

## Running tests

The test suite is hermetic: every test gets an isolated SQLite database, a fixed
clock, the mock product source, and fake external services. No live credential is
used.

From `backend/`:

```powershell
Set-Location backend
$env:PYTHONDONTWRITEBYTECODE="1"
..\.venv\Scripts\python.exe -m pytest -p no:cacheprovider
```

The pytest configuration already applies `-q` and sets `pythonpath = ["."]`.

**Current result:** 243 tests collected, covering 18 test modules. The latest
full run is **243 passed, 0 failed, 0 skipped**.

Test coverage includes:

- API contracts, validation, ownership boundaries, and error codes.
- Discovery adapters, normalization, fixtures, and Channel3 contract behavior.
- Quote arithmetic, snapshots, Decimal handling, and journal serialization.
- Purchase Guard rules, expiry, price changes, duplicates, and payment safety.
- Gemini adapter behavior, structured parsing, typed errors, and secret
  redaction.
- KERNEL URL safety, session cleanup, redirect handling, and read-only action
  guards.
- Shopping understanding, clarification, evidence evaluation, research
  decisions, recommendations, and full API flow.
- SQLite `BEGIN IMMEDIATE` transaction behavior.
- Purchase Guard regression checks for the shopping workflow.

Optional compile check:

```powershell
$env:PYTHONDONTWRITEBYTECODE="1"
..\.venv\Scripts\python.exe -m compileall -q app tests
```

---

## Security and purchase-safety principles

### AI is advisory, never authoritative

- Gemini never assigns a compliance status, product price, or recommendation
  category.
- The LLM does not receive the Purchase Guard, policy objects, payment gateway,
  or credentials.
- AI statements are stored with `kind=AI_STATEMENT` and
  `trust=AI_UNVERIFIED`; they are excluded from fact queries.
- The model cannot call approval, payment, or capture paths.

### Evidence is explicit and provenance-aware

- Every observed offer and researched claim has a source URL, timestamp, field,
  value, and trust level.
- `VERIFIED` requires direct structured provider data or a literal match on a
  visited page.
- `MISSING`, `UNVERIFIED`, and `CONFLICTING` mandatory evidence blocks
  certification rather than being guessed away.
- Conflicting claims are preserved and disclosed, not silently reconciled.

### KERNEL is read-only by construction

- URLs must be `http` or `https`, with no userinfo, localhost, private,
  link-local, or single-label hosts.
- Cross-host redirects to private hosts are refused and the page is discarded.
- The adapter emits only read-only browser operations: navigate, read URL, read
  title, read visible text.
- An action guard asserts that emitted JavaScript contains no fill, click, type,
  select, keyboard, or mouse actions.
- Sessions are deleted in a `finally` block.
- Page text is inert data. Prompt-injection strings can be returned as text but
  can never become instructions or actions.

### Money cannot move without explicit approval

- The API never accepts a client-supplied price or total.
- Quotes are priced from persisted offers and bound to an immutable snapshot
  hash.
- Approval requires `explicit_consent=true` and a matching snapshot.
- Revalidation runs before payment creation and capture.
- Approvals have a TTL and are superseded on re-approval.
- Captures are unique per quote and snapshot at the database level.
- Duplicate-capture and amount-mismatch checks are enforced server-side.

### Secrets and audit hygiene

- Settings use `SecretStr`; empty values are treated as unconfigured.
- Error messages and journal payloads redact credentials.
- `.env`, keys, databases, logs, and caches are ignored by Git.
- Evidence, journal events, transaction events, and captures are append-only,
  with SQLite triggers rejecting update and delete.
- The journal is hash-chained and verifiable.

### Payment scope disclosure

SmartCart's PayPal flow is a **payment-rail demo**. A captured payment is money
paid to SmartCart, not a purchase from a retailer. The application intentionally
reports `RETAILER_PURCHASE_STATE=UNKNOWN` until retailer evidence exists, and no
retailer checkout automation is implemented.

---

## Current implementation status

| Area | Status | Notes |
| --- | --- | --- |
| FastAPI backend and typed API | Implemented | `/api/v1`, OpenAPI docs, typed error mapping |
| Deterministic product discovery (mock) | Implemented | Default credential-free source; `data_mode=MOCK` is explicit |
| Channel3 REST discovery | Implemented, live adapter check recorded | Mock remains the default; live use requires an API key |
| Gemini understanding and explanations | Implemented, live adapter check recorded | Advisory only; no retry/backoff on transient 5xx |
| KERNEL targeted research | Implemented, live adapter check recorded | Read-only, bounded, session cleanup |
| Evidence evaluation and recommendations | Implemented | Deterministic; five evidence statuses; three recommendation categories |
| Purchase Guard | Implemented | 17 rules; any `FAIL` denies, any `INSUFFICIENT` needs info |
| Quote, approval, revalidation | Implemented | Snapshot-bound approvals with TTL and material-change detection |
| PayPal sandbox flow | Implemented, tested with fakes | Live sandbox order/capture/webhook cycle not verified in recorded checks |
| Append-only journal and evidence | Implemented | Hash chain, SQLite triggers, health verification |
| Automated tests | Implemented | 243 tests, 18 modules, hermetic |
| Frontend | Not implemented | Empty local directory; not tracked or published |
| Production authentication | Not implemented | `X-User-Id` is a demo convention |
| Alembic migrations | Not implemented | Alembic is a dependency, but schema is created with `create_all` |
| Deployment, CI, containers | Not included | No Dockerfile, CI workflow, or hosting configuration |
| MCP runtime integration | Not implemented | Integrations use REST adapters |
| Retailer checkout / purchase execution | Not implemented | Deliberately out of scope |

### Live integration evidence

The Milestone 2 report records adapter-level live checks:

- **Gemini** returned a valid structured understanding using the configured
  model. Earlier attempts hit transient HTTP 503 responses; the configured model
  was not changed.
- **Channel3** returned live products and offers with HTTPS URLs.
- **KERNEL** created a session, visited public pages, read title and text, and
  deleted the session.

These checks verify each adapter separately. The full three-service shopping
workflow was exercised with fakes in the automated suite, not end-to-end against
all live services at once.

---

## Known limitations

- **Gemini is required for `/shopping/understand`.** Without it, that endpoint
  returns `503 AI_NOT_CONFIGURED`. Deterministic search, quotes, guard checks,
  approvals, and journaling still work.
- **No automatic retry/backoff.** Transient Gemini 5xx responses map to
  `AI_UNAVAILABLE`; interpretation falls back to a deterministic explanation
  where possible.
- **KERNEL extraction is intentionally simple.** It uses literal and regex
  matching over visible text. It does not parse JSON-LD, screenshots, variant
  selectors, or JavaScript-rendered content added after the initial read.
  Unresolved fields remain `MISSING` or `UNKNOWN`.
- **Research is bounded.** At most three products are researched per search by
  default (`SMARTCART_MAX_RESEARCH_PRODUCTS`).
- **Unknown condition does not automatically block a product.** It is disclosed
  and triggers research, but only blocks if the user makes condition mandatory.
- **Channel3 normalization is tolerant.** Malformed offers are skipped with
  warnings; optional attributes depend on the provider payload.
- **PayPal live/sandbox verification is incomplete.** The flow is covered with
  fakes. A real sandbox order/capture/webhook cycle requires credentials, a
  webhook ID, and a public HTTPS endpoint. Changing `SMARTCART_PAYPAL_ENV` alone
  does not switch the base URL.
- **No production authentication or authorization.** `X-User-Id` is a demo
  identity header, not a security boundary.
- **No migration history.** Tables are created at startup with
  `Base.metadata.create_all`; Alembic migration scripts are absent.
- **SQLite is the only exercised database.** Column types are chosen to be
  portable, but PostgreSQL behavior is unverified.
- **No frontend.** The API is the only shipped interface.
- **No retailer purchase execution.** SmartCart does not add items to a retailer
  cart, check out, or confirm fulfillment.
- **No basket optimization.** The quote path prices an explicit selection of
  offers; it does not solve for an optimal basket across constraints.
- **No rate limiting, response caching, or cost controls** for Gemini, KERNEL, or
  Channel3.
- **No deployment or CI configuration.** The repository is a local prototype, not
  a hosted service.

---

## Future development roadmap

1. **Frontend for the evidence workflow** — understand → confirm → compare
   evidence → approve/reject, with clear `VERIFIED`, `UNVERIFIED`, `MISSING`, and
   `CONFLICTING` disclosure. No hidden "buy" action.
2. **Production identity and authorization** — replace `X-User-Id` with a real
   identity provider and enforce per-user access at every boundary.
3. **External-call resilience** — bounded retry/backoff with jitter for transient
   Gemini, KERNEL, Channel3, and PayPal failures, plus circuit-breaker behavior
   where appropriate.
4. **Richer research extraction** — structured data, variant and merchant
   resolution, per-field confidence, and explicit limitations.
5. **PayPal sandbox verification and webhook deployment** — run a complete
   sandbox order/capture/webhook cycle; validate environment and base URL
   consistency at startup; require the webhook ID when credentials are present.
6. **Schema migrations and deployment** — add Alembic migrations, a container
   image, CI, and a documented deployment target.
7. **Rate limiting, caching, and cost controls** — protect paid integrations and
   make latency/cost predictable.
8. **PostgreSQL verification** — exercise the schema and transaction behavior
   against PostgreSQL, not only SQLite.
9. **Channel3 attribute coverage** — expand normalization and add live
   end-to-end regression fixtures.
10. **Basket optimization** — optimize a multi-item basket under budget,
    availability, merchant, and evidence constraints without weakening the guard.

---

## Contributing and license

### Contributing

There is no formal `CONTRIBUTING.md` yet. If you want to contribute:

1. Open an issue describing the change or bug.
2. Keep the deterministic core and Purchase Guard invariants intact.
3. Add or update tests; do not weaken, skip, or delete existing tests.
4. Run the full suite from `backend/` before opening a pull request.
5. Never commit `.env`, credentials, tokens, databases, or logs.

### License

**No license file is currently included in this repository.** The code is
publicly visible on GitHub, but no open-source license is granted for reuse,
modification, or redistribution. If you intend to use or distribute this project,
add an explicit `LICENSE` file first and confirm the intended terms with the
project owner.

---

## Documentation

The repository includes detailed historical reports:

- [Milestone 0 — technical reconnaissance and architecture](SMARTCART_MILESTONE_0.md)
- [Milestone 1 — deterministic core and integrations](SMARTCART_MILESTONE_1.md)
- [Milestone 1.1 — final verification](SMARTCART_MILESTONE_1_1.md)
- [Milestone 2 — Gemini, evidence evaluation, and KERNEL research](SMARTCART_MILESTONE_2.md)
- [Implementation and integration audit](SMARTCART_IMPLEMENTATION_AUDIT.md)
- [Interrupted session recovery report](SMARTCART_INTERRUPTED_SESSION_RECOVERY.md)
- [Milestone 1.1 recovery report](SMARTCART_MILESTONE_1_1_RECOVERY.md)

These documents are historical records. Where they conflict with the current
code, the code and this README take precedence. In particular, the audit predates
Milestone 2 and therefore predates the verified Gemini, Channel3, and KERNEL
adapter-level checks.

---

## Project status

SmartCart is a **local prototype with a tested deterministic core**, not a
production shopping service. It demonstrates a credible safety architecture:
advisory AI, explicit evidence, deterministic purchasing rules, snapshot-bound
approval, and an auditable record. Production authentication, a frontend, live
PayPal verification, migrations, and deployment remain future work.
