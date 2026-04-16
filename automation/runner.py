from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from automation.io_utils import iso_now, load_tester_gt, slugify, write_json, write_text
from automation.models import LoadTestStats, RunResult, ServiceConfig, TestCase
from automation.normalize import normalize_payload, response_hash


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def replace_env_in_url(url: str, target_env: str) -> str:
    """Swap the environment segment in Takeda insight-center URLs.

    Transforms ``https://api-insights-dev2.takeda.io/...`` into
    ``https://api-insights-<target_env>.takeda.io/...``.
    URLs that do not match the pattern are returned unchanged.
    """
    return re.sub(
        r"(?<=api-insights-)([a-z][a-z0-9]*)(?=\.takeda\.io)",
        target_env,
        url,
    )


# ---------------------------------------------------------------------------
# URL / header construction
# ---------------------------------------------------------------------------

def case_url(case: TestCase, service: ServiceConfig, env: dict[str, str], target_env: str = "dev2") -> str:
    endpoint = case.endpoint.strip()
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return replace_env_in_url(endpoint, target_env)
    base_url = env.get(service.base_url_env, "").strip() if service.base_url_env else ""
    if not base_url:
        base_url = service.base_url.strip()
    if not base_url:
        return endpoint
    base_url = replace_env_in_url(base_url, target_env)
    # When endpoint is empty (GraphQL cases), return base_url exactly as the
    # catalog defines it.  Do NOT add an extra "/" via the f-string below —
    # that trailing slash causes HTTP 307 redirects that urllib will not follow
    # for POST requests.  Do NOT rstrip either — some services (e.g. ERM
    # GraphQL) define a trailing slash in base_url that their server requires
    # (removing it returns 404).
    if not endpoint:
        return base_url
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def merged_headers(case: TestCase, env: dict[str, str]) -> dict[str, str]:
    headers = {
        "Authorization": env.get("AUTHORIZATION", ""),
        "App_auth_token": env.get("APP_AUTH_TOKEN", ""),
        "Accept": "application/json",
    }
    if case.protocol == "graphql" or case.method in {"POST", "PUT", "PATCH", "DELETE"}:
        headers["Content-Type"] = "application/json"
    headers.update(case.headers)
    return {key: value for key, value in headers.items() if value}


# ---------------------------------------------------------------------------
# Single-run execution
# ---------------------------------------------------------------------------

def build_skipped_result(case: TestCase, service: ServiceConfig, reason: str, started_at: str) -> RunResult:
    return RunResult(
        case_id=case.case_id,
        name=case.name,
        service=case.service,
        service_alias=service.alias,
        url="",
        method=case.method,
        protocol=case.protocol,
        enabled=case.enabled,
        executed=False,
        status="skipped",
        expected_status=case.expected_status,
        actual_status=None,
        elapsed_ms=None,
        error=reason,
        request_body=case.body,
        response_headers={},
        response_json=None,
        response_text=None,
        normalized_response=None,
        response_hash=None,
        sources=case.sources,
        tags=case.tags,
        notes=case.notes,
        execution_mode=case.execution_mode,
        captured_at=started_at,
    )


def execute_case(
    case: TestCase,
    service: ServiceConfig,
    env: dict[str, str],
    started_at: str,
    target_env: str = "dev2",
) -> RunResult:
    url = case_url(case, service, env, target_env)
    headers = merged_headers(case, env)
    request_body = case.body
    encoded_body: bytes | None = None
    if case.protocol == "graphql" and isinstance(case.body, str):
        request_body = {"query": case.body}
    if request_body is not None and case.method in {"POST", "PUT", "PATCH", "DELETE"}:
        encoded_body = json.dumps(request_body).encode("utf-8")

    response_headers: dict[str, str] = {}
    response_text: str | None = None
    parsed_response: Any = None
    actual_status: int | None = None
    error_message: str | None = None
    started = time.perf_counter()

    try:
        request = urllib.request.Request(url=url, data=encoded_body, headers=headers, method=case.method)
        with urllib.request.urlopen(request, timeout=case.timeout_seconds) as response:
            actual_status = response.status
            response_headers = dict(response.headers.items())
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        actual_status = exc.code
        response_headers = dict(exc.headers.items()) if exc.headers else {}
        response_text = exc.read().decode("utf-8", errors="replace")
        error_message = f"HTTPError {exc.code}"
    except Exception as exc:  # noqa: BLE001
        error_message = str(exc)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    if response_text:
        try:
            parsed_response = json.loads(response_text)
        except json.JSONDecodeError:
            parsed_response = None

    normalized = None
    if parsed_response is not None or response_text is not None:
        normalized = normalize_payload(
            parsed_response if parsed_response is not None else response_text,
            case.ignore_paths,
            set(case.ignore_field_names),
        )

    status = "passed"
    if error_message and actual_status is None:
        status = "error"
    elif actual_status != case.expected_status:
        status = "failed"

    return RunResult(
        case_id=case.case_id,
        name=case.name,
        service=case.service,
        service_alias=service.alias,
        url=url,
        method=case.method,
        protocol=case.protocol,
        enabled=case.enabled,
        executed=True,
        status=status,
        expected_status=case.expected_status,
        actual_status=actual_status,
        elapsed_ms=elapsed_ms,
        error=error_message,
        request_body=request_body,
        response_headers=response_headers,
        response_json=parsed_response,
        response_text=response_text,
        normalized_response=normalized,
        response_hash=response_hash(normalized),
        sources=case.sources,
        tags=case.tags,
        notes=case.notes,
        execution_mode=case.execution_mode,
        captured_at=started_at,
    )


# ---------------------------------------------------------------------------
# Regression run (single pass per case)
# ---------------------------------------------------------------------------

def run_capture(
    cases: list[TestCase],
    services: dict[str, ServiceConfig],
    env: dict[str, str],
    target_env: str = "dev2",
) -> list[RunResult]:
    started_at = iso_now()
    results: list[RunResult] = []
    for case in cases:
        service = services[case.service]
        if not case.enabled:
            results.append(build_skipped_result(case, service, "Case is disabled in catalog.", started_at))
            continue
        url = case_url(case, service, env, target_env)
        if not url.startswith(("http://", "https://")):
            results.append(build_skipped_result(case, service, "Service base URL is not configured.", started_at))
            continue
        results.append(execute_case(case, service, env, started_at, target_env))
    return results


def persist_run_artifacts(root: Path, results: list[RunResult], namespace: str) -> None:
    bucket_root = root / namespace
    for result in results:
        case_dir = bucket_root / slugify(result.service_alias) / result.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        write_json(case_dir / "result.json", result.__dict__)
        write_json(
            case_dir / "request.json",
            {
                "case_id": result.case_id,
                "url": result.url,
                "method": result.method,
                "protocol": result.protocol,
                "request_body": result.request_body,
            },
        )
        if result.response_json is not None:
            write_json(case_dir / "response.json", result.response_json)
        elif result.response_text is not None:
            write_text(case_dir / "response.txt", result.response_text)
        if result.normalized_response is not None:
            write_json(case_dir / "normalized.json", result.normalized_response)


# ---------------------------------------------------------------------------
# Load test helpers
# ---------------------------------------------------------------------------

def compute_stats(values: list[int]) -> dict[str, Any]:
    """Return min/max/avg/p50/p90/p95 for a list of latency measurements (ms)."""
    if not values:
        return {
            "min_ms": None,
            "max_ms": None,
            "avg_ms": None,
            "p50_ms": None,
            "p90_ms": None,
            "p95_ms": None,
        }
    sorted_v = sorted(values)
    n = len(sorted_v)

    def _pct(p: float) -> int:
        idx = max(0, min(n - 1, int(math.ceil(p / 100.0 * n)) - 1))
        return sorted_v[idx]

    return {
        "min_ms": sorted_v[0],
        "max_ms": sorted_v[-1],
        "avg_ms": round(sum(sorted_v) / n, 1),
        "p50_ms": _pct(50),
        "p90_ms": _pct(90),
        "p95_ms": _pct(95),
    }


# ---------------------------------------------------------------------------
# Load test helpers — single case, N runs
# ---------------------------------------------------------------------------

def _run_case_n_times(
    case: TestCase,
    service: ServiceConfig,
    env: dict[str, str],
    runs: int,
    started_at: str,
    target_env: str,
) -> LoadTestStats:
    """Execute *case* against *target_env* exactly *runs* times and aggregate stats.

    The representative response (used for hash comparison) is the run whose
    elapsed_ms is closest to p50, so a single outlier does not skew it.
    """
    run_results: list[RunResult] = []
    for _ in range(runs):
        result = execute_case(case, service, env, started_at, target_env)
        run_results.append(result)

    successful = [r for r in run_results if r.elapsed_ms is not None and r.status != "error"]
    errors = [r for r in run_results if r.status == "error"]

    elapsed_values = [r.elapsed_ms for r in successful]  # type: ignore[misc]
    error_samples = [r.error for r in errors if r.error]

    stats = compute_stats(elapsed_values)

    rep: RunResult | None = None
    if successful:
        p50 = stats["p50_ms"] or 0
        rep = min(successful, key=lambda r: abs((r.elapsed_ms or 0) - p50))

    url = case_url(case, service, env, target_env)
    return LoadTestStats(
        case_id=case.case_id,
        name=case.name,
        service=case.service,
        service_alias=service.alias,
        url=url,
        method=case.method,
        protocol=case.protocol,
        runs=runs,
        success_count=len(successful),
        error_count=len(errors),
        min_ms=stats["min_ms"],
        max_ms=stats["max_ms"],
        avg_ms=stats["avg_ms"],
        p50_ms=stats["p50_ms"],
        p90_ms=stats["p90_ms"],
        p95_ms=stats["p95_ms"],
        raw_elapsed_ms=elapsed_values,
        response_hash=rep.response_hash if rep else None,
        normalized_response=rep.normalized_response if rep else None,
        error_samples=error_samples[:5],
        sources=case.sources,
        tags=case.tags,
        notes=case.notes,
        execution_mode=case.execution_mode,
        captured_at=started_at,
    )


# ---------------------------------------------------------------------------
# Load test run (N passes per case)
# ---------------------------------------------------------------------------

def run_load_test(
    cases: list[TestCase],
    services: dict[str, ServiceConfig],
    env: dict[str, str],
    runs: int = 10,
    target_env: str = "dev2",
) -> list[LoadTestStats]:
    """Execute every enabled case *runs* times and return aggregated latency stats."""
    started_at = iso_now()
    stats_list: list[LoadTestStats] = []

    for case in cases:
        service = services[case.service]
        if not case.enabled:
            continue
        url = case_url(case, service, env, target_env)
        if not url.startswith(("http://", "https://")):
            continue
        stats_list.append(_run_case_n_times(case, service, env, runs, started_at, target_env))

    return stats_list


# ---------------------------------------------------------------------------
# Env-compare — run each case against two environments concurrently
# ---------------------------------------------------------------------------

def _ms(v: int | None) -> str:
    return f"{v}ms" if v is not None else "n/a"


def _status_icon(status: int | None, expected: int = 200) -> str:
    if status is None:
        return "ERR"
    return str(status) if status == expected else f"{status}✗"


def run_env_compare(
    cases: list[TestCase],
    services: dict[str, ServiceConfig],
    env: dict[str, str],
    dev_env: str = "dev2",
    test_env: str = "test2",
) -> list[tuple[RunResult, RunResult]]:
    """Run each enabled case against *dev_env* and *test_env* concurrently.

    Returns a list of ``(dev_result, test_result)`` pairs in case order.
    Disabled or un-routable cases are included as skipped pairs so the caller
    can still produce a complete comparison table.
    """
    started_at = iso_now()
    pairs: list[tuple[RunResult, RunResult]] = []
    total = len(cases)
    print(f"  Single-pass: {total} cases", flush=True)

    for idx, case in enumerate(cases, 1):
        service = services[case.service]
        prefix = f"  [{idx:>3}/{total}] {service.alias:<16} {case.name[:38]:<38}"
        if not case.enabled:
            print(f"{prefix}  skipped (disabled)", flush=True)
            skipped = build_skipped_result(case, service, "Case is disabled in catalog.", started_at)
            pairs.append((skipped, skipped))
            continue
        dev_url = case_url(case, service, env, dev_env)
        test_url = case_url(case, service, env, test_env)
        if not dev_url.startswith(("http://", "https://")) or not test_url.startswith(("http://", "https://")):
            print(f"{prefix}  skipped (no base URL)", flush=True)
            skipped = build_skipped_result(case, service, "Service base URL is not configured.", started_at)
            pairs.append((skipped, skipped))
            continue

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_dev = pool.submit(execute_case, case, service, env, started_at, dev_env)
            future_test = pool.submit(execute_case, case, service, env, started_at, test_env)
            dev_result = future_dev.result()
            test_result = future_test.result()

        dev_cell = f"{_status_icon(dev_result.actual_status, case.expected_status)} ({_ms(dev_result.elapsed_ms)})"
        test_cell = f"{_status_icon(test_result.actual_status, case.expected_status)} ({_ms(test_result.elapsed_ms)})"
        print(f"{prefix}  {dev_env}: {dev_cell:<18}  {test_env}: {test_cell}", flush=True)
        pairs.append((dev_result, test_result))

    return pairs


def run_env_compare_load_test(
    cases: list[TestCase],
    services: dict[str, ServiceConfig],
    env: dict[str, str],
    runs: int = 5,
    dev_env: str = "dev2",
    test_env: str = "test2",
) -> list[tuple[LoadTestStats, LoadTestStats]]:
    """Run a load test for each enabled case against both environments concurrently.

    For each case, *runs* HTTP calls are made to *dev_env* and *runs* to
    *test_env* at the same time (2 threads per case, cases are sequential).

    Returns a list of ``(dev_stats, test_stats)`` pairs.
    """
    started_at = iso_now()
    pairs: list[tuple[LoadTestStats, LoadTestStats]] = []
    enabled_cases = [
        (case, services[case.service])
        for case in cases
        if case.enabled
        and case_url(case, services[case.service], env, dev_env).startswith(("http://", "https://"))
    ]
    total = len(enabled_cases)
    print(f"  Load test: {total} cases × {runs} runs each env", flush=True)

    for idx, (case, service) in enumerate(enabled_cases, 1):
        prefix = f"  [{idx:>3}/{total}] {service.alias:<16} {case.name[:38]:<38}"
        print(f"{prefix}  running…", end="\r", flush=True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_dev = pool.submit(_run_case_n_times, case, service, env, runs, started_at, dev_env)
            future_test = pool.submit(_run_case_n_times, case, service, env, runs, started_at, test_env)
            dev_stats = future_dev.result()
            test_stats = future_test.result()

        dev_cell = f"{dev_stats.success_count}/{dev_stats.runs} ok  avg {_ms(dev_stats.avg_ms)}  p95 {_ms(dev_stats.p95_ms)}"
        test_cell = f"{test_stats.success_count}/{test_stats.runs} ok  avg {_ms(test_stats.avg_ms)}  p95 {_ms(test_stats.p95_ms)}"
        print(f"{prefix}  {dev_env}: {dev_cell:<34}  {test_env}: {test_cell}", flush=True)
        pairs.append((dev_stats, test_stats))

    return pairs


def tester_gt_to_run_results(
    entries: list[dict],
    cases: list[TestCase],
    services: dict[str, ServiceConfig],
) -> list[RunResult]:
    """Convert tester-supplied GT entries into synthetic RunResult objects.

    Each entry must have ``case_id`` and ``expected_status``.
    ``expected_response`` is optional — when absent only the HTTP status is
    checked during comparison.
    """
    cases_by_id = {c.case_id: c for c in cases}
    now = iso_now()
    results: list[RunResult] = []

    for entry in entries:
        case_id = entry.get("case_id", "").strip()
        if not case_id:
            continue
        case = cases_by_id.get(case_id)
        service_alias = (
            services[case.service].alias
            if case and case.service in services
            else "Unknown"
        )
        expected_status = int(entry.get("expected_status", 200))
        expected_response = entry.get("expected_response")

        normalized = None
        if expected_response is not None:
            ignore_paths = case.ignore_paths if case else []
            ignore_field_names = set(case.ignore_field_names) if case else set()
            normalized = normalize_payload(expected_response, ignore_paths, ignore_field_names)

        results.append(
            RunResult(
                case_id=case_id,
                name=case.name if case else case_id,
                service=case.service if case else "",
                service_alias=service_alias,
                url=case.endpoint if case else "",
                method=case.method if case else "GET",
                protocol=case.protocol if case else "rest",
                enabled=True,
                executed=True,
                status="passed",
                expected_status=expected_status,
                actual_status=expected_status,
                elapsed_ms=None,
                error=None,
                request_body=case.body if case else None,
                response_headers={},
                response_json=expected_response,
                response_text=None,
                normalized_response=normalized,
                response_hash=response_hash(normalized),
                sources=(case.sources if case else []) + ["tester-gt"],
                tags=case.tags if case else [],
                notes=entry.get("notes", case.notes if case else ""),
                execution_mode=case.execution_mode if case else "regression",
                captured_at=now,
            )
        )
    return results


def persist_load_test_artifacts(root: Path, stats: list[LoadTestStats]) -> None:
    """Write per-case load test stats and representative response to disk."""
    bucket = root / "load_test"
    for stat in stats:
        case_dir = bucket / slugify(stat.service_alias) / stat.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        write_json(case_dir / "stats.json", stat.__dict__)
        if stat.normalized_response is not None:
            write_json(case_dir / "representative_response.json", stat.normalized_response)
