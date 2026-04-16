# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A standalone Python 3.11+ CLI tool for API regression testing against Takeda Insight Center backend services. It captures HTTP/GraphQL responses, diffs them against baselines, and generates interactive HTML dashboards. No web framework, no async, no external dependencies beyond `openpyxl`.

## Commands

```bash
# Setup (one-time): copy .env.example to .env and fill in AUTHORIZATION and APP_AUTH_TOKEN
cp .env.example .env

# List all available test cases
python dashboard_automation.py list

# Capture a Ground Truth baseline (run against dev2)
python dashboard_automation.py run --label release-dev-new

# Run and compare against a prior baseline
python dashboard_automation.py run --label BICN-12345 --baseline output/<gt-folder>/run_results.json

# Offline diff between two previously captured runs
python dashboard_automation.py compare --gt <gt-path> --candidate <candidate-path> --label LABEL

# Compare dev2 vs test2 environments side-by-side
python dashboard_automation.py env-compare --label sprint-42

# Demo (no credentials needed)
python dashboard_automation.py demo

# Load testing variants (10 calls per case, aggregates latency stats)
python dashboard_automation.py run --label perf-check --load-test
python dashboard_automation.py env-compare --label sprint-42 --load-test

# Filter to specific service or case
python dashboard_automation.py run --label test --only-service Risk
python dashboard_automation.py run --label test --only-case <case_id>
```

## Architecture

**Entry point:** `dashboard_automation.py` → `automation/cli.py`

**Core pipeline for `run` command:**
1. `catalog.py` — loads `test_catalog.json` + optional Excel workbook (`API_Automation_Test_Data.xlsx`) into `ServiceConfig` + `TestCase` dataclasses
2. `runner.py` — builds requests, fires concurrent HTTP calls via `ThreadPoolExecutor` + `urllib`, captures responses
3. `normalize.py` — normalizes/hashes responses (SHA256) for comparison; handles field filtering via `ignore_paths` / `ignore_field_names` per test case
4. `comparator.py` — diffs candidate vs baseline `RunResult` lists, produces `ComparisonResult` list
5. `reporter.py` — writes HTML dashboards and JSON artifacts to `output/<ISO_TIMESTAMP>__<label>/`

**Key models** (all dataclasses in `models.py`): `TestCase`, `RunResult`, `ComparisonResult`, `LoadTestStats`, `EnvCompareResult`.

## Adding Test Cases

Test cases live in `test_catalog.json` under the `test_cases` array, or in the `API_Automation_Test_Data.xlsx` workbook (sheet import rules also in the JSON). Key fields: `service`, `protocol` (`rest`|`graphql`), `method`, `endpoint`, `headers`, `body`, `expected_status`, `ignore_paths`, `ignore_field_names`.

## Services

Six microservices, all defaulting to `api-insights-dev2.takeda.io`:

| Alias | Protocol | Base path |
|-------|----------|-----------|
| `Purchase_Order` | REST | `/purchase` |
| `Risk` | REST | `/risks` |
| `ERM_GraphQL` | GraphQL | `/risks/graphql/` |
| `Core` | GraphQL | `/core/graphql/graphql` |
| `Platform_API_Test` | GraphQL | `/core/user-management/graphql` |
| `Batches` | GraphQL | `/batches/graphql` |

## Output Structure

```
output/<ISO_TIMESTAMP>__<label>/
├── run_results.json          # Per-case HTTP status, hash, latency
├── comparison_results.json   # Diff outcomes
├── Dashboard.html            # Interactive diff dashboard
├── Raw Report.html           # Tabular summary
├── gt/ candidate/ baseline/  # Per-case response artifacts
└── comparisons/              # Detailed per-case JSON diffs
```
