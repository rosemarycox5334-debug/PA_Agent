# PR #15 Data Integrity Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven first-batch data-integrity gaps found in PR #15 without adding strategy, execution, position, backtest, GUI, LLM, authentication, or trading behavior.

**Architecture:** Keep `pa_agent.research_data` isolated and public-read-only. Bind every persisted page and checkpoint to a canonical request identity, validate immutable raw evidence before resume, reject conflicting primary-key duplicates, split schedule verification from coverage verification, make native-period comparison bidirectional, and persist explicit contract-rule validation facts.

**Tech Stack:** Python 3.12 standard library, dataclasses, Decimal, SHA-256, Canonical JSON, pytest 9, ruff.

## Global Constraints

- Public unauthenticated `GET` only, fixed `https://fapi.binance.com` allowlist.
- Raw evidence and completed datasets fail closed on identity/hash conflicts.
- Kline V1 accepts exactly 12 Binance fields and validates OHLCV/count/time invariants.
- Funding schedule version is `FUNDING_SCHEDULE_ASSUMED_8H_V1`; schedule and coverage are independent statuses.
- No second-batch implementation and no merge to `main`.

---

### Task 1: Request identity and immutable raw-page evidence

**Files:**
- Modify: `pa_agent/research_data/downloader.py`
- Modify: `pa_agent/research_data/storage.py`
- Modify: `tests/research_data/test_downloader_resume.py`
- Modify: `tests/research_data/test_storage.py`

**Interfaces:**
- Produces: `DOWNLOADER_SCHEMA_VERSION`, canonical request identity fields/hash, page evidence fields, and fail-closed resume rules.

- [ ] Add failing tests for checkpoint identity mismatch, completed-directory explicit reuse/rejection, changed parameters, raw-page hash tampering, and page-chain metadata.
- [ ] Run focused tests and confirm failures are caused by missing identity/evidence validation.
- [ ] Implement canonical identity from dataset name, path, normalized parameters, symbol/pair, interval, requested range, limit, and downloader schema version.
- [ ] Store identity/hash in every checkpoint and raw page; store payload hash, first/last timestamp, next start, request, retry count, download time, row count, and page index.
- [ ] Re-read and hash all raw pages before resume/final manifest; reject modified files, wrong identity, noncontiguous page indexes, and ambiguous completed-directory reuse.
- [ ] Run focused tests to green.

### Task 2: Canonical Kline schema and conflicting duplicates

**Files:**
- Modify: `pa_agent/research_data/normalize.py`
- Modify: `pa_agent/research_data/downloader.py`
- Modify: `tests/research_data/test_normalize.py`
- Modify: `tests/research_data/test_downloader_resume.py`

**Interfaces:**
- Produces: exact 12-field Kline V1 validation and `ConflictingDuplicateRecord` fail-closed behavior.

- [ ] Add failing tests for extra/missing fields, non-integral trade count, negative volumes/count, invalid OHLC relationships, invalid time order/interval boundary, identical duplicate acceptance, and conflicting duplicate rejection.
- [ ] Run focused tests and observe expected failures.
- [ ] Implement exact field count plus Decimal/integer/time/OHLCV invariants.
- [ ] Replace last-write-wins dedupe with byte-equivalent canonical duplicate acceptance and conflicting duplicate rejection.
- [ ] Run focused tests to green.

### Task 3: Funding schedule and requested-range coverage

**Files:**
- Modify: `pa_agent/research_data/models.py`
- Modify: `pa_agent/research_data/gaps.py`
- Modify: `pa_agent/research_data/cli.py`
- Modify: `tests/research_data/test_gaps.py`
- Modify: `tests/research_data/test_cli.py`

**Interfaces:**
- Produces: `schedule_status`, `coverage_status`, `gap_intervals`, and `observed_steps_ms` with expected range/version inputs.

- [ ] Add failing tests for zero records, one record, leading/trailing gaps, exact 8h schedule, millisecond jitter, and true non-8h observations.
- [ ] Run tests and confirm the old COMPLETE/EMPTY model fails.
- [ ] Implement independent schedule/coverage facts; zero or one record cannot establish schedule completeness.
- [ ] Version the tolerance for legitimate settlement timestamp jitter without treating true missing periods as the same class.
- [ ] Pass expected start/end and explicit schedule version from orchestration.
- [ ] Run focused tests to green.

### Task 4: Bidirectional native-period cross-validation

**Files:**
- Modify: `pa_agent/research_data/validation.py`
- Modify: `tests/research_data/test_aggregation_validation.py`

**Interfaces:**
- Produces: one-to-one key-set comparison and deterministic issues for missing, extra, and duplicate native bars.

- [ ] Add failing tests for empty aggregate, extra native, duplicate native, partial edge buckets, and mismatched key sets.
- [ ] Run tests and observe false-positive valid reports.
- [ ] Reject duplicates before map construction and compare aggregated/native key sets in both directions.
- [ ] Keep partial edge buckets outside the compared set but report them separately in orchestration.
- [ ] Run focused tests to green.

### Task 5: Complete contract-rule snapshot facts

**Files:**
- Modify: `pa_agent/research_data/models.py`
- Modify: `pa_agent/research_data/normalize.py`
- Modify: `pa_agent/research_data/cli.py`
- Modify: `tests/research_data/test_normalize.py`
- Modify: `tests/research_data/test_cli.py`

**Interfaces:**
- Produces: `ContractRuleValidationFailure` and snapshot fields `requested_symbols`, `returned_symbols`, `missing_symbols`, `source_hash`, `acquired_at_utc_ms`, `validity`, and `review_status`.

- [ ] Add failing tests proving that any missing requested BTC/ETH symbol raises a clear validation failure.
- [ ] Add failing orchestration test for a complete snapshot with explicit symbol-set and review facts.
- [ ] Implement fail-closed missing-symbol validation and persist the snapshot facts without silent partial success.
- [ ] Run focused tests to green.

### Task 6: Report and release verification

**Files:**
- Modify: `docs/verification/2026-07-13-first-batch-data-validation-results.md`

**Interfaces:**
- Produces: corrected evidence, test results, and remaining first-batch limitations.

- [ ] Correct the earlier claim that every raw page already contained a payload hash.
- [ ] Record the seven remediations and their regression tests.
- [ ] Run the required first-batch, security/scope, ruff, diff-check, and compileall commands.
- [ ] Re-run fixed-seed main/feature comparison and trigger or document actual GitHub Actions status.
- [ ] Commit, push the same feature branch, verify PR #15 head SHA/files, and stop without merging or starting second batch.

### Task 7: Exchange-server close-time boundary

**Files:**
- Modify: `pa_agent/research_data/cli.py`
- Modify: `pa_agent/research_data/normalize.py`
- Modify: `tests/research_data/test_cli.py`
- Modify: `tests/research_data/test_normalize.py`

**Interfaces:**
- Consumes: public `GET /fapi/v1/time` response `{\"serverTime\": int}`.
- Produces: `source_server_time_utc_ms`, its raw response SHA-256, and fail-closed `UNCLOSED_BAR` behavior before Canonical persistence.

- [ ] Add failing orchestration tests proving `/fapi/v1/time` is the first request, local `clock_ms` cannot change content hash, and source time evidence is persisted.
- [ ] Add failing normalization/orchestration tests proving any trade, mark, index, 4H, or 1D bar whose close boundary has not passed raises `UNCLOSED_BAR` before Canonical output.
- [ ] Run focused tests and confirm they fail because closure currently uses local `clock_ms` and unclosed rows are accepted.
- [ ] Fetch and validate Binance server time once at orchestration start, persist the raw envelope/hash, pass it to every Kline normalizer, and reject unclosed records.
- [ ] Run focused tests to green.

### Task 8: Persisted Canonical schema identities

**Files:**
- Modify: `pa_agent/research_data/models.py`
- Modify: `pa_agent/research_data/normalize.py`
- Modify: `tests/research_data/test_normalize.py`
- Modify: `tests/research_data/test_cli.py`

**Interfaces:**
- Produces: `Kline.schema_version=BINANCE_KLINE_V1_EXACT_12`, `FundingRate.schema_version=BINANCE_FUNDING_V1`, and `ContractRuleSnapshot.schema_version=CONTRACT_RULE_SNAPSHOT_V1` in Canonical JSON and content hashes.

- [ ] Add failing tests for all three model schema fields and for content-hash changes when a Canonical schema identity changes.
- [ ] Run tests and confirm the fields are absent.
- [ ] Add immutable schema fields at normalization boundaries so dataclass serialization automatically includes them in Canonical records and dataset hashes.
- [ ] Run focused tests to green.

### Task 9: Funding source identity and numeric invariants

**Files:**
- Modify: `pa_agent/research_data/normalize.py`
- Modify: `pa_agent/research_data/cli.py`
- Modify: `tests/research_data/test_normalize.py`

**Interfaces:**
- Consumes: `normalize_funding_rate(item, expected_symbol=...)`.
- Produces: fail-closed validation for exact symbol match, integral nonnegative funding time, strictly positive mark price, and finite funding rate.

- [ ] Add failing tests for wrong symbol, fractional/negative time, zero/negative mark price, and non-finite rate.
- [ ] Run focused tests and confirm existing normalization accepts the invalid cases.
- [ ] Add `expected_symbol` and enforce the four invariants before returning a Canonical `FundingRate`.
- [ ] Run focused tests to green.

### Task 10: Raw response request-range invariants and final evidence

**Files:**
- Modify: `pa_agent/research_data/downloader.py`
- Modify: `tests/research_data/test_downloader_resume.py`
- Modify: `docs/verification/2026-07-13-first-batch-data-validation-results.md`

**Interfaces:**
- Produces: `RAW_RECORD_OUT_OF_REQUEST_RANGE` for records outside the original inclusive range or before the page request `startTime`, enforced both on fresh pages and restored pages.

- [ ] Add failing tests for original-range overflow, page-start underflow, and restored-page tampering across page boundaries.
- [ ] Run focused tests and confirm out-of-range payloads currently pass raw validation.
- [ ] Validate every timestamp against the immutable request identity and the page request before raw commit and again during raw-page replay.
- [ ] Run all first-batch, security/scope, ruff, diff-check, and compileall commands.
- [ ] Run a real BTCUSDT/ETHUSDT three-day interrupted/resumed/clean comparison and record server time, closure, funding identity, raw-range, aggregation, and dual-hash evidence.
- [ ] Update the PR report, commit/push the existing feature branch, verify PR #15 status, and stop without merging or starting second batch.
