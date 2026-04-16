from __future__ import annotations

from automation.models import (
    ComparisonResult,
    EnvCompareLoadResult,
    EnvCompareResult,
    LoadTestComparison,
    LoadTestStats,
    RunResult,
)
from automation.normalize import build_diffs, compare_schemas, extract_schema


def compare_runs(candidate_results: list[RunResult], baseline_results: list[RunResult]) -> list[ComparisonResult]:
    baseline_by_case = {item.case_id: item for item in baseline_results}
    comparisons: list[ComparisonResult] = []

    for candidate in candidate_results:
        baseline = baseline_by_case.get(candidate.case_id)
        if baseline is None:
            comparisons.append(
                ComparisonResult(
                    case_id=candidate.case_id,
                    name=candidate.name,
                    service=candidate.service,
                    service_alias=candidate.service_alias,
                    outcome="missing_baseline",
                    baseline_status=None,
                    candidate_status=candidate.status,
                    baseline_http_status=None,
                    candidate_http_status=candidate.actual_status,
                    baseline_hash=None,
                    candidate_hash=candidate.response_hash,
                    elapsed_ms=candidate.elapsed_ms,
                    diff_count=1,
                    diffs=["Baseline snapshot missing for this case."],
                    baseline_error=None,
                    candidate_error=candidate.error,
                    baseline_url=None,
                    candidate_url=candidate.url,
                    request_body=candidate.request_body,
                    candidate_response=candidate.response_json or candidate.response_text,
                    baseline_response=None,
                    response_text=candidate.response_text,
                    sources=candidate.sources,
                    tags=candidate.tags,
                    notes=candidate.notes,
                    execution_mode=candidate.execution_mode,
                )
            )
            continue

        diffs: list[str] = []
        outcome = "passed"
        if baseline.actual_status != candidate.actual_status:
            diffs.append(f"HTTP status mismatch baseline={baseline.actual_status} candidate={candidate.actual_status}")
        if baseline.response_hash != candidate.response_hash:
            diffs.extend(build_diffs(candidate.normalized_response, baseline.normalized_response))
        if baseline.status == "error" or candidate.status == "error":
            outcome = "error"
        elif diffs or baseline.status != "passed" or candidate.status != "passed":
            outcome = "failed"

        comparisons.append(
            ComparisonResult(
                case_id=candidate.case_id,
                name=candidate.name,
                service=candidate.service,
                service_alias=candidate.service_alias,
                outcome=outcome,
                baseline_status=baseline.status,
                candidate_status=candidate.status,
                baseline_http_status=baseline.actual_status,
                candidate_http_status=candidate.actual_status,
                baseline_hash=baseline.response_hash,
                candidate_hash=candidate.response_hash,
                elapsed_ms=candidate.elapsed_ms,
                diff_count=len(diffs),
                diffs=diffs,
                baseline_error=baseline.error,
                candidate_error=candidate.error,
                baseline_url=baseline.url,
                candidate_url=candidate.url,
                request_body=candidate.request_body,
                candidate_response=candidate.response_json or candidate.response_text,
                baseline_response=baseline.response_json or baseline.response_text,
                response_text=candidate.response_text,
                sources=candidate.sources,
                tags=candidate.tags,
                notes=candidate.notes,
                execution_mode=candidate.execution_mode,
            )
        )

    known_candidate_ids = {item.case_id for item in candidate_results}
    for baseline in baseline_results:
        if baseline.case_id in known_candidate_ids:
            continue
        comparisons.append(
            ComparisonResult(
                case_id=baseline.case_id,
                name=baseline.name,
                service=baseline.service,
                service_alias=baseline.service_alias,
                outcome="missing_candidate",
                baseline_status=baseline.status,
                candidate_status="missing",
                baseline_http_status=baseline.actual_status,
                candidate_http_status=None,
                baseline_hash=baseline.response_hash,
                candidate_hash=None,
                elapsed_ms=None,
                diff_count=1,
                diffs=["Candidate run did not execute this baseline case."],
                baseline_error=baseline.error,
                candidate_error=None,
                baseline_url=baseline.url,
                candidate_url="",
                request_body=baseline.request_body,
                candidate_response=None,
                baseline_response=baseline.response_json or baseline.response_text,
                response_text=None,
                sources=baseline.sources,
                tags=baseline.tags,
                notes=baseline.notes,
                execution_mode=baseline.execution_mode,
            )
        )

    return sorted(comparisons, key=lambda item: (item.service_alias, item.case_id))


# ---------------------------------------------------------------------------
# Env-compare — dev2 vs test2 direct comparison
# ---------------------------------------------------------------------------

def _env_compare_latency(
    dev_elapsed_ms: int | None,
    test_elapsed_ms: int | None,
    threshold_pct: float,
) -> tuple[float | None, bool]:
    """Return (delta_pct, within_threshold).  Positive = dev is slower than test."""
    if dev_elapsed_ms is None or test_elapsed_ms is None or test_elapsed_ms <= 0:
        return None, True
    delta_pct = round(((dev_elapsed_ms - test_elapsed_ms) / test_elapsed_ms) * 100, 1)
    return delta_pct, delta_pct <= threshold_pct


def compare_env_pair(
    dev_result: RunResult,
    test_result: RunResult,
    dev_env: str = "dev2",
    test_env: str = "test2",
    latency_threshold_pct: float = 20.0,
) -> EnvCompareResult:
    """Produce an :class:`EnvCompareResult` from a dev/test :class:`RunResult` pair."""
    # ---- Status ----
    status_match = dev_result.actual_status == test_result.actual_status

    # ---- Structural check (test2 is reference) ----
    structural_diffs: list[str] = []
    structural_match = True
    if test_result.normalized_response is not None:
        if dev_result.normalized_response is None:
            structural_diffs = ["dev2 returned no parseable response body"]
            structural_match = False
        else:
            test_schema = extract_schema(test_result.normalized_response)
            dev_schema = extract_schema(dev_result.normalized_response)
            structural_diffs = compare_schemas(test_schema, dev_schema)
            structural_match = len(structural_diffs) == 0

    # ---- Latency ----
    latency_delta_pct, latency_within_threshold = _env_compare_latency(
        dev_result.elapsed_ms, test_result.elapsed_ms, latency_threshold_pct
    )

    # ---- Outcome ----
    if dev_result.status == "skipped" and test_result.status == "skipped":
        outcome = "skipped"
    elif dev_result.status == "error" and test_result.status != "error":
        outcome = "dev_failed"
    elif test_result.status == "error" and dev_result.status != "error":
        outcome = "test_env_error"
    elif dev_result.status == "error" and test_result.status == "error":
        outcome = "error"
    elif not status_match:
        outcome = "status_mismatch"
    elif not structural_match:
        outcome = "structure_mismatch"
    elif not latency_within_threshold:
        outcome = "latency_regression"
    else:
        outcome = "passed"

    return EnvCompareResult(
        case_id=dev_result.case_id,
        name=dev_result.name,
        service=dev_result.service,
        service_alias=dev_result.service_alias,
        method=dev_result.method,
        protocol=dev_result.protocol,
        request_body=dev_result.request_body,
        dev_env=dev_env,
        test_env=test_env,
        dev_url=dev_result.url,
        dev_status=dev_result.actual_status,
        dev_elapsed_ms=dev_result.elapsed_ms,
        dev_error=dev_result.error,
        dev_response_json=dev_result.response_json,
        dev_response_hash=dev_result.response_hash,
        test_url=test_result.url,
        test_status=test_result.actual_status,
        test_elapsed_ms=test_result.elapsed_ms,
        test_error=test_result.error,
        test_response_json=test_result.response_json,
        test_response_hash=test_result.response_hash,
        status_match=status_match,
        structural_match=structural_match,
        latency_delta_pct=latency_delta_pct,
        latency_within_threshold=latency_within_threshold,
        structural_diffs=structural_diffs,
        outcome=outcome,
        sources=dev_result.sources,
        tags=dev_result.tags,
        notes=dev_result.notes,
        execution_mode=dev_result.execution_mode,
        captured_at=dev_result.captured_at,
    )


def compare_env_pairs(
    pairs: list[tuple[RunResult, RunResult]],
    dev_env: str = "dev2",
    test_env: str = "test2",
    latency_threshold_pct: float = 20.0,
) -> list[EnvCompareResult]:
    """Convert a list of ``(dev_result, test_result)`` pairs into comparison results."""
    results = [
        compare_env_pair(dev, test, dev_env, test_env, latency_threshold_pct)
        for dev, test in pairs
    ]
    return sorted(results, key=lambda r: (r.service_alias, r.case_id))


def compare_env_load_pairs(
    pairs: list[tuple[LoadTestStats, LoadTestStats]],
    dev_env: str = "dev2",
    test_env: str = "test2",
    latency_threshold_pct: float = 20.0,
) -> list[EnvCompareLoadResult]:
    """Convert ``(dev_stats, test_stats)`` pairs into load-test comparison results."""
    results: list[EnvCompareLoadResult] = []

    for dev, test in pairs:
        # Latency delta (p95-based, test2 is baseline)
        latency_delta_pct, latency_within_threshold = _env_compare_latency(
            dev.p95_ms, test.p95_ms, latency_threshold_pct
        )

        # Response structure check
        structural_diffs: list[str] = []
        response_match = dev.response_hash == test.response_hash
        if not response_match:
            if test.normalized_response is not None and dev.normalized_response is not None:
                test_schema = extract_schema(test.normalized_response)
                dev_schema = extract_schema(dev.normalized_response)
                structural_diffs = compare_schemas(test_schema, dev_schema)
            elif test.normalized_response is not None:
                structural_diffs = ["dev2 has no representative response to compare"]

        # Outcome
        if dev.success_count == 0 and test.success_count > 0:
            outcome = "dev_failed"
        elif test.success_count == 0 and dev.success_count > 0:
            outcome = "test_env_error"
        elif dev.success_count == 0 and test.success_count == 0:
            outcome = "error"
        elif not latency_within_threshold and structural_diffs:
            outcome = "both_changed"
        elif not latency_within_threshold:
            outcome = "latency_regression"
        elif structural_diffs:
            outcome = "response_changed"
        else:
            outcome = "passed"

        results.append(
            EnvCompareLoadResult(
                case_id=dev.case_id,
                name=dev.name,
                service=dev.service,
                service_alias=dev.service_alias,
                method=dev.method,
                protocol=dev.protocol,
                request_body=None,
                dev_env=dev_env,
                test_env=test_env,
                dev_url=dev.url,
                dev_runs=dev.runs,
                dev_success_count=dev.success_count,
                dev_error_count=dev.error_count,
                dev_min_ms=dev.min_ms,
                dev_max_ms=dev.max_ms,
                dev_avg_ms=dev.avg_ms,
                dev_p50_ms=dev.p50_ms,
                dev_p90_ms=dev.p90_ms,
                dev_p95_ms=dev.p95_ms,
                dev_response_hash=dev.response_hash,
                dev_error_samples=dev.error_samples,
                test_url=test.url,
                test_runs=test.runs,
                test_success_count=test.success_count,
                test_error_count=test.error_count,
                test_min_ms=test.min_ms,
                test_max_ms=test.max_ms,
                test_avg_ms=test.avg_ms,
                test_p50_ms=test.p50_ms,
                test_p90_ms=test.p90_ms,
                test_p95_ms=test.p95_ms,
                test_response_hash=test.response_hash,
                test_error_samples=test.error_samples,
                latency_delta_pct=latency_delta_pct,
                latency_within_threshold=latency_within_threshold,
                response_match=response_match,
                structural_diffs=structural_diffs,
                outcome=outcome,
                sources=dev.sources,
                tags=dev.tags,
                notes=dev.notes,
                execution_mode=dev.execution_mode,
                captured_at=dev.captured_at,
            )
        )

    return sorted(results, key=lambda r: (r.service_alias, r.case_id))


# ---------------------------------------------------------------------------
# Load test comparison
# ---------------------------------------------------------------------------

def compare_load_tests(
    candidate: list[LoadTestStats],
    gt: list[LoadTestStats],
    latency_threshold_pct: float = 20.0,
) -> list[LoadTestComparison]:
    """Compare candidate load-test stats against GT stats.

    A *latency regression* is flagged when the candidate p95 exceeds the GT
    p95 by more than ``latency_threshold_pct`` percent.
    A *response change* is flagged when the representative response hashes
    differ between candidate and GT.
    """
    gt_by_case = {item.case_id: item for item in gt}
    comparisons: list[LoadTestComparison] = []

    for cand in candidate:
        baseline = gt_by_case.get(cand.case_id)

        if baseline is None:
            comparisons.append(
                LoadTestComparison(
                    case_id=cand.case_id,
                    name=cand.name,
                    service=cand.service,
                    service_alias=cand.service_alias,
                    gt_avg_ms=None,
                    candidate_avg_ms=cand.avg_ms,
                    gt_p50_ms=None,
                    candidate_p50_ms=cand.p50_ms,
                    gt_p90_ms=None,
                    candidate_p90_ms=cand.p90_ms,
                    gt_p95_ms=None,
                    candidate_p95_ms=cand.p95_ms,
                    gt_min_ms=None,
                    candidate_min_ms=cand.min_ms,
                    gt_max_ms=None,
                    candidate_max_ms=cand.max_ms,
                    latency_delta_pct=None,
                    latency_regression=False,
                    gt_hash=None,
                    candidate_hash=cand.response_hash,
                    response_changed=False,
                    diffs=["GT snapshot missing for this case."],
                    gt_runs=0,
                    candidate_runs=cand.runs,
                    gt_success_count=0,
                    candidate_success_count=cand.success_count,
                    outcome="missing_gt",
                    sources=cand.sources,
                    tags=cand.tags,
                    notes=cand.notes,
                    execution_mode=cand.execution_mode,
                )
            )
            continue

        # --- Latency regression check (based on p95) ---
        latency_delta_pct: float | None = None
        latency_regression = False
        if cand.p95_ms is not None and baseline.p95_ms is not None and baseline.p95_ms > 0:
            latency_delta_pct = round(
                ((cand.p95_ms - baseline.p95_ms) / baseline.p95_ms) * 100, 1
            )
            latency_regression = latency_delta_pct > latency_threshold_pct

        # --- Response change check ---
        response_changed = cand.response_hash != baseline.response_hash
        diffs: list[str] = []
        if response_changed and cand.normalized_response is not None and baseline.normalized_response is not None:
            diffs = build_diffs(cand.normalized_response, baseline.normalized_response)
        elif response_changed:
            diffs = ["Response hash changed (no normalized data available for diff)."]

        # --- Overall outcome ---
        if cand.success_count == 0:
            outcome = "error"
        elif latency_regression and response_changed:
            outcome = "both_changed"
        elif latency_regression:
            outcome = "latency_regression"
        elif response_changed:
            outcome = "response_changed"
        else:
            outcome = "passed"

        comparisons.append(
            LoadTestComparison(
                case_id=cand.case_id,
                name=cand.name,
                service=cand.service,
                service_alias=cand.service_alias,
                gt_avg_ms=baseline.avg_ms,
                candidate_avg_ms=cand.avg_ms,
                gt_p50_ms=baseline.p50_ms,
                candidate_p50_ms=cand.p50_ms,
                gt_p90_ms=baseline.p90_ms,
                candidate_p90_ms=cand.p90_ms,
                gt_p95_ms=baseline.p95_ms,
                candidate_p95_ms=cand.p95_ms,
                gt_min_ms=baseline.min_ms,
                candidate_min_ms=cand.min_ms,
                gt_max_ms=baseline.max_ms,
                candidate_max_ms=cand.max_ms,
                latency_delta_pct=latency_delta_pct,
                latency_regression=latency_regression,
                gt_hash=baseline.response_hash,
                candidate_hash=cand.response_hash,
                response_changed=response_changed,
                diffs=diffs,
                gt_runs=baseline.runs,
                candidate_runs=cand.runs,
                gt_success_count=baseline.success_count,
                candidate_success_count=cand.success_count,
                outcome=outcome,
                sources=cand.sources,
                tags=cand.tags,
                notes=cand.notes,
                execution_mode=cand.execution_mode,
            )
        )

    return sorted(comparisons, key=lambda item: (item.service_alias, item.case_id))
