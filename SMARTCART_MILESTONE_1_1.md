# Smartcart — Milestone 1.1 Final Verification Report

**Date:** 2026-11-03
**Scope:** Milestone 1.1 final verification only (compile/pip, Uvicorn startup + health, live API workflow probes, no-shim audit rerun, milestone report). A genuine application defect surfaced during live verification and was root-caused and fixed under the reporting rules; no other application or test work was performed.

---

## 1. Environment

| Item | Value |
| --- | --- |
| Python | 3.13 (project `.venv`) |
| Test runner | pytest 9.0.1 (`-p no:cacheprovider`) |
| Server | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8777` (single process, PID verified on port) |
| Server env | `SMARTCART_DATABASE_URL=sqlite+pysqlite:///C:\Users\Admin\AppData\Local\Temp\opencode\milestone11_live.db`, `SMARTCART_PRODUCT_SOURCE=mock`, `SMARTCART_AI_PROVIDER=none` |
| Externals | No real PayPal (credentials absent → 503 path), no MCP, no live Channel3. Mock product source only. |
| Version control | none (workspace is not a git repo) — noted as a safety-net limitation |

Constraints honored: `_smoke.db` and `SMARTCART_IMPLEMENTATION_AUDIT.md` untouched (the audit doc still contains all of its original BROKEN findings verbatim); prior recovery reports unmodified; original audit scripts preserved (only copies were edited/run); no shims or bypasses in the rerun audit; no existing tests modified.

---

## 2. Checks executed and results

### 2.1 Compile & dependencies — PASS (rerun after defect fix: PASS)

| Command (cwd `backend`) | Result |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 python -m compileall -q app tests` | exit 0 |
| `python -m pip check` | exit 0 — “No broken requirements found.” |

Executed before and after the defect fix; identical results.

### 2.2 Uvicorn startup + `/api/v1/health` — PASS

```json
{
  "status": "ok", "environment": "dev", "data_mode": "MOCK",
  "product_source": "mock", "channel3_configured": false,
  "ai_provider": "none", "paypal_configured": false,
  "paypal_environment": "sandbox", "guard_policy_version": "1.0.0",
  "journal_chain_ok": true, "journal_broken_at": null
}
```

`GET /api/v1/health` → 200. Verified (a) before the probes and (b) again after the complete probe sequence — after the full workflow the final health still reports `journal_chain_ok: true`.

### 2.3 Live API workflow probes — PASS 12/12

Script: `C:\Users\Admin\AppData\Local\Temp\opencode\milestone11_live_probes.py` — an httpx client against the running uvicorn with its own throwaway DB (mock data, no external credentials).

| Check | Result |
| --- | --- |
| search 200 | PASS |
| bestbuy offer present | PASS |
| quote 201 | PASS |
| quote ALLOW | PASS |
| approve 200 | PASS |
| revalidate ALLOW (live) | PASS |
| payment 503 PAYPAL_NOT_CONFIGURED (live) | PASS |
| reject 200 | PASS |
| reject-then-approve 409 QUOTE_NOT_APPROVABLE (live) | PASS |
| journal chain_ok (live) | PASS |
| journal contains revalidation + failure evidence | PASS |
| final health chain_ok | PASS |

`SUMMARY 12/12 checks passed`, exit 0. Server log confirms 200/201/200/200/503/200/409/200/200 for the corresponding requests. (One earlier probe run aborted with `UnicodeEncodeError: 'cp1252'` — a defect in the probe script itself printing a `→` character on the Windows console; script fixed, flow unchanged, rerun green. Not an application issue.)

### 2.4 Full test suite — PASS 79/79 (77 pre-existing + 2 new regression tests)

`PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider` (cwd `backend`) → **79 passed, 0 failed, 0 skipped**, exit 0, 51.9 s.

The 77 pre-existing tests were **not modified**; 2 new regression tests (see 3.4) were added in a new file.

### 2.5 No-shim audit rerun — PASS 67/67 + edge audit PASS

Originals at `C:\Users\Admin\AppData\Local\Temp\opencode\audit_smoke.py` and `audit_edge.py` are preserved byte-for-byte. Two copies were produced and run:

| Script (copy) | Changes vs original | Result |
| --- | --- | --- |
| `verify_no_shim.py` (copy of `audit_smoke.py`) | removed both audit-only shim blocks (aware-UTC datetime normalizer; Decimal-str journal payload); added one check: reject → re-approve must be 409 `QUOTE_NOT_APPROVABLE` | **67/67 checks passed**, exit 0 — includes journal UPDATE/DELETE and evidence UPDATE blocked by append-only triggers, price-change invalidation, expiry, idempotent capture replay, webhook dedupe, provider amount mismatch rejection |
| `audit_edge.py` run as-is | none (already shim-free) | exit 0 — reject→2nd approve 409 `QUOTE_NOT_APPROVABLE` (journaled terminal state); cross-user quote/approve 404; supersede semantics correct; journal isolation by user holds |

Both former shims were **no longer needed under the current code**: the underlying defects were fixed root-cause in Phase 1–4 (`UTCDateTime` type + savepoint-correct pysqlite transaction handling in `app/persistence/base.py`; `json_safe` journal payload serialization in `app/persistence/repositories.py`). This is direct evidence the Phase 1–4 fixes hold in the audit context without any bypasses.

One anomaly, documented: the very first attempt at running `verify_no_shim.py` reported a 120 s tool timeout with no output; an immediate unbuffered rerun executed all 67 checks in seconds with correct results. The completed run is the recorded evidence (two subsequent invocations also complete normally).

---

## 3. Genuine application defect found during live verification — ROOT-CAUSED & FIXED

The milestone probes exposed `(sqlite3.OperationalError) database is locked` on the very first `POST /api/v1/quotes` after a `POST /api/v1/search`, deterministically, only under real uvicorn — while the identical flow passed the 77-test suite, TestClient audits, and plain threaded scripts.

### 3.1 Diagnosis method (instrumented, no shims; app logic untouched during diagnosis)

- Temporary diagnostic launchers (verification-only, outside the project) added `set_trace_callback` on every raw sqlite3 connection, pool checkout/checkin listeners, SQL echo and pool echo; never modified project files.
- Offline inspection of the throwaway DB between runs: WAL active, `user_demo` row durably committed, external `BEGIN IMMEDIATE`/writes succeed while the server's own writer fails.
- Bare sequential replication (pure sqlite3, same pragmas, same lifecycle) passes — isolating the concurrency factor unique to the live server.

### 3.2 Root cause

1. Sync FastAPI dependencies (`get_session` in `app/api/deps.py`) commit the session **during teardown, after the response has already been sent**. The prior request's commit is therefore still in flight (WAL fsync, ~tens of ms) when the very next keep-alive request arrives.
2. With a **deferred `BEGIN`** in WAL mode, the read snapshot is taken at the first *read*, not at `BEGIN`. A transaction whose first read executes while the previous commit is still being written to the WAL gets a **stale (pre-commit) snapshot** — confirmed by the raw trace: the quote request's `SELECT users WHERE id = 'user_demo'` returned no row even though the row was durably committed and externally visible.
3. That connection then attempts its first write (INSERT), and SQLite returns `SQLITE_BUSY_SNAPSHOT` — surfaced as the generic `database is locked` — **bypassing `busy_timeout`** entirely (waiting would deadlock, so SQLite fails immediately). This matches the observed sub-second deterministic failure and the absence of any externally-observable write lock.
4. Sequentials contexts (TestClient suites, plain threaded scripts) do not interleave requests at the commit boundary — hence "passes everywhere except the live server". FastAPI's teardown-commit-after-response makes the race deterministic for same-connection keep-alive clients.

### 3.3 Fix (minimal, engine-local, one statement change)

`app/persistence/base.py` — the SQLite transaction-begin listener now emits **`BEGIN IMMEDIATE`** instead of a deferred `BEGIN`:

| | Before | After |
| --- | --- | --- |
| transaction begin | `connection.exec_driver_sql("BEGIN")` | `connection.exec_driver_sql("BEGIN IMMEDIATE")` |

Rationale (kept as a comment next to the code): a transaction that will write takes the WAL write lock at start; if a previous commit is in flight, `busy_timeout` applies (polite wait) and the snapshot is taken only after it lands. Read-only requests acquire the write lock briefly; savepoint/nested-transaction semantics, append-only triggers, last-UTC datetime semantics, and journal chain hashing are unaffected (re-verified below). Before in-hand confirmation, the identical change was first validated through a temporary launcher monkey-patch on a live uvicorn instance: 3/3 repeated search→quote flows returned 201 (pre-fix: 0/6).

### 3.4 Regression test added

New file `backend/tests/test_sqlite_begin_immediate.py` (no existing tests touched):

1. `test_sqlite_transactions_begin_immediate` — attaches a raw statement trace to the underlying sqlite3 connection of an engine built by `create_engine_for` and asserts every transaction begins with `BEGIN IMMEDIATE`.
2. `test_orm_round_trip_still_works` — normal ORM write/read round-trip (guards `Base.metadata.create_all` / sessionmaker / journal-relevant behavior against regressions from the change).

### 3.5 Verification of the fix

| Re-check | Result |
| --- | --- |
| Full suite after fix | 79 passed / 0 failed / 0 skipped, exit 0 |
| Live probes after fix (12 checks) | 12/12 PASS, exit 0 |
| Health after full workflow | `journal_chain_ok: true` |
| No-shim audit after fix | 67/67 PASS, exit 0 |
| Edge audit after fix (as-is) | PASS, exit 0 |
| Journal chain (live, after everything) | `chain_ok = True` on `GET /api/v1/journal` |

---

## 4. Exact commands and chronology

| # | Command (cwd `backend` unless noted) | Result |
| --- | --- | --- |
| 1 | `python -m compileall -q app tests` | exit 0 |
| 2 | `python -m pip check` | exit 0 |
| 3 | `python -m uvicorn app.main:app --host 127.0.0.1 --port 8777` (throwaway DB) | running; `GET /api/v1/health` → 200 |
| 4 | `python %TEMP%\opencode\milestone11_live_probes.py` | search 200 → quote **500** (defect discovered; see §3) |
| 5 | (diagnosis phase — see §3.1; server restarted several times with fresh throwaway DBs) | root cause identified; validated fix via temporary launcher: quote → 201, deterministic |
| 6 | fix applied to `app/persistence/base.py` + new test file `tests/test_sqlite_begin_immediate.py` | — |
| 7 | `python -m pytest -p no:cacheprovider tests/test_sqlite_begin_immediate.py -q` | 1 passed → 2 passed (after assertion cleanup) |
| 8 | `python -m pytest -p no:cacheprovider` | **79 passed**, 0 failed, 0 skipped, exit 0 |
| 9 | fresh uvicorn + fresh throwaway DB; `GET /api/v1/health` | 200 OK; all fields correct |
| 10 | `python %TEMP%\opencode\milestone11_live_probes.py` | **12/12 PASS**, exit 0 |
| 11 | `Copy-Item ...audit_smoke.py ...verify_no_shim.py` (copy; then shim blocks removed, one check added) | — |
| 12 | `python %TEMP%\opencode\verify_no_shim.py` | **67/67 PASS**, exit 0 |
| 13 | `python %TEMP%\opencode\audit_edge.py` | PASS, exit 0 |
| 14 | final `GET /api/v1/journal?limit=500` on live server | `chain_ok = true` |
| 15 | re-runs of #1, #2 after fix | exit 0 both |

No check was skipped. Nothing failed except the one application defect (§3), which was resolved and everything re-run green.

---

## 5. Limitations and not-run checks

- **Mock-only data path.** `product_source=mock`, `ai_provider=none`. The Channel3 marketplace adapter and Gemini adapter were only exercised via their absence/misconfiguration paths (fast-fail checks). No live external integration is claimed as verified.
- **No real PayPal.** Verified only the unconfigured → 503 `PAYPAL_NOT_CONFIGURED` path and the in-process fake-gateway audit; no credentials were used or changed; no real order/capture/webhook against PayPal.
- **No MCP work.** Excluded by scope.
- **Not tested:** host reboots / crash recovery beyond normal kill, `>1` uvicorn workers, WAL behavior across many concurrent remote clients, database migration tooling, disk-full scenarios.
- **Stop condition honored.** No Milestone 1.2 work or new features were started beyond the single defect fix (§3), which was required to complete the requested live verification.
