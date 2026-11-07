# First Batch Data Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public-read-only Binance USDⓈ-M data acquisition and validation package for BTCUSDT and ETHUSDT with canonical persistence, resumable pagination, independent stream gap reports, stable content hashes, acquisition hashes, current contract-rule snapshots, and native-period cross-validation.

**Architecture:** Add an isolated `pa_agent.research_data` package that uses only the Python standard library and does not import PA_Agent GUI, AI, strategy, position, or ledger modules. Pure model/canonical/validation functions remain independent from the allowlisted HTTP client and filesystem orchestration. Tests use real pure functions and an injected fake HTTP transport; one explicit live example uses only public GET market-data endpoints.

**Tech Stack:** Python 3.12 standard library (`dataclasses`, `decimal`, `hashlib`, `json`, `pathlib`, `urllib`), pytest 9, hypothesis 6, ruff.

## Global Constraints

- Only public unauthenticated `GET` requests to `https://fapi.binance.com` and an exact allowlist of market-data paths.
- Never accept or send API keys, signatures, account parameters, or order/trade requests.
- First batch must contain no indicators, Candidate generation, ExecutionPlan business logic, positions, matching, funding settlement, liquidation, ledger, performance, GUI, or LLM code.
- Index price is audit-only; every stream reports independent gap intervals and status.
- `AGG_VALIDATION_V1` uses frozen Decimal absolute/relative tolerances; OHLC/UTC/trade_count remain exact.
- Versioned acquisition/strategy/execution/audit/contract content hashes are independent from acquisition metadata; legacy `dataset_content_hash` aliases the complete acquisition bundle hash.
- `computational_experiment_id` must not accept acquisition fields; Candidate scope accepts only the explicit versioned strategy-data dependency.

---

### Task 1: Canonical models and serialization

**Files:**
- Create: `pa_agent/research_data/__init__.py`
- Create: `pa_agent/research_data/models.py`
- Create: `pa_agent/research_data/canonical.py`
- Test: `tests/research_data/test_canonical.py`
- Test: `tests/research_data/test_models.py`

**Interfaces:**
- Produces: `canonical_dumps(value) -> str`, `canonical_decimal(value) -> str`, immutable `Kline`, `FundingRate`, `GapInterval`, `StreamGapReport`, and `ContractRuleSnapshot` dataclasses.

- [ ] Write failing tests proving key order, Decimal normalization, negative-zero normalization, UTC integer preservation, immutable models, and primary-key ordering.
- [ ] Run `python -m pytest tests/research_data/test_canonical.py tests/research_data/test_models.py -v` and verify missing-module failures.
- [ ] Implement minimal dataclasses and Canonical JSON conversion. Decimal values serialize as non-exponent strings; mappings sort keys; caller-declared record sort keys determine dataset ordering.
- [ ] Re-run the tests and verify they pass.
- [ ] Run `ruff check pa_agent/research_data/canonical.py pa_agent/research_data/models.py tests/research_data/test_canonical.py tests/research_data/test_models.py`.

### Task 2: Stable content and acquisition identities

**Files:**
- Create: `pa_agent/research_data/hashing.py`
- Test: `tests/research_data/test_hashing.py`

**Interfaces:**
- Consumes: `canonical_dumps`.
- Produces: per-dataset content hashes, versioned acquisition/strategy/execution/audit/contract bundle hashes, `acquisition_manifest_hash(manifest)`, `acquisition_run_id(manifest)`, and explicit-dependency `computational_experiment_id(...)`.

- [ ] Write failing tests showing identical normalized records with different order/download metadata yield the same content hash; different acquisition metadata yields different acquisition hashes; computational IDs reject acquisition arguments.
- [ ] Run the test and verify failures are due to missing functions.
- [ ] Implement SHA-256 over Canonical UTF-8 bytes. Make the computational function signature accept only explicit versioned dependency hashes, experiment scope, sample range, strategy version, execution version, cost version, code commit, and dependency lock version.
- [ ] Re-run tests and ruff.

### Task 3: Security-constrained Binance public client

**Files:**
- Create: `pa_agent/research_data/binance_public.py`
- Test: `tests/research_data/test_binance_public_security.py`

**Interfaces:**
- Produces: `BinancePublicClient.get_json(path, params)`, with injected `transport(url, timeout) -> bytes`.
- Allowlist: `/fapi/v1/klines`, `/fapi/v1/markPriceKlines`, `/fapi/v1/indexPriceKlines`, `/fapi/v1/fundingRate`, `/fapi/v1/exchangeInfo`, `/fapi/v1/time`.

- [ ] Write failing tests that allow valid public GET URLs and reject alternate hosts, schemes, userinfo, unlisted paths, API key/signature parameters, POST-like method inputs, and account/trade paths.
- [ ] Run tests and verify missing client failure.
- [ ] Implement an exact-host HTTPS URL builder and stdlib GET transport with timeout, JSON type validation, and no headers containing credentials.
- [ ] Re-run security tests and ruff.

### Task 4: Raw/canonical atomic storage and resumable pages

**Files:**
- Create: `pa_agent/research_data/storage.py`
- Create: `pa_agent/research_data/downloader.py`
- Test: `tests/research_data/test_storage.py`
- Test: `tests/research_data/test_downloader_resume.py`

**Interfaces:**
- Produces: `AtomicDatasetStore`, `DatasetDownloader.download_pages(...)`, raw page files, checkpoint, canonical JSONL, acquisition manifest.

- [ ] Write failing storage tests for atomic replacement, no partial target after a simulated write failure, Canonical JSONL ordering, and checkpoint round trip.
- [ ] Implement atomic temp-file write plus `os.replace` and read helpers; re-run storage tests.
- [ ] Write failing downloader tests with an injected fake client: multiple pages, boundary overlap, duplicate timestamp, interruption, restart, and same final content after resume.
- [ ] Implement pagination using a declared timestamp extractor, overlap re-fetch on resume, primary-key dedupe, sorted merge, per-page raw hashes, and checkpoint advancement only after a page is persisted.
- [ ] Re-run downloader/storage tests and ruff.

### Task 5: Binance normalization, gap facts, and current contract snapshots

**Files:**
- Create: `pa_agent/research_data/normalize.py`
- Create: `pa_agent/research_data/gaps.py`
- Test: `tests/research_data/test_normalize.py`
- Test: `tests/research_data/test_gaps.py`

**Interfaces:**
- Produces: `normalize_trade_kline`, `normalize_price_kline`, `normalize_funding_rate`, `normalize_contract_rules`, `detect_gap_intervals`.

- [ ] Write failing tests for Binance array/object parsing, Decimal preservation, closed-bar status, symbol/pair identity, malformed schema rejection, and current snapshot semantics.
- [ ] Implement strict normalization; unknown array shape or invalid Decimal raises `DataSchemaError`.
- [ ] Write failing tests that separately report `trade_gap_intervals`, `mark_gap_intervals`, `funding_gap_intervals`, and `index_gap_intervals` without a global invalid flag.
- [ ] Implement expected-step gap interval detection and per-stream status (`COMPLETE`, `GAPS_DETECTED`, `EMPTY`).
- [ ] Re-run tests and ruff.

### Task 6: Aggregation and AGG_VALIDATION_V1

**Files:**
- Create: `pa_agent/research_data/aggregation.py`
- Create: `pa_agent/research_data/validation.py`
- Test: `tests/research_data/test_aggregation.py`
- Test: `tests/research_data/test_aggregation_validation.py`

**Interfaces:**
- Produces: `aggregate_klines(one_minute, interval_ms)`, `validate_native_bars(aggregated, native, version="AGG_VALIDATION_V1")`.

- [ ] Write failing aggregation tests for exact UTC 4H/1D bucket boundaries, OHLC, four volume sums, and trade_count sum; incomplete buckets are reported and not emitted as valid aggregates.
- [ ] Implement Decimal aggregation and incomplete-bucket facts; re-run tests.
- [ ] Write failing validation tests for exact OHLC/UTC/trade_count, each absolute tolerance boundary, each relative tolerance boundary, OR semantics, and just-outside failures.
- [ ] Implement frozen constants: base volumes abs `1E-8`, quote volumes abs `1E-6`, all relative `1E-12`; use Decimal only.
- [ ] Re-run tests and ruff.

### Task 7: Orchestration CLI and public small-window example

**Files:**
- Create: `pa_agent/research_data/cli.py`
- Create: `scripts/download_binance_research.py`
- Modify: `.gitignore`
- Test: `tests/research_data/test_cli.py`
- Test: `tests/research_data/test_scope_guard.py`

**Interfaces:**
- Produces: CLI arguments for symbols, UTC start/end, output directory, resume flag, page limit, and optional index audit download; summary JSON with hashes, gap reports, cross-validation, contract snapshot, and acquisition metadata.

- [ ] Write failing CLI tests using a fake client and temporary directory to prove BTC/ETH downloads, resume, hash separation, gap facts, and summary output.
- [ ] Implement the minimal orchestration and CLI entry point; raw/canonical output stays in an ignored `research_data_output/` directory.
- [ ] Write failing scope-guard tests that scan `pa_agent/research_data` for forbidden account/trade endpoint strings, credential headers, `create_order`, and imports from GUI/AI/strategy/ledger modules.
- [ ] Implement any minimal boundary declarations needed for the guard to pass; do not weaken the guard.
- [ ] Run the complete first-batch test suite and ruff.
- [ ] Execute a live public small-window download for BTCUSDT and ETHUSDT, interrupt once, resume, perform a second clean acquisition, and compare content/acquisition hashes plus 4H/1D validation.

### Task 8: Final verification and commits

**Files:**
- Modify: `docs/superpowers/plans/2026-07-13-first-batch-data-validation.md` checkboxes only as work completes.

**Interfaces:**
- Produces: verified first-batch commit history and evidence for handoff.

- [ ] Run `python -m pytest tests/research_data -v` and retain complete output.
- [ ] Run `python -m ruff check pa_agent/research_data tests/research_data scripts/download_binance_research.py`.
- [ ] Run scope/security tests separately with `python -m pytest tests/research_data/test_binance_public_security.py tests/research_data/test_scope_guard.py -v`.
- [ ] Run `git diff --check`, `git status --short`, and inspect `git diff --stat`.
- [ ] Verify the source tree contains no indicator, Candidate-generation, ExecutionPlan-business, position, matching, funding-settlement, liquidation, ledger, performance, GUI, LLM, auth, or live-trading implementation.
- [ ] Commit independently testable work with descriptive messages; do not start second-batch tasks.
