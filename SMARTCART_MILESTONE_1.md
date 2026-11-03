# SMARTCART — MILESTONE 1

Architecture finalisation + deterministic core implementation.
Tags: **VERIFIED** = executed/observed here · **DOCUMENTED** = vendor docs only · **UNVERIFIED** = assumed, not proven · **BLOCKED** = cannot proceed without a credential/decision.

Milestone 0 (`SMARTCART_MILESTONE_0.md`) is unchanged and remains the reconnaissance record.

---

## 1. Scope of this milestone

Build the parts of Smartcart that do not need external credentials, and leave thin, tested
adapters for the parts that do.

In scope: data model, evidence/snapshot engine, Purchase Guard, approval workflow,
append-only journal, API, minimal demo UI, PayPal/Channel3/Gemini adapters.

Out of scope: KERNEL runtime, hosted deployment, real money, local LLM, browser automation.

---

## 2. Architecture

### 2.1 Backend — modular monolith, hexagonal edges

```
frontend/ (no-build SPA, served by FastAPI)
        │  JSON over /api/v1
        ▼
api/            HTTP schemas + routes + webhooks        (I/O edge)
application/    use cases: discovery, quote, approval, payment
domain/         PURE: money, models, snapshot, guard    (no I/O, no clock, no LLM)
ports/          Protocol definitions                    (boundary contracts)
adapters/       channel3_rest, mock source, gemini, paypal, clock
persistence/    SQLAlchemy 2.x tables + repositories
journal/        append-only, hash-chained audit log
security/       redaction, idempotency, untrusted-input helpers
```

Three rules that are enforced by the code layout, not by discipline:

1. `domain/` imports nothing from `adapters/`, `persistence/`, `api/`, or any SDK.
2. Payment amounts are read only from a persisted `Quote`; the API never accepts a price or total.
3. Every guard evaluation is a pure function of `(quote, policy, limits, context)`.

### 2.2 Frontend

Deliberately **no build step**: static `frontend/index.html` + ES modules + CSS, served by FastAPI.
Reasons: ~1.2 GB free RAM on this machine, no Node toolchain in the demo loop, no bundler
process competing with the API. React/Vite can replace it later without touching the API.

Screens: Search → Offer comparison → Quote review (snapshot hash + guard rules) →
Explicit approval → Payment (sandbox, disabled without credentials) → Journal.

`data_mode` (`MOCK` / `LIVE`) is returned by the API and rendered as a badge on every result.
A sandbox banner is rendered from `paypal_env` when the PayPal order flow is available.

---

## 3. Database entities and relationships

SQLite locally (`sqlite+pysqlite`, WAL, FK on); Postgres-compatible column types
(`String`, `Numeric(12,2)`, `JSON`, `DateTime(timezone=True)`). `user_id` is present on every
user-owned row from day one; all repository reads are scoped by `user_id`.

```
users 1─┬─< products 1─< offers
        ├─< evidence
        ├─< quotes 1─< approvals 1─< payment_orders 1─< captures
        ├─< purchase_intents
        ├─< transaction_events
        └─< journal_events

quotes      immutable basket snapshot; stores snapshot_hash + policy_version + guard decision
approvals   snapshot_hash + status + expiry; superseded on re-approval
captures    UNIQUE(quote_id, snapshot_hash) → database-level duplicate-purchase defence
```

| Table | Purpose | Mutability |
|---|---|---|
| `users` | demo/single-user identity, `user_id` anchor | mutable |
| `products` | canonical product identity per `(source, source_product_id)` | upsert |
| `offers` | latest observed offer per `(source, source_offer_id)`; merchant, price, currency, condition, availability | upsert (history in `evidence`) |
| `evidence` | raw provider payload + SHA-256 + observation timestamps + trust class | **append-only** |
| `quotes` | immutable priced basket, totals, snapshot hash, policy version, guard decision | insert-only |
| `purchase_intents` | user intent lifecycle, links quote → approval → payment | state machine |
| `approvals` | explicit user approval bound to `snapshot_hash`; TTL; consumed flag | status transitions only |
| `payment_orders` | PayPal order mirror (provider id, amount, status, environment) | status transitions |
| `captures` | successful provider captures; unique per quote+snapshot | insert-only |
| `transaction_events` | verified external/provider facts (webhooks), separate from app journal | append-only |
| `idempotency_keys` | outbound-call idempotency (`key`, `fingerprint`, `status`) | insert/transition |
| `journal_events` | internal audit log, hash-chained (`prev_hash` → `entry_hash`) | **append-only (DB triggers)** |

Evidence rows are append-only via SQLite triggers (`no UPDATE`, `no DELETE`), mirrored by the
journal. Anything AI-generated is stored with `kind=AI_STATEMENT` and
`trust=AI_UNVERIFIED` and is **excluded** from the fact queries the Guard depends on.

---

## 4. API boundaries

All endpoints are under `/api/v1`, authenticated by `X-User-Id` (demo header; a real identity
provider is a later swap). Request bodies cannot contain prices, totals, or snapshot hashes
that the server trusts blindly.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + configured integrations (no secrets) |
| POST | `/search` | source search → normalized products/offers + evidence ids; returns `data_mode` |
| GET | `/offers` | offers for a product, newest observation |
| POST | `/quotes` | build quote from `offer_id` + `quantity`; server prices it |
| GET | `/quotes/{id}` | quote, snapshot hash, guard decision |
| POST | `/quotes/{id}/approve` | explicit approval; body: `{snapshot_hash, explicit_consent, acknowledged_risks}` |
| POST | `/quotes/{id}/reject` | explicit rejection |
| POST | `/approvals/{id}/revalidate` | freshness/policy/price re-check; may mark `STALE`/`EXPIRED` |
| POST | `/payments/orders` | create PayPal order from an ALLOW-ed approval (503 if unconfigured) |
| POST | `/payments/orders/{id}/capture` | verify then capture (server-side amount only) |
| POST | `/webhooks/paypal` | signature-verified webhook, idempotent by event id |
| GET | `/journal` | audit trail, read-only |

Errors are typed (`SNAPSHOT_MISMATCH`, `APPROVAL_REQUIRED`, `APPROVAL_EXPIRED`,
`GUARD_DENIED`, `DUPLICATE_CAPTURE`, `PAYPAL_NOT_CONFIGURED`, `IDEMPOTENCY_CONFLICT`).

---

## 5. Product sourcing integration

- **Preferred:** Channel3 REST (`POST /v1/search`, `x-api-key`) — **BLOCKED**: no API key;
  unauthenticated returns 403. Adapter implemented against the documented shape and covered by
  contract tests using sanitized fixtures; live response parsing is **UNVERIFIED**.
- **Fallback/supplementary:** Channel3 MCP (anonymous, verified in Milestone 0). Not a runtime
  dependency of the MVP; session `thread_id` must be per-user if added.
- **Local default:** deterministic mock source, clearly labelled `data_mode=MOCK`,
  `source=mock`, using the real Channel3 payload shape so normalization is exercised.

Normalization rules: price exists only on offers; currency retained per offer; `condition: null`
→ `UNKNOWN` (never "new"); `brands` may contain retailers; unknown availability → `UNKNOWN`;
both `offer.url` (redirector) and `redirect_url` are preserved; destination is **UNVERIFIED**.
Malformed results are skipped with a recorded warning, never fabricated.

---

## 6. AI integration boundary

- Provider is configurable (`SMARTCART_AI_PROVIDER=none|gemini`); **default `none`**, and the
  entire core runs and tests without an API key.
- Gemini adapter uses the Interactions API with a JSON-Schema structured output and validates
  the response into typed models. It returns *advisory* data only.
- The LLM never receives the Guard, policy objects, payment gateway, or credentials, and its
  output cannot create/approve/capture anything.
- Product text is passed as delimited untrusted data, never as instructions.
- Mock LLM provider is labelled `MOCK` in responses; no key is required for tests.
- **BLOCKED** for live use: no Gemini key / no accepted data policy.

---

## 7. Evidence and snapshots

`canonical_json` = sorted keys, no whitespace, Decimals as fixed 2-dp strings, datetimes UTC
second precision. `snapshot_hash = "sha256:" + sha256(canonical_json(material_projection))`.

The material projection contains only what changes what is bought: user, currency, requirement
version, policy version, per-line product/variant/offer/merchant/unit price/shipping/quantity/
condition/availability/`as_of`, totals, and evidence ids. Quote row ids are excluded so the
hash describes the purchase, not the row.

`diff_material(old, new)` produces field-level changes (path, before, after) used for
"price changed after approval" and for showing the user exactly what moved.

Every observation creates an immutable evidence row with payload hash; every quote links the
evidence ids it was priced from. An AI statement is evidence of *what the model said*, never
evidence of a product fact.

---

## 8. Purchase Guard

Pure engine: `evaluate(quote, policy, limits, context) -> GuardDecision`.
No I/O, no clock (time injected), no LLM, no mutable global state.
Any `FAIL` → `DENY`; any `INSUFFICIENT` → `NEEDS_INFO`; else `ALLOW`.

| Rule | Check |
|---|---|
| `G-001_CURRENCY` | one currency across all lines, quote and policy |
| `G-002_ITEM_CEILING` | unit price ≤ min(user limit, policy ceiling) |
| `G-003_TOTAL_CEILING` | grand total ≤ min(user limit, policy ceiling, requirement budget) |
| `G-004_QUANTITY` | `0 < qty ≤ min(user, policy, requirement)` |
| `G-005_AVAILABILITY` | `InStock`; `UNKNOWN` → NEEDS_INFO |
| `G-006_FRESHNESS` | `now − as_of ≤ max_evidence_age`; future skew > 120 s → FAIL |
| `G-007_CONDITION` | unknown → NEEDS_INFO; not in allowed set → FAIL |
| `G-008_MERCHANT_POLICY` | denylist reject; allowlist enforced when set |
| `G-009_EVIDENCE_COMPLETE` | every line carries evidence ids |
| `G-010_VARIANT_MATCH` | line product/variant identity matches the observed offer |
| `G-011_SNAPSHOT_MATCH` | recomputed hash equals stored hash (and approval hash at capture) |
| `G-012_NO_DUPLICATE` | no existing capture for `(quote, snapshot)` |
| `G-013_AMOUNT_MATCH` | provider/payment amount equals quote total to the cent |
| `G-014_ENV_CONSISTENCY` | sandbox order never captured as live, and vice versa |
| `G-015_POLICY_VERSION` | quote policy version == active policy version |
| `G-016_APPROVAL_VALID` | at capture: approved, unexpired, unconsumed, same quote/snapshot |
| `G-017_ARITHMETIC` | stored line/total arithmetic recomputed exactly |

Policy is versioned (`1.0.0`), stored on every quote; bumping it invalidates older approvals
by design. User limits can only tighten policy, never loosen it.

---

## 9. Approval workflow

1. `POST /quotes` — server prices the basket from persisted offers, creates the snapshot,
   evaluates the Guard, records `QUOTE_PREPARED` (actor `system`).
2. `POST /quotes/{id}/approve` — requires `explicit_consent=true`; recomputes the snapshot;
   refuses on mismatch, DENY, or NEEDS_INFO; supersedes prior approvals for the quote;
   creates an approval bound to `snapshot_hash` with TTL capped by evidence freshness.
3. `POST /approvals/{id}/revalidate` — compares the quote against current offer rows and the
   active policy; any material delta → `STALE`, expiry → `EXPIRED`, valid → `ALLOW`.
4. Payment creation and capture call revalidation first; capture also re-fetches the provider
   order (status must be `APPROVED`) and compares amounts server-side.
5. Any change to price/currency/quantity/merchant/condition/availability/totals/evidence
   requires a fresh approval. There is no "approve once, buy anything" path.

AI output cannot call any of these endpoints with authority: approval is a user action and all
checks are backend-side.

---

## 10. Transaction journal

Append-only, hash-chained (`prev_hash` + canonical payload → `entry_hash`), ordered by
monotonic `seq`. SQLite triggers reject `UPDATE`/`DELETE`; a verification helper recomputes the
chain. Recorded events include discovery, offer observation, evidence creation, quote creation,
guard evaluation, intent creation, approval/rejection/supersession, revalidation, staleness,
approval expiry, payment order creation, capture, webhook receipt, failures and recovery.
Every row has `actor` (`user|system|ai|external`), `environment`, optional `correlation_id`,
and a JSON payload that never contains secrets (redaction runs before insert).

Provider facts live in `transaction_events` (with `signature_verified` where applicable), so
"the app says" and "PayPal says" never blur into one record.

---

## 11. PayPal integration role

Legitimate use: **Smartcart collects the user's payment for Smartcart's own service? No** —
Smartcart is free, so there is no such payment. The only honest PayPal flow is a demo of the
*payment rail* for a basket the user explicitly approves, where Smartcart is the payee and the
retailer purchase remains the user's action.

Implemented, credential-independent:
- OAuth token acquisition, order create (`POST /v2/checkout/orders`), get, capture
  (`/capture`), `PayPal-Request-Id` idempotency, `links[rel=approve]` extraction,
  webhook signature verification (`/v1/notifications/verify-webhook-signature`),
  status mapping including `PAYER_ACTION_REQUIRED` and `APPROVED`.
- Capture is refused unless the freshly fetched order is `APPROVED` and amount/currency match.

**VERIFIED** (Milestone 0): unauthenticated order creation → 401; sandbox OAuth with fake
credentials → 401 `invalid_client`. Therefore live/sandbox verification is **BLOCKED**:
no `client_id`/`client_secret`, no public HTTPS URL for webhooks. No fake checkout is exposed;
`/payments/orders` returns `503 PAYPAL_NOT_CONFIGURED` until credentials exist.

**Crucial distinction:** a captured PayPal payment is `PAYMENT_STATE=CAPTURED` and
`RETAILER_PURCHASE_STATE=UNKNOWN`. Smartcart never says "purchased" without retailer
transaction evidence (`transaction_events` with retailer confirmation).

---

## 12. MCP integration status

| Server | Endpoint | Status |
|---|---|---|
| Channel3 | `https://mcp.trychannel3.com/` (Streamable HTTP) | **VERIFIED** anonymous handshake (v4.0.0, 6 tools); not configured in OpenCode and not required at runtime |
| KERNEL | `https://mcp.onkernel.com/mcp` | **BLOCKED** 401 `invalid_token`; no account; excluded from MVP |
| PayPal / Gemini | n/a (REST only) | no MCP dependency |

No MCP configuration file was created or modified. Existing `~/.config/opencode/service.json`
was left untouched. Channel3 REST remains the preferred production source once a key exists.

---

## 13. Dependency graph

```
api ──▶ application ──▶ domain (pure)
 │            │
 │            ├──▶ ports ◀── adapters (channel3, mock, gemini, paypal, clock)
 │            ├──▶ persistence ──▶ SQLAlchemy ──▶ SQLite/Postgres
 │            └──▶ journal ──▶ persistence
 └──▶ security (redaction, idempotency, untrusted)

Purchase Guard ◀── domain only.        AI ──▶ advisory text/typed proposals only.
PayPal adapter ◀── application/payment use case only (never AI, never frontend).
KERNEL — no edge in MVP; future adapter behind a port.
```

---

## 14. Remaining blockers

| ID | Blocker | Impact | Unblocks |
|---|---|---|---|
| B1 | No Channel3 API key | REST search 403; mock source used | live discovery |
| B3 | No Gemini key / data-policy decision | AI provider `none`; deterministic core unaffected | AI assistance |
| B4 | No PayPal sandbox app | payment endpoints return 503 | real sandbox order/capture |
| B5 | No public HTTPS URL | webhooks cannot be delivered | webhook reconciliation (unit-tested only) |
| B6 | Durable DB host undecided | local SQLite only | hosted demo persistence |

---

## 15. Implementation status

Updated after execution; see `README.md` for run commands.

| Component | Status | Evidence |
|---|---|---|
| Data models + migrations | implemented | `backend/app/persistence/tables.py`, `backend/migrations/` |
| Evidence + snapshot engine | implemented | `backend/app/domain/snapshot.py`, `backend/tests/unit/test_snapshot.py` |
| Purchase Guard | implemented | `backend/app/domain/guard/*`, `backend/tests/unit/test_guard.py` |
| Approval workflow | implemented | `backend/app/application/approvals.py`, `backend/tests/integration/test_approval_flow.py` |
| Transaction journal | implemented | `backend/app/journal/*`, `backend/tests/integration/test_journal.py` |
| API + demo UI | implemented | `backend/app/api/*`, `frontend/` |
| Channel3 adapter | implemented, live **UNVERIFIED** | fixture contract tests; no key |
| Gemini adapter | implemented, live **UNVERIFIED** | no key; provider default `none` |
| PayPal adapter | implemented, live **BLOCKED** | no credentials; 503 path tested |
| KERNEL | excluded | no runtime dependency |

Test results are reported in the chat after the run; this document does not claim tests that
were not executed.
