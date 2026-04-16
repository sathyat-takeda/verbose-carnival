# API Test Automation Dashboard

Standalone regression and load-test runner for the Takeda Insight Center backend services.
The script hits live APIs, normalises responses, diffs them against a Ground Truth (GT) snapshot,
and produces `Dashboard.html`, `Raw Report.html`, and — when load testing is enabled — a
`Load Test Dashboard.html` in a timestamped output folder.

All services default to the **dev2** environment (`api-insights-dev2.takeda.io`).
Pass `--env test2` (or any other environment name) to redirect every URL in one flag.

---

## Project layout

```
dashboard_automation.py       ← CLI entry point
test_catalog.json             ← service definitions + all test cases
API_Automation_Test_Data.xlsx ← workbook source for additional cases
.env                          ← credentials (copy from .env.example)
automation/
  catalog.py                  ← loads services and cases from JSON + workbook
  runner.py                   ← HTTP execution, single-pass and load-test runs
  comparator.py               ← diffs run results and load-test stats against GT
  normalize.py                ← response normalisation and hashing
  reporter.py                 ← Dashboard / Raw Report / Load Test Dashboard HTML
  cli.py                      ← argparse wiring
  models.py                   ← dataclasses (TestCase, RunResult, LoadTestStats, …)
  io_utils.py                 ← file helpers
  workbook.py                 ← xlsx row parsing
output/                       ← generated; one sub-folder per run
```

---

## Setup

**Requirements:** Python 3.11+ (no third-party packages needed for the runner itself;
`openpyxl` is used only when loading the workbook).

```bash
pip install openpyxl          # only needed for workbook import
cp .env.example .env          # create your credentials file
```

Edit `.env` and fill in the two required headers:

```
AUTHORIZATION=Bearer <your-token>
APP_AUTH_TOKEN=<your-app-token>
```

Optionally override a service base URL (takes precedence over the catalog value):

```
PURCHASE_ORDER_BASE_URL=https://api-insights-dev2.takeda.io/purchase
```

---

## Commands at a glance

| Command | Purpose |
|---------|---------|
| `list` | Print every test case (service, method, enabled/disabled) |
| `run` | Execute cases, build HTML dashboards |
| `compare` | Offline comparison of two existing result files — no API calls |
| `demo` | Render sample dashboards with no network calls |

---

## Step-by-step: how to run

### 1 — List all cases

```bash
python dashboard_automation.py list
```

Filter to a specific service:

```bash
python dashboard_automation.py list --only-service insight-center-purchase-order-services
```

Filter to a specific case:

```bash
python dashboard_automation.py list --only-case po_adh_sdn_status_fetch
```

---

### 2 — Capture Ground Truth from `release/dev-new`

Run against **dev2** (the default) and save the GT snapshot:

```bash
python dashboard_automation.py run --label release-dev-new
```

With **load testing** (runs each API 10 times, captures latency stats):

```bash
python dashboard_automation.py run --label release-dev-new --load-test
```

Custom number of runs (e.g. 20):

```bash
python dashboard_automation.py run --label release-dev-new --load-test --load-test-runs 20
```

On a different environment (e.g. test2):

```bash
python dashboard_automation.py run --label release-dev-new --env test2 --load-test
```

Output folder: `output/<timestamp>__release-dev-new/`

Key files produced:

| File | Purpose |
|------|---------|
| `run_results.json` | Per-case HTTP status, response hash, latency |
| `gt_snapshot.json` | Same data, named for GT workflows |
| `load_test_gt.json` | Per-case latency stats + representative response *(load test only)* |
| `Dashboard.html` | Visual summary of the GT capture |
| `Load Test Dashboard.html` | Latency stats table *(load test only)* |
| `gt/<service>/<case>/` | Per-case response artifacts |

---

### 3 — Compare a feature branch against the GT

**Single-pass response comparison only:**

```bash
python dashboard_automation.py run \
  --label BICN-12345 \
  --baseline output/<gt-folder>/run_results.json
```

**Full comparison: response diff + load-test latency comparison:**

```bash
python dashboard_automation.py run \
  --label BICN-12345 \
  --baseline output/<gt-folder>/run_results.json \
  --load-test \
  --load-test-gt output/<gt-folder>/load_test_gt.json
```

Fail CI when any diff is found:

```bash
python dashboard_automation.py run \
  --label BICN-12345 \
  --baseline output/<gt-folder>/run_results.json \
  --load-test \
  --load-test-gt output/<gt-folder>/load_test_gt.json \
  --fail-on-diff
```

Custom latency regression threshold (default is 20 %):

```bash
python dashboard_automation.py run \
  --label BICN-12345 \
  --baseline output/<gt-folder>/run_results.json \
  --load-test \
  --load-test-gt output/<gt-folder>/load_test_gt.json \
  --latency-threshold 15
```

Output folder: `output/<timestamp>__BICN-12345/`

Additional files produced on a comparison run:

| File | Purpose |
|------|---------|
| `comparison_results.json` | Per-case response diff results |
| `load_test_comparison.json` | Per-case latency delta + response change *(load test only)* |
| `Dashboard.html` | Response comparison dashboard |
| `Load Test Dashboard.html` | GT vs candidate latency table with delta % *(load test only)* |
| `candidate/<service>/<case>/` | Per-case candidate artifacts |
| `baseline/<service>/<case>/` | Per-case GT artifacts (copied from baseline) |
| `comparisons/<service>/<case>/comparison.json` | Detailed diff per case |

---

### 4 — Tester-supplied Ground Truth

Testers who already know what the correct response looks like can supply their own GT
instead of first running a GT capture. The script hits the live APIs, then compares the
actual responses against the tester's expectations.

**JSON format** — one object per case, `expected_response` is optional:

```json
[
  {
    "case_id": "po_adh_sdn_status_fetch",
    "expected_status": 200,
    "expected_response": {
      "status": "Success",
      "data": { "batches": [ { "sdn_status": null } ] }
    },
    "notes": "sdn_status must be null or active"
  },
  {
    "case_id": "po_adh_count_by_batch",
    "expected_status": 200
  }
]
```

Omit `expected_response` for a **status-code-only** check on that case.

**CSV format** — `expected_response_file` is optional (path relative to the CSV):

```csv
case_id,expected_status,expected_response_file,notes
po_adh_sdn_status_fetch,200,expected/sdn_status.json,SDN status check
po_adh_batch_stages_all_params,200,,status only
po_adh_count_by_batch,200,,
```

**Run:**

```bash
python dashboard_automation.py run --label BICN-12345 \
  --tester-gt my_expected.json \
  --baseline-label "tester-approved"

# or with a CSV
python dashboard_automation.py run --label BICN-12345 \
  --tester-gt my_expected.csv \
  --baseline-label "tester-approved"
```

`--tester-gt` and `--baseline` are mutually exclusive — use one or the other.

---

### 5 — Offline comparison (`compare` subcommand)

Compare two existing `run_results.json` files without making any API calls.
Useful when you already have both a GT capture and a candidate capture on disk
and just want to regenerate the comparison dashboard.

```bash
python dashboard_automation.py compare \
  --gt    output/<gt-folder>/run_results.json \
  --candidate output/<candidate-folder>/run_results.json \
  --label BICN-12345 \
  --baseline-label release-dev-new
```

Fail CI when any diff is found:

```bash
python dashboard_automation.py compare \
  --gt    output/<gt-folder>/run_results.json \
  --candidate output/<candidate-folder>/run_results.json \
  --label BICN-12345 \
  --fail-on-diff
```

---

### 6 — Quick demo (no credentials needed)

```bash
python dashboard_automation.py demo
```

Opens `output/<timestamp>__demo-candidate/Dashboard.html` in your file browser.

---

### 7 — Narrow a run to specific cases or services

```bash
# Only PO Adherence cases
python dashboard_automation.py run --label release-dev-new \
  --only-service insight-center-purchase-order-services

# Only two specific cases
python dashboard_automation.py run --label release-dev-new \
  --only-case po_adh_sdn_status_fetch \
  --only-case po_adh_count_by_batch

# Skip workbook rows (manual catalog cases only)
python dashboard_automation.py run --label release-dev-new --skip-workbook
```

---

## Complete flag reference

```
dashboard_automation.py list    [options]
dashboard_automation.py run     --label LABEL [options]
dashboard_automation.py compare --gt PATH --candidate PATH --label LABEL [options]
dashboard_automation.py demo    [--label LABEL] [--baseline-label LABEL]
```

**Shared flags (list + run):**

| Flag | Default | Description |
|------|---------|-------------|
| `--env ENV` | `dev2` | Environment name. Rewrites `api-insights-dev2.takeda.io` → `api-insights-ENV.takeda.io` in every URL. |
| `--catalog FILE` | `test_catalog.json` | Path to the test catalog JSON. |
| `--env-file FILE` | `.env` | Path to credentials file. |
| `--workbook FILE` | `API_Automation_Test_Data.xlsx` | Path to Excel workbook. |
| `--only-service SERVICE_ID` | *(all)* | Restrict to one service. Repeatable. |
| `--only-case CASE_ID` | *(all)* | Restrict to one case. Repeatable. |
| `--skip-workbook` | false | Skip loading cases from the Excel workbook. |

**`run`-only flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--label LABEL` | *(required)* | Short label for this run (used in folder name and dashboard title). |
| `--baseline FILE` | *(none)* | Path to a `run_results.json` from a previous GT run. Enables comparison mode. Mutually exclusive with `--tester-gt`. |
| `--tester-gt FILE` | *(none)* | Path to a tester-supplied GT file (`.json` or `.csv`). Hits live APIs then compares against tester expectations. Mutually exclusive with `--baseline`. |
| `--baseline-label LABEL` | `release/dev-new` | Display name for the baseline / tester GT in dashboards. |
| `--fail-on-diff` | false | Exit code 1 if any comparison diff is found (useful in CI). |
| `--load-test` | false | Run each case N times and collect latency statistics. |
| `--load-test-runs N` | `10` | Number of calls per case in load test mode. |
| `--load-test-gt FILE` | *(none)* | Path to a `load_test_gt.json` to compare latency against. |
| `--latency-threshold PCT` | `20` | % above which candidate p95 is flagged as a latency regression. |

**`compare`-only flags (no API calls made):**

| Flag | Default | Description |
|------|---------|-------------|
| `--candidate FILE` | *(required)* | Path to the candidate `run_results.json` (feature branch / newer run). |
| `--gt FILE` | *(required)* | Path to the GT `run_results.json` (ground truth / approved baseline). |
| `--label LABEL` | *(required)* | Short label for the candidate run. |
| `--baseline-label LABEL` | `GT` | Display label for the GT in dashboards. |
| `--catalog FILE` | `test_catalog.json` | Path to catalog — used only for service display names. |
| `--fail-on-diff` | false | Exit code 1 if any comparison diff is found. |

---

## Services and test cases

### Services (all default to dev2)

| Alias | Kind | Service ID |
|-------|------|-----------|
| `Purchase_Order` | REST | `insight-center-purchase-order-services` |
| `Risk` | REST | `insight-center-dev-erm-services` |
| `ERM_GraphQL` | GraphQL | `insight-center-dev-erm-graphql-services` |
| `Core` | GraphQL | `insight-center-dev-core-graphql-services` |
| `Platform_API_Test` | GraphQL | `insight-center-dev-graphql-user-management-services` |
| `Batches` | GraphQL | `insight-center-dev-batches-graphql-services` *(URL TBD)* |

### PO Adherence cases (15 cases — all `Purchase_Order` / `GET`)

| Case ID | TC | Endpoint | What it covers |
|---------|----|----------|----------------|
| `po_adh_sdn_status_fetch` | TC-11463 | `GET /api/v1/purchase-order/batches` | `sdn_status` field is null or active |
| `po_adh_invalid_tag_stage_negative` | TC-11464 | `GET /api/v1/purchase-order/batches` | Negative — invalid Tag/Stage, 200 with null data |
| `po_adh_optimized_response_time` | TC-11160 | `GET /api/v1/purchase-order/batches` | Response time < 30 s for bulk 50-record page |
| `po_adh_batch_stages_all_params` | TC-11276 | `GET /api/v2/purchase-order/batch-stages` | All params: OPU + Brand + site_type + stage_code |
| `po_adh_multiple_status_filter` | TC-11277 | `GET /api/v1/purchase-order/batches` | `batch_status=urgent,in_progress` multi-value |
| `po_adh_count_by_batch` | TC-11279 step 1 | `GET /api/v1/purchase-order/batches` | `count_status_by=batch` — only batch counts populated |
| `po_adh_count_by_material` | TC-11279 step 2 | `GET /api/v1/purchase-order/batches` | `count_status_by=material` — only material counts |
| `po_adh_count_by_batch_and_material` | TC-11279 step 3 | `GET /api/v1/purchase-order/batches` | `count_status_by=batch,material` — both counts |
| `po_adh_batch_stages_count_by_material` | TC-11284 | `GET /api/v2/purchase-order/batch-stages` | batch-stages with `count_status_by=material` |
| `po_adh_date_range_plant_rank_supply_model` | TC-11299 | `GET /api/v1/purchase-order/batches` | plant rank, WOC, PSS, `supply_model_category_code` |
| `po_adh_event_ids_in_plants` | TC-11300 | `GET /api/v1/purchase-order/batches` | `id` field present in `plants.events` |
| `po_adh_pss_and_woc_values` | TC-11301 | `GET /api/v1/purchase-order/batches` | `planned_safety_stock_weeks` + `current_weeks_of_coverage` |
| `po_adh_forecast_grpt_pdt_values` | TC-11302 | `GET /api/v1/purchase-order/batches` | `forecast_next_6_weeks_avg`, PDT, GRPT fields |
| `po_adh_smart_search` | TC-11303 | `GET /api/v2/purchase-order/batch-stages` | Smart search requirements |
| `po_adh_api_documentation_openapi` | TC-11305 | `GET /openapi.json` | Documentation comments present in OpenAPI spec |

---

## Load test workflow in detail

The load test feature runs each case **N times sequentially** and records latency per run.
The *representative response* (used for response diffing) is taken from the run whose
latency is closest to the p50 value, avoiding outlier responses skewing comparisons.

**Latency regression threshold** (default 20 %):

> `candidate p95 > GT p95 × 1.20` → flagged as `latency_regression`

**Comparison outcomes:**

| Outcome | Meaning |
|---------|---------|
| `passed` | Latency within threshold, response unchanged |
| `latency_regression` | p95 exceeded threshold, response unchanged |
| `response_changed` | Response drifted, latency within threshold |
| `both_changed` | Both latency regression and response drift |
| `error` | No successful runs in the candidate |
| `missing_gt` | Case exists in candidate but not in the GT file |

**End-to-end example:**

```bash
# 1. Capture GT on release/dev-new
python dashboard_automation.py run \
  --label release-dev-new \
  --load-test --load-test-runs 10

# 2. Note the output folder, e.g.:
#    output/20260415T120000Z__release-dev-new/

# 3. Test a feature branch (same env, same cases)
python dashboard_automation.py run \
  --label BICN-136017-sdn-status \
  --baseline output/20260415T120000Z__release-dev-new/run_results.json \
  --load-test \
  --load-test-gt output/20260415T120000Z__release-dev-new/load_test_gt.json \
  --latency-threshold 20 \
  --fail-on-diff
```

---

## Comparison rules

- Responses are normalised before comparison: keys are sorted, `__typename` fields
  and any paths listed in `ignore_paths` / `ignore_field_names` per case are stripped.
- The primary pass condition is an exact match of the normalised response hash.
- HTTP status must also match.
- Workbook mutations are cataloged but disabled by default to prevent state drift.

---

## Adding new test cases

Edit `test_catalog.json` — add an entry to `manual_cases`:

```json
{
  "case_id": "po_adh_my_new_case",
  "name": "PO Adherence - my new scenario",
  "service": "insight-center-purchase-order-services",
  "protocol": "rest",
  "method": "GET",
  "endpoint": "/api/v1/purchase-order/batches?brand_name=ADVATE&page=1&limit=10",
  "enabled": true,
  "body": null,
  "headers": {},
  "timeout_seconds": 60,
  "expected_status": 200,
  "tags": ["rest", "functional", "purchase-order", "po-adherence"],
  "sources": ["TC-XXXXX"],
  "execution_mode": "regression",
  "notes": "What this case validates.",
  "ignore_paths": [],
  "ignore_field_names": []
}
```

The `service` value must match a `service_id` in the `services` array.

---

## Notes

- The `Batches` GraphQL service base URL is `https://api-insights-dev2.takeda.io/batches/graphql` (confirmed). Its placeholder case is disabled until real batch IDs are provided.
- `--env` rewrites only URLs matching `api-insights-*.takeda.io`. Custom URLs set via environment variables (e.g. `PURCHASE_ORDER_BASE_URL`) take full precedence and are not rewritten.
- To add more workbook sheet imports, extend `workbook_imports` in `test_catalog.json`.
