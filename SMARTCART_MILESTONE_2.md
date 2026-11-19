# Smartcart — Milestone 2

**Status:** completed (implementation, tests and live adapter checks)
**Date:** 2026-11-05
**Scope:** Gemini request understanding / evidence interpretation, Channel3
discovery reuse, KERNEL targeted product-page research, deterministic evidence
evaluation, recommendations, shopping API workflow, automated tests and
documentation.

No commit, push, reset, rebase or amend was performed. All Milestone 2 changes
remain uncommitted for review. The root `.env` was not modified and no
credential, token, session id or CDP URL is recorded in this document.

---

## 1. Recovery point (where the interrupted run stopped)

The interrupted run had left the following uncommitted work:

| State at recovery | Detail |
| --- | --- |
| Present but unfinished | `backend/app/adapters/kernel_browser.py`, `backend/app/shopping/*`, `backend/app/application/shopping.py` (broken imports / dead findings holder) |
| Partially wired | `config.py`, `ports`, `adapters`, `container`, persistence tables/repositories, journal event names |
| Not yet present | API schemas/routes for shopping, any Milestone 2 tests, Milestone 2 documentation |
| Baseline | `compileall` exit 0, **104 passed** existing tests (hermetic) |

The unfinished orchestration file was replaced and completed (it was generated
code with broken imports, not authored user work). Everything else listed above
was reused and finished rather than rebuilt. Existing tests were neither
removed nor weakened.

---

## 2. Architecture

```
POST /shopping/understand
        │  Gemini (advisory) extracts structure + clarifications
        ▼
POST /shopping/confirm
        │  user-corrected understanding becomes ConfirmedCriteria (authoritative)
        ▼
POST /shopping/search
        │
        ├─ Channel3 (or mock source) discovery → Product + Offer + evidence rows
        ├─ deterministic evaluation → ProductAssessment (COMPLIANT /
        │     DISQUALIFIED / INSUFFICIENT_EVIDENCE)
        ├─ deterministic research decision → exact offer URL + fields
        ├─ KERNEL session: create → visit → read title/visible text → delete
        ├─ findings persisted as evidence rows (source URL + observed_at)
        ├─ re-evaluation with combined Channel3 + KERNEL claims
        ├─ deterministic recommendations (cheapest / best value / best match)
        └─ Gemini interpretation of fixed statuses (advisory prose only)
        ▼
GET /shopping/searches/{id}   (stored result + research provenance)
POST /shopping/searches/{id}/research   (internal, bounded re-research)
```

Key principle: **Gemini never assigns a status, price or decision.** It writes
structured understanding and advisory explanations. The deterministic evaluator
is the only authority for compliance and recommendations. Page content is
untrusted data; the KERNEL adapter only emits read-only browser JavaScript
(`goto`, `title`, `url`, `innerText`) and never fill/click/type/navigate to
account or checkout flows.

### Code map

| File | Responsibility |
| --- | --- |
| `app/shopping/models.py` | Pydantic contracts: requirements, confirmed criteria, claims, evidence statuses, assessments, research decisions/findings, explanations, recommendations |
| `app/shopping/understanding.py` | Gemini schema/prompt, parsing, deterministic clarifications, confirmation → criteria |
| `app/shopping/evaluation.py` | Deterministic claims → statuses, mandatory/preference checks, budget/currency/exclusion rules, offer selection, value scoring, ranking |
| `app/shopping/research.py` | Research-trigger matrix and literal page-text extraction |
| `app/shopping/interpretation.py` | Gemini interpretation prompt/schema/merge and deterministic fallback explanation |
| `app/adapters/gemini.py` | Gemini HTTP adapter with typed error taxonomy and redacted messages |
| `app/adapters/kernel_browser.py` | KERNEL REST client, URL safety validation, redirect checks, read-only action guard, cleanup |
| `app/application/shopping.py` | Orchestration, persistence, journaling, bounded research, recommendations |
| `app/persistence/repositories.py` | `ShoppingRepository` for requests, searches and research rows |
| `app/api/routes.py` | Five shopping endpoints + health fields |

---

## 3. Deterministic evaluation contract

Evidence statuses (assigned only by the backend):

| Status | Meaning |
| --- | --- |
| `VERIFIED` | All claims are directly observed structured provider data or a literal/substring match on a visited KERNEL page |
| `UNVERIFIED` | A statement exists but was not directly observed, or direct and AI claims are mixed |
| `MISSING` | No evidence at all (unresolved, never assumed) |
| `CONFLICTING` | Claims disagree; every claim is preserved and disclosed |
| `AI_ASSESSMENT` | Only AI statements; never usable as a fact |

Product decisions:

| Decision | Condition |
| --- | --- |
| `COMPLIANT` | No hard violation, no mandatory check failed/blocked, no price conflict |
| `DISQUALIFIED` | Verified hard violation: budget, currency, excluded brand/merchant, or a failed mandatory requirement |
| `INSUFFICIENT_EVIDENCE` | A mandatory requirement is MISSING/UNVERIFIED/CONFLICTING, or Channel3/KERNEL prices conflict |

Additional fixed rules:

* Unknown mandatory information can never be presented as verified compliance.
* Budget, currency mismatch and excluded brands/merchants are hard
  disqualifiers; currency is never silently relaxed.
* Preferences are recorded and can help ranking, but a missed preference never
  disqualifies a product.
* Offer-level facts (price, condition, availability, shipping, merchant) are
  evaluated only for the **selected purchasable offer**: in-stock offers are
  preferred, then cheapest, with `offer_id` as a stable tie-break. A different
  merchant's condition is never mixed in.
* A price conflict between Channel3 and a researched page blocks certification
  (`INSUFFICIENT_EVIDENCE`) and preserves both claims.
* Money stays `Decimal` throughout; no float arithmetic enters money paths.

### Recommendation rules

Only `COMPLIANT` assessments with a best offer are eligible.

| Category | Selection |
| --- | --- |
| `cheapest_compliant` | Lowest best-offer price |
| `best_value` | Highest transparent score: `3 × mandatory satisfied + 2 × preferences matched + verified specs − 2 × (missing + conflicts)` |
| `best_match` | Most preference matches (plus preferred-brand bonus), then cheapest |

Categories are omitted (`null`) when no product qualifies. `no_qualifying_products`
is `true` and nothing is manufactured. Each recommendation carries supporting
evidence, missing/uncertain information, trade-offs, source URL and optional
Gemini explanation.

---

## 4. KERNEL research decision matrix

Research is **not** run for every product. It is triggered only when a
purchase-decision evidence gap exists:

| Trigger | Investigated fields |
| --- | --- |
| Mandatory requirement is MISSING / UNVERIFIED / CONFLICTING | that requirement |
| Condition unknown on the selected offer | `condition` |
| Availability unknown | `availability` |
| Every offer lacks shipping cost | `shipping_cost` |
| Merchant identity unclear | `merchant_identity` |
| >25% price spread unexplained by attributes | `variants` |

Not triggered by missing preferences alone, and never for a `DISQUALIFIED`
product. At most `max_research_products` (default 3) products are researched per
search, cheapest first with a stable product-id tie-break. Each investigation
records: product id, offer id, exact URL, field list, reasons, browser session
id, findings (value/numeric/snippet/directly-observed), source URL, page title,
timestamp and limitations. Channel3 evidence is stored separately and never
overwritten.

---

## 5. API workflow

All new endpoints are under `/api/v1` and use the existing `X-User-Id`
convention (`user_demo` by default).

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/shopping/understand` | Gemini extracts a structured understanding + backend clarifications |
| `POST` | `/shopping/confirm` | User confirms/corrects the understanding; freezes authoritative criteria |
| `POST` | `/shopping/search` | Discovery → evaluation → targeted research → recommendations |
| `GET` | `/shopping/searches/{search_id}` | Stored result plus research provenance |
| `POST` | `/shopping/searches/{search_id}/research` | Internal, bounded re-research of products already in this search |

The research endpoint is **not** browser control: it accepts only optional
`product_ids` that belong to the stored search, performs the same deterministic
research decision, and returns the re-evaluated search. No arbitrary URL, action
or credentials can be submitted.

### Request/response shapes (abridged)

`POST /shopping/understand`
```json
{"text": "I must have wireless noise cancelling headphones under $100"}
```
→ `{ "request_id", "status": "understood", "understanding": {...}, "clarifications": [...], "next_action": "confirm" }`

`POST /shopping/confirm`
```json
{"request_id": "sreq_...", "understanding": { ...corrected understanding... }}
```
→ `{ "request_id", "status": "confirmed", "criteria": {...}, "next_action": "search" }`

`POST /shopping/search`
```json
{"request_id": "sreq_...", "research": true}
```
→ `{ "search_id", "request_id", "as_of", "data_mode", "source", "warnings",
     "criteria", "product_count", "assessments": [...], "research": {
       "planned": [...], "performed": [...] },
     "recommendations": {"cheapest_compliant": ..., "best_value": ...,
                         "best_match": ...},
     "explanations": {...}, "no_qualifying_products", "ai_participated" }`

Error conventions reuse the existing typed errors: `404 NOT_FOUND`,
`409 SHOPPING_STATE_INVALID`, `422 VALIDATION_ERROR`, `502 AI_UNAVAILABLE`,
`503 AI_NOT_CONFIGURED`. `GET /health` now additionally reports `ai_provider`
(effective), `ai_configured` and `kernel_configured` (booleans only).

### State machine

`understand` → `understood` → `confirm` → `confirmed` → `search`.
Searching before confirmation returns `409 SHOPPING_STATE_INVALID`; confirming an
unknown request returns `404 NOT_FOUND`; searches are private to their owner.

---

## 6. Configuration (no secrets)

Credentials are read from the root `.env`. Bare deployment names and documented
`SMARTCART_`-prefixed names are both accepted (`AliasChoices`).

| Setting | Default | Notes |
| --- | --- | --- |
| `GEMINI_API_KEY` / `SMARTCART_GEMINI_API_KEY` | – | required for AI |
| `GEMINI_MODEL` / `SMARTCART_GEMINI_MODEL` | `gemini-2.5-flash` in code | deployment `.env` sets the configured model |
| `SMARTCART_AI_PROVIDER` | `auto` | `auto` enables Gemini when a key exists; `none` disables |
| `KERNEL_API_KEY` / `SMARTCART_KERNEL_API_KEY` | – | required for research |
| `SMARTCART_KERNEL_BASE_URL` | `https://api.onkernel.com` | |
| `SMARTCART_KERNEL_TIMEOUT_SECONDS` | `90` | |
| `SMARTCART_MAX_RESEARCH_PRODUCTS` | `3` | per search |
| `SMARTCART_MAX_PAGE_TEXT_LENGTH` | `20000` | clipped before extraction |
| `SMARTCART_GEMINI_TIMEOUT_SECONDS` | `60` | |
| `SMARTCART_PRODUCT_SOURCE` | `mock` | `channel3` for live discovery |
| `SMARTCART_CHANNEL3_API_KEY` / `CHANNEL3_API_KEY` | – | live discovery |

`settings.effective_ai_provider` reports `gemini` only when a key is present and
the provider is not disabled. Empty values are treated as unconfigured.

---

## 7. Safety and Purchase Guard

* Gemini and page content have **no** authority over eligibility, payment or
  authorisation. Evaluation is deterministic backend code.
* KERNEL is read-only by construction: the adapter validates URLs (http/https
  only, no userinfo, no localhost/private/link-local/no-dot hosts), refuses
  cross-host redirects to private hosts, asserts the emitted JavaScript contains
  no fill/click/type/select/keyboard/mouse actions, uses timeouts and always
  deletes the session in `finally`.
* Page text is treated strictly as inert data. Prompt-injection strings are
  returned as text and can never become instructions or actions.
* The shopping workflow cannot approve, authorise, capture or complete a
  payment. There is no payment field, approval id or snapshot hash in a shopping
  response; the existing approval/payment endpoints still require a genuine
  approval. Purchase Guard tests remain green.
* Secrets never appear in error messages, evidence payloads or documentation.
  Evidence rows store a source URL, field, value, timestamp and trust level only.

---

## 8. Automated tests

Run from `backend` with a disposable SQLite database and mocked externals; no
live credential is used.

```
python -m pytest -p no:cacheprovider
```

**Result: 243 passed, 0 failed, 0 skipped, exit 0, 154.51 s.**
Baseline before Milestone 2 was 104 passed. New coverage: 139 tests.

| Test file | Tests | Focus |
| --- | ---: | --- |
| `test_gemini_adapter.py` | 16 | request construction, configured model, structured parsing, auth/rate-limit/404/5xx/timeout/transport/malformed/blocked, key redaction |
| `test_kernel_browser.py` | 31 | auth, session create/delete, navigation, text clipping, redirect handling, unsafe URLs, private-host redirects, action guard, typed errors/timeouts, malformed bodies, injection as data |
| `test_shopping_understanding.py` | 16 | prompt rules, MUST vs PREFERENCE, parsing/no invention, clarifications, confirmation/criteria |
| `test_shopping_evaluator.py` | 21 | all five statuses, unknown-mandatory blocking, budget/currency/exclusions, preference non-disqualification, offer selection, price conflicts, ranking, no qualifying products |
| `test_kernel_research_decision.py` | 28 | trigger matrix (including no-browse cases and DISQUALIFIED), exact URL selection, determinism/bounds, battery/ANC/availability/condition/shipping/price extraction, word boundaries, injection, redirect notes |
| `test_shopping_api_flow.py` | 16 | full understand→confirm→search, evidence rows + journal chain, ownership 404s, state errors, research off/on, no qualifying products, KERNEL failure cleanup, malformed/missing AI, Purchase Guard regression |
| `test_settings_env_loading.py` | 18 | bare/prefixed Channel3, Gemini and KERNEL aliases, `effective_ai_provider`, empty values, secret redaction (11 new) |

The new tests use `FakeLlm` and `FakeKernel`; they do not claim to prove live
integrations. Live verification is reported separately below.

---

## 9. Live integration verification

These were run as separate scripts against the real configured services. No
credential, bearer token, session id or CDP URL was printed. Settings loaded from
the root `.env` via the bare names.

| Integration | Result |
| --- | --- |
| Gemini | **PASS.** Configured model **`gemini-3.5-flash`** exists in the key's model list and returned a valid structured understanding (`category=headphones`, must-have `wireless`, `noise_cancellation`, `price`, preference `brand`, budget `100.00 USD`). Upstream returned intermittent HTTP 503 on earlier attempts; retries succeeded and the configured model was **not** changed. |
| Channel3 | **PASS.** Live search (`data_mode=LIVE`) returned **5 products / 19 offers**, all offer URLs https, 1 normalisation warning. |
| KERNEL | **PASS.** Session created → navigated `https://example.com/` (title/text read) → navigated `https://www.iana.org/domains/reserved` (title/text read) → session deleted in `finally`. |

Note: the full HTTP shopping workflow was exercised with fakes in the automated
suite, not end-to-end against all three live services simultaneously. The live
checks above verify each external integration at adapter level.

---

## 10. Known limitations

* Gemini occasionally returns transient HTTP 503; there is no automatic retry
  (the client maps it to `AI_UNAVAILABLE`). Interpretation falls back to the
  deterministic explanation; understanding returns `502` if Gemini fails.
* KERNEL extraction is intentionally simple literal/regex matching over visible
  text. It does not parse JSON-LD, screenshots, variant selectors or JavaScript
  rendered after the initial read; unresolved fields stay `MISSING`/`UNKNOWN`.
* Only up to 3 products are researched per search (configurable). Broader
  coverage would require more sessions/time.
* Condition unknown is disclosed in `missing_information` and triggers research,
  but does not by itself block a product unless the user makes condition a
  mandatory requirement.
* Channel3 offers are normalised tolerantly; malformed offers are skipped with
  warnings. Optional filters/attribute coverage depend on the provider payload.
* `X-User-Id` remains the existing lightweight identity convention; this is not
  production authentication.
* `Research` performed via the internal endpoint sets `ai_participated=false`
  (it returns deterministic explanations) rather than re-running Gemini prose.
* No frontend, basket optimisation, PayPal checkout or AI payment authority was
  added; those remain out of scope for this milestone.

---

## 11. Remaining work / next milestone

* Add bounded retry/backoff for transient Gemini 5xx responses (and optionally
  KERNEL 5xx), with tests.
* Richer page extraction (structured data, variant/merchant resolution) and
  per-field confidence/limitations.
* Re-run Gemini interpretation after targeted re-research if desired.
* Production authentication/authorisation for shopping endpoints.
* Frontend for understand → confirm → compare evidence → recommend, with clear
  UNVERIFIED/MISSING/CONFLICTING disclosure.
* Rate limiting, caching and cost controls for Gemini/KERNEL.
* Expand Channel3 attribute normalisation and live end-to-end regression
  fixtures.

---

## 12. Exact backend commands

From `C:\Users\Admin\Desktop\smartcart`:

```powershell
# Install / verify environment (once)
.\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
.\.venv\Scripts\python.exe -m pip check

# Compile
Set-Location backend
$env:PYTHONDONTWRITEBYTECODE="1"
C:\Users\Admin\Desktop\smartcart\.venv\Scripts\python.exe -m compileall -q app tests

# Full automated test suite (hermetic; mocks only)
C:\Users\Admin\Desktop\smartcart\.venv\Scripts\python.exe -m pytest -p no:cacheprovider

# Run the API (uses root .env; live adapters only if configured)
C:\Users\Admin\Desktop\smartcart\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# Health
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

Example shopping calls (PowerShell):

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

To force the deterministic mock source and disable AI/research for local smoke
runs, set `SMARTCART_PRODUCT_SOURCE=mock` and `SMARTCART_AI_PROVIDER=none` in the
shell (do not edit the root `.env`).
