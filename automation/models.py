from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceConfig:
    service_id: str
    display_name: str
    alias: str
    kind: str
    base_url: str = ""
    base_url_env: str = ""
    notes: str = ""


@dataclass
class WorkbookImport:
    sheet: str
    service: str
    endpoint_strategy: str = "use_service_base"
    enabled: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    execution_mode: str = "regression"
    notes: str = ""


@dataclass
class TestCase:
    case_id: str
    name: str
    service: str
    protocol: str
    method: str
    endpoint: str
    enabled: bool = True
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 60
    expected_status: int = 200
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    execution_mode: str = "regression"
    notes: str = ""
    ignore_paths: list[str] = field(default_factory=list)
    ignore_field_names: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    case_id: str
    name: str
    service: str
    service_alias: str
    url: str
    method: str
    protocol: str
    enabled: bool
    executed: bool
    status: str
    expected_status: int
    actual_status: int | None
    elapsed_ms: int | None
    error: str | None
    request_body: Any
    response_headers: dict[str, str]
    response_json: Any
    response_text: str | None
    normalized_response: Any
    response_hash: str | None
    sources: list[str]
    tags: list[str]
    notes: str
    execution_mode: str
    captured_at: str


@dataclass
class ComparisonResult:
    case_id: str
    name: str
    service: str
    service_alias: str
    outcome: str
    baseline_status: str | None
    candidate_status: str
    baseline_http_status: int | None
    candidate_http_status: int | None
    baseline_hash: str | None
    candidate_hash: str | None
    elapsed_ms: int | None
    diff_count: int
    diffs: list[str]
    baseline_error: str | None
    candidate_error: str | None
    baseline_url: str | None
    candidate_url: str
    request_body: Any
    candidate_response: Any
    baseline_response: Any
    response_text: str | None
    sources: list[str]
    tags: list[str]
    notes: str
    execution_mode: str


@dataclass
class LoadTestStats:
    """Aggregated latency and response statistics for a single test case run N times."""

    case_id: str
    name: str
    service: str
    service_alias: str
    url: str
    method: str
    protocol: str
    runs: int
    success_count: int
    error_count: int
    # Latency percentiles (ms) — None when no successful runs
    min_ms: int | None
    max_ms: int | None
    avg_ms: float | None
    p50_ms: int | None
    p90_ms: int | None
    p95_ms: int | None
    # All per-run elapsed values for charting / debugging
    raw_elapsed_ms: list[int]
    # Representative response (from the run nearest to p50 latency)
    response_hash: str | None
    normalized_response: Any
    # Up to 5 error messages sampled from failed runs
    error_samples: list[str]
    sources: list[str]
    tags: list[str]
    notes: str
    execution_mode: str
    captured_at: str


@dataclass
class LoadTestComparison:
    """Per-case comparison of a candidate load-test run against the GT snapshot."""

    case_id: str
    name: str
    service: str
    service_alias: str
    # Latency
    gt_avg_ms: float | None
    candidate_avg_ms: float | None
    gt_p50_ms: int | None
    candidate_p50_ms: int | None
    gt_p90_ms: int | None
    candidate_p90_ms: int | None
    gt_p95_ms: int | None
    candidate_p95_ms: int | None
    gt_min_ms: int | None
    candidate_min_ms: int | None
    gt_max_ms: int | None
    candidate_max_ms: int | None
    # Delta based on p95 (positive = candidate is slower)
    latency_delta_pct: float | None
    latency_regression: bool
    # Response
    gt_hash: str | None
    candidate_hash: str | None
    response_changed: bool
    diffs: list[str]
    # Run counts
    gt_runs: int
    candidate_runs: int
    gt_success_count: int
    candidate_success_count: int
    # Overall verdict
    # "passed" | "latency_regression" | "response_changed" | "both_changed" | "error" | "missing_gt"
    outcome: str
    sources: list[str]
    tags: list[str]
    notes: str
    execution_mode: str


# ---------------------------------------------------------------------------
# Env-compare models (dev2 vs test2 direct comparison)
# ---------------------------------------------------------------------------

@dataclass
class EnvCompareResult:
    """Single-pass side-by-side comparison of one case run against two environments."""

    case_id: str
    name: str
    service: str
    service_alias: str
    method: str
    protocol: str
    request_body: Any

    # Environment labels
    dev_env: str
    test_env: str

    # dev environment results
    dev_url: str
    dev_status: int | None
    dev_elapsed_ms: int | None
    dev_error: str | None
    dev_response_json: Any
    dev_response_hash: str | None

    # test environment results
    test_url: str
    test_status: int | None
    test_elapsed_ms: int | None
    test_error: str | None
    test_response_json: Any
    test_response_hash: str | None

    # Comparison verdict
    status_match: bool
    structural_match: bool
    # Positive = dev is slower than test (test2 is baseline per advisor guidance)
    latency_delta_pct: float | None
    latency_within_threshold: bool
    structural_diffs: list[str]

    # Overall outcome
    # "passed" | "status_mismatch" | "structure_mismatch" | "latency_regression"
    # | "dev_failed" | "test_env_error" | "error" | "skipped"
    outcome: str

    sources: list[str]
    tags: list[str]
    notes: str
    execution_mode: str
    captured_at: str


@dataclass
class EnvCompareLoadResult:
    """Load test comparison of one case across dev and test environments (N runs each)."""

    case_id: str
    name: str
    service: str
    service_alias: str
    method: str
    protocol: str
    request_body: Any

    dev_env: str
    test_env: str

    # dev stats
    dev_url: str
    dev_runs: int
    dev_success_count: int
    dev_error_count: int
    dev_min_ms: int | None
    dev_max_ms: int | None
    dev_avg_ms: float | None
    dev_p50_ms: int | None
    dev_p90_ms: int | None
    dev_p95_ms: int | None
    dev_response_hash: str | None
    dev_error_samples: list[str]
    dev_raw_elapsed_ms: list[int]

    # test stats
    test_url: str
    test_runs: int
    test_success_count: int
    test_error_count: int
    test_min_ms: int | None
    test_max_ms: int | None
    test_avg_ms: float | None
    test_p50_ms: int | None
    test_p90_ms: int | None
    test_p95_ms: int | None
    test_response_hash: str | None
    test_error_samples: list[str]
    test_raw_elapsed_ms: list[int]

    # Comparison (test2 is baseline; positive delta = dev is slower)
    latency_delta_pct: float | None
    latency_within_threshold: bool
    response_match: bool
    structural_diffs: list[str]

    # Error rate comparison (positive = dev has higher error rate than test)
    dev_error_rate_pct: float | None
    test_error_rate_pct: float | None
    error_rate_delta_pct: float | None
    concurrency_regression: bool

    # Overall outcome
    # "passed" | "latency_regression" | "response_changed" | "concurrency_regression"
    # | "both_changed" | "dev_failed" | "test_env_error" | "error"
    outcome: str

    sources: list[str]
    tags: list[str]
    notes: str
    execution_mode: str
    captured_at: str
