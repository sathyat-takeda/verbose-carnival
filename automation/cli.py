from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

from automation.catalog import build_workbook_cases, load_catalog, print_case_table, select_cases
from automation.comparator import compare_env_load_pairs, compare_env_pairs, compare_load_tests, compare_runs
from automation.io_utils import load_dotenv, load_tester_gt
from automation.models import LoadTestStats, RunResult, ServiceConfig
from automation.reporter import (
    ensure_output_dir,
    persist_comparison_artifacts,
    persist_service_case_index,
    save_env_compare_reports,
    save_load_test_reports,
    save_reports,
)
from automation.runner import (
    persist_load_test_artifacts,
    persist_run_artifacts,
    run_capture,
    run_env_compare,
    run_env_compare_load_test,
    run_load_test,
    tester_gt_to_run_results,
)


def _demo_results() -> tuple[list[RunResult], list]:
    from automation.io_utils import iso_now
    from automation.normalize import response_hash
    from automation.comparator import compare_runs

    started_at = iso_now()
    baseline = [
        RunResult(
            case_id="core_demo_pass",
            name="Core demo pass",
            service="insight-center-dev-core-graphql-services",
            service_alias="Core",
            url="https://demo/core/graphql/graphql",
            method="POST",
            protocol="graphql",
            enabled=True,
            executed=True,
            status="passed",
            expected_status=200,
            actual_status=200,
            elapsed_ms=120,
            error=None,
            request_body={"query": "{ demo }"},
            response_headers={},
            response_json={"data": {"demo": "same"}},
            response_text='{"data":{"demo":"same"}}',
            normalized_response={"data": {"demo": "same"}},
            response_hash=response_hash({"data": {"demo": "same"}}),
            sources=["demo"],
            tags=["demo"],
            notes="",
            execution_mode="regression",
            captured_at=started_at,
        ),
        RunResult(
            case_id="risk_demo_fail",
            name="Risk demo diff",
            service="insight-center-dev-erm-services",
            service_alias="Risk",
            url="https://demo/risks/api/v1/params/products-list",
            method="GET",
            protocol="rest",
            enabled=True,
            executed=True,
            status="passed",
            expected_status=200,
            actual_status=200,
            elapsed_ms=150,
            error=None,
            request_body=None,
            response_headers={},
            response_json={"data": [{"product_name": "ADVATE"}]},
            response_text='{"data":[{"product_name":"ADVATE"}]}',
            normalized_response={"data": [{"product_name": "ADVATE"}]},
            response_hash=response_hash({"data": [{"product_name": "ADVATE"}]}),
            sources=["demo"],
            tags=["demo"],
            notes="",
            execution_mode="regression",
            captured_at=started_at,
        ),
    ]
    candidate = [baseline[0], RunResult(**{**baseline[1].__dict__, "elapsed_ms": 180, "response_json": {"data": [{"product_name": "ADYNOVATE"}]}, "response_text": '{"data":[{"product_name":"ADYNOVATE"}]}', "normalized_response": {"data": [{"product_name": "ADYNOVATE"}]}, "response_hash": response_hash({"data": [{"product_name": "ADYNOVATE"}]})})]
    return candidate, compare_runs(candidate, baseline)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture baseline API responses and compare later runs against them.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              # List all cases
              python dashboard_automation.py list

              # Capture GT from release/dev-new (dev2, single-pass + load test)
              python dashboard_automation.py run --label release-dev-new --load-test

              # Capture GT on test2 environment
              python dashboard_automation.py run --label release-dev-new --env test2 --load-test

              # Compare a feature branch against the GT (single-pass only)
              python dashboard_automation.py run --label BICN-144957 \\
                  --baseline output/<gt-dir>/run_results.json

              # Compare a feature branch with full load test comparison
              python dashboard_automation.py run --label BICN-144957 \\
                  --baseline output/<gt-dir>/run_results.json \\
                  --load-test \\
                  --load-test-gt output/<gt-dir>/load_test_gt.json

              # Tester-supplied GT — run APIs and compare against hand-crafted expectations
              python dashboard_automation.py run --label BICN-144957 \\
                  --tester-gt my_expected.json \\
                  --baseline-label "tester-approved"

              python dashboard_automation.py run --label BICN-144957 \\
                  --tester-gt my_expected.csv \\
                  --baseline-label "tester-approved"

              # Offline compare — bring your own GT, no API calls
              python dashboard_automation.py compare \\
                  --gt my_gt/run_results.json \\
                  --candidate output/<run-dir>/run_results.json \\
                  --label BICN-144957 \\
                  --baseline-label my-gt

              # Quick demo (no network calls)
              python dashboard_automation.py demo

              # Direct env-compare: dev2 vs test2 (single pass)
              python dashboard_automation.py env-compare --label sprint-42

              # Env-compare with load test (5 runs per case per env)
              python dashboard_automation.py env-compare --label sprint-42 --load-test

              # Env-compare with custom envs and tighter latency threshold
              python dashboard_automation.py env-compare --label sprint-42 \\
                  --dev-env dev2 --test-env test2 \\
                  --latency-threshold 20 \\
                  --load-test --load-test-runs 5
            """
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", default="test_catalog.json", metavar="FILE",
                        help="Path to test_catalog.json (default: test_catalog.json).")
    common.add_argument("--env-file", default=".env", metavar="FILE",
                        help="Path to .env file with AUTHORIZATION / APP_AUTH_TOKEN (default: .env).")
    common.add_argument("--workbook", default="API_Automation_Test_Data.xlsx", metavar="FILE",
                        help="Path to the Excel workbook for workbook_imports (default: API_Automation_Test_Data.xlsx).")
    common.add_argument("--only-service", action="append", default=[], metavar="SERVICE_ID",
                        help="Run only cases belonging to this service ID. Repeatable.")
    common.add_argument("--only-case", action="append", default=[], metavar="CASE_ID",
                        help="Run only the case with this case_id. Repeatable.")
    common.add_argument("--skip-workbook", action="store_true",
                        help="Skip loading cases from the Excel workbook entirely.")
    common.add_argument(
        "--env",
        default="dev2",
        metavar="ENV",
        help=(
            "Target environment name (default: dev2). Replaces the environment "
            "segment in Takeda insight-center URLs "
            "(e.g. api-insights-dev2.takeda.io → api-insights-test2.takeda.io). "
            "Use dev2 for release/dev-new GT capture; use the same env when "
            "running the candidate branch."
        ),
    )

    subparsers.add_parser(
        "list",
        parents=[common],
        help="Print all seeded test cases with their service, method, protocol and enabled state.",
        description=(
            "Print a table of every test case loaded from test_catalog.json and "
            "the workbook. Use --only-service or --only-case to narrow the list."
        ),
    )

    run_parser = subparsers.add_parser(
        "run",
        parents=[common],
        help="Execute live HTTP calls, capture responses, and build HTML dashboards.",
        description=(
            "Run all enabled test cases against the chosen environment (default: dev2). "
            "Without --baseline the output is treated as a Ground Truth (GT) snapshot. "
            "With --baseline the run is treated as a candidate and responses are diffed "
            "against the GT. Add --load-test to repeat each case N times and collect "
            "latency statistics; add --load-test-gt to compare those stats against a "
            "previously captured GT load_test_gt.json."
        ),
    )
    run_parser.add_argument("--label", required=True, help="Short label for this run (e.g. release-dev-new, BICN-12345).")
    run_parser.add_argument("--baseline", help="Path to a previous run_results.json to compare against.")
    run_parser.add_argument(
        "--tester-gt",
        metavar="PATH",
        help=(
            "Path to a tester-supplied GT file (.json or .csv). "
            "The script runs the APIs live then compares actual responses against this GT. "
            "Use instead of --baseline when testers own the expected results. "
            "Cannot be used together with --baseline."
        ),
    )
    run_parser.add_argument("--baseline-label", default="release/dev-new", help="Display label for the baseline / tester GT in dashboards (default: release/dev-new).")
    run_parser.add_argument("--fail-on-diff", action="store_true", help="Exit with code 1 if any comparison diff is found.")
    # Load test options
    run_parser.add_argument(
        "--load-test",
        action="store_true",
        help="Run each case N times and collect latency statistics (load test mode).",
    )
    run_parser.add_argument(
        "--load-test-runs",
        type=int,
        default=10,
        metavar="N",
        help="Number of times to call each API in load test mode (default: 10).",
    )
    run_parser.add_argument(
        "--load-test-gt",
        metavar="PATH",
        help=(
            "Path to a load_test_gt.json produced by a previous GT capture run. "
            "When provided alongside --load-test, the candidate latency and "
            "responses are compared against the GT stats."
        ),
    )
    run_parser.add_argument(
        "--latency-threshold",
        type=float,
        default=20.0,
        metavar="PCT",
        help=(
            "Percentage by which candidate p95 may exceed GT p95 before it is "
            "flagged as a latency regression (default: 20)."
        ),
    )

    compare_parser = subparsers.add_parser(
        "compare",
        help="Compare two existing run_results.json files without hitting any APIs.",
        description=(
            "Offline comparison mode. Provide a candidate run_results.json and a GT "
            "run_results.json (both produced by previous 'run' commands, or hand-crafted) "
            "and generate the comparison Dashboard and Raw Report without making any "
            "network calls. Use this when you want to bring your own GT or re-compare "
            "two already-captured runs."
        ),
    )
    compare_parser.add_argument(
        "--candidate",
        required=True,
        metavar="PATH",
        help="Path to the candidate run_results.json (the newer / feature-branch run).",
    )
    compare_parser.add_argument(
        "--gt",
        required=True,
        metavar="PATH",
        help="Path to the GT run_results.json (your ground truth — can be hand-crafted or from a previous run).",
    )
    compare_parser.add_argument(
        "--label",
        required=True,
        help="Short label for the candidate (e.g. BICN-12345, feat-branch).",
    )
    compare_parser.add_argument(
        "--baseline-label",
        default="GT",
        help="Display label for the GT in dashboards (default: GT).",
    )
    compare_parser.add_argument(
        "--catalog",
        default="test_catalog.json",
        metavar="FILE",
        help="Path to test_catalog.json — used only to resolve service display names (default: test_catalog.json).",
    )
    compare_parser.add_argument(
        "--fail-on-diff",
        action="store_true",
        help="Exit with code 1 if any comparison diff is found.",
    )

    demo_parser = subparsers.add_parser(
        "demo",
        help="Generate sample HTML dashboards from synthetic data — no network calls.",
        description=(
            "Renders a Dashboard.html and Raw Report.html using hard-coded synthetic "
            "results so you can inspect the output format without any credentials or "
            "live services."
        ),
    )
    demo_parser.add_argument("--label", default="demo-candidate", help="Label shown in the demo dashboard header.")
    demo_parser.add_argument("--baseline-label", default="demo-baseline", help="Label shown as the baseline in the demo dashboard header.")

    ec_parser = subparsers.add_parser(
        "env-compare",
        parents=[common],
        help=(
            "Run all test cases against two environments concurrently and produce a "
            "side-by-side comparison of status, response structure and latency."
        ),
        description=(
            "Env-Compare mode hits every enabled test case against both --dev-env "
            "(default: dev2) and --test-env (default: test2) at the same time using "
            "concurrent HTTP calls.  For each case it checks:\n"
            "  1. HTTP status — both must return 200 (or the same expected_status).\n"
            "  2. Response structure — every field present in test2 must also exist in "
            "dev2 (missing records in dev2 are OK; missing field keys are not).\n"
            "  3. Latency — dev2 p95 must not exceed test2 p95 by more than "
            "--latency-threshold %% (default 20%%).\n\n"
            "Add --load-test to repeat each case N times per environment (default 5) "
            "and produce a dedicated load-test comparison dashboard."
        ),
    )
    ec_parser.add_argument("--label", required=True, help="Short label for this run (e.g. my-env-check).")
    ec_parser.add_argument(
        "--dev-env",
        default="dev2",
        metavar="ENV",
        help="The 'dev' environment to compare (default: dev2). Used as the environment segment in Takeda URLs.",
    )
    ec_parser.add_argument(
        "--test-env",
        default="test2",
        metavar="ENV",
        help="The 'test' environment to compare against (default: test2). Acts as the latency and structural baseline.",
    )
    ec_parser.add_argument(
        "--latency-threshold",
        type=float,
        default=20.0,
        metavar="PCT",
        help=(
            "Percentage by which dev p95 may exceed test p95 before it is flagged "
            "as a latency regression (default: 20)."
        ),
    )
    ec_parser.add_argument(
        "--load-test",
        action="store_true",
        help="Run each case N times against both environments and produce a load-test comparison dashboard.",
    )
    ec_parser.add_argument(
        "--load-test-runs",
        type=int,
        default=5,
        metavar="N",
        help="Number of runs per case per environment in load-test mode (default: 5).",
    )
    ec_parser.add_argument(
        "--concurrency-threshold",
        type=float,
        default=5.0,
        metavar="PCT",
        help=(
            "Error-rate percentage by which dev error rate may exceed test error rate "
            "before flagged as a concurrency regression (default: 5)."
        ),
    )
    ec_parser.add_argument(
        "--fail-on-diff",
        action="store_true",
        help="Exit with code 1 if any comparison shows a non-passed outcome.",
    )

    return parser.parse_args(argv)


def _load_context(args: argparse.Namespace, cwd: Path):
    catalog_path = (cwd / args.catalog).resolve()
    env_file = (cwd / args.env_file).resolve()
    workbook_path = (cwd / args.workbook).resolve()
    services, manual_cases, workbook_imports = load_catalog(catalog_path)
    all_cases = list(manual_cases)
    if not args.skip_workbook:
        all_cases.extend(build_workbook_cases(workbook_path, services, workbook_imports))
    selected_cases = select_cases(all_cases, set(args.only_service), set(args.only_case))
    return services, selected_cases, env_file


def _demo_services() -> dict[str, ServiceConfig]:
    return {
        "insight-center-dev-core-graphql-services": ServiceConfig(
            service_id="insight-center-dev-core-graphql-services",
            display_name="insight-center-dev-core-graphql-services",
            alias="Core",
            kind="graphql",
        ),
        "insight-center-dev-erm-services": ServiceConfig(
            service_id="insight-center-dev-erm-services",
            display_name="insight-center-dev-erm-services",
            alias="Risk",
            kind="rest",
        ),
    }


def _print_outcome_summary(label: str, results: list) -> None:
    from collections import Counter
    counts = Counter(r.outcome for r in results)
    total = len(results)
    passed = counts.get("passed", 0)
    failures = {k: v for k, v in counts.items() if k != "passed"}
    parts = [f"passed {passed}/{total}"]
    for outcome, n in sorted(failures.items()):
        parts.append(f"{outcome.replace('_', ' ')} {n}")
    print(f"  {label}: {' | '.join(parts)}")


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = parse_args(argv)
    cwd = Path.cwd()

    if args.command == "compare":
        # Load candidate
        candidate_path = (cwd / args.candidate).resolve() if not Path(args.candidate).is_absolute() else Path(args.candidate)
        candidate_data = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate_results = [RunResult(**item) for item in candidate_data]

        # Load GT (user-supplied)
        gt_path = (cwd / args.gt).resolve() if not Path(args.gt).is_absolute() else Path(args.gt)
        gt_data = json.loads(gt_path.read_text(encoding="utf-8"))
        gt_results = [RunResult(**item) for item in gt_data]

        # Load services for display names (best-effort — fall back to empty dict)
        try:
            catalog_path = (cwd / args.catalog).resolve()
            services, _, _ = load_catalog(catalog_path)
        except Exception:
            services = {}

        comparisons = compare_runs(candidate_results, gt_results)
        destination = ensure_output_dir(cwd, args.label)
        persist_run_artifacts(destination, candidate_results, "candidate")
        persist_run_artifacts(destination, gt_results, "baseline")
        persist_comparison_artifacts(destination, comparisons)
        paths = save_reports(destination, args.label, services, candidate_results, comparisons, args.baseline_label)

        print(f"Candidate        : {candidate_path}")
        print(f"GT               : {gt_path}")
        print(f"Comparison JSON  : {paths['comparison_results']}")
        print(f"Dashboard        : {paths['dashboard']}")
        print(f"Raw report       : {paths['raw_report']}")

        if args.fail_on_diff:
            return 1 if any(item.outcome != "passed" for item in comparisons) else 0
        return 0

    if args.command == "demo":
        destination = ensure_output_dir(cwd, args.label)
        candidate, comparisons = _demo_results()
        paths = save_reports(destination, args.label, _demo_services(), candidate, comparisons, args.baseline_label)
        persist_run_artifacts(destination, candidate, "candidate")
        persist_comparison_artifacts(destination, comparisons)
        print(f"Demo dashboard: {paths['dashboard']}")
        print(f"Demo raw report: {paths['raw_report']}")
        return 0

    if args.command == "env-compare":
        services, selected_cases, env_file = _load_context(args, cwd)
        dev_env: str = args.dev_env
        test_env: str = args.test_env
        env_values = {**os.environ, **load_dotenv(env_file)}
        destination = ensure_output_dir(cwd, args.label)

        print(f"Mode               : env-compare ({dev_env}  vs  {test_env})")
        print(f"Output directory   : {destination}")
        print(f"Latency threshold  : {args.latency_threshold}% (dev p95 vs test p95)")

        # ------------------------------------------------------------------
        # Single-pass comparison
        # ------------------------------------------------------------------
        pairs = run_env_compare(selected_cases, services, env_values, dev_env=dev_env, test_env=test_env)
        ec_results = compare_env_pairs(pairs, dev_env=dev_env, test_env=test_env, latency_threshold_pct=args.latency_threshold)
        _print_outcome_summary("Single-pass results", ec_results)

        # ------------------------------------------------------------------
        # Load test comparison (optional)
        # ------------------------------------------------------------------
        ec_load_results = None
        if getattr(args, "load_test", False):
            lt_runs: int = args.load_test_runs
            print(f"\nLoad test          : {lt_runs} runs per case per environment")
            lt_pairs = run_env_compare_load_test(
                selected_cases, services, env_values,
                runs=lt_runs, dev_env=dev_env, test_env=test_env,
            )
            ec_load_results = compare_env_load_pairs(
                lt_pairs, dev_env=dev_env, test_env=test_env,
                latency_threshold_pct=args.latency_threshold,
                concurrency_threshold_pct=args.concurrency_threshold,
            )
            _print_outcome_summary("Load-test results ", ec_load_results)

        paths = save_env_compare_reports(
            destination, args.label, dev_env, test_env, services,
            ec_results, load_results=ec_load_results,
        )

        print(f"\nEnv compare JSON   : {paths['env_compare_results']}")
        print(f"Dashboard          : {paths['env_compare_dashboard']}")
        if "env_compare_load_results" in paths:
            print(f"Load compare JSON  : {paths['env_compare_load_results']}")
            print(f"Load Dashboard     : {paths['env_compare_load_dashboard']}")

        if args.fail_on_diff:
            any_fail = any(r.outcome not in ("passed", "skipped") for r in ec_results)
            if ec_load_results:
                any_fail = any_fail or any(r.outcome not in ("passed",) for r in ec_load_results)
            return 1 if any_fail else 0
        return 0

    services, selected_cases, env_file = _load_context(args, cwd)
    if args.command == "list":
        print_case_table(selected_cases, services)
        print(f"\nTotal cases: {len(selected_cases)}")
        print(f"Default environment: {args.env}")
        return 0

    target_env: str = args.env
    env_values = {**os.environ, **load_dotenv(env_file)}
    destination = ensure_output_dir(cwd, args.label)

    print(f"Target environment : {target_env}")
    print(f"Output directory   : {destination}")

    # ------------------------------------------------------------------
    # Standard single-pass regression run
    # ------------------------------------------------------------------
    run_results = run_capture(selected_cases, services, env_values, target_env=target_env)
    persist_run_artifacts(destination, run_results, "candidate" if args.baseline else "gt")
    persist_service_case_index(destination, run_results)

    # Validate mutually exclusive options
    tester_gt_path_str: str | None = getattr(args, "tester_gt", None)
    if args.baseline and tester_gt_path_str:
        print("ERROR: --baseline and --tester-gt cannot be used together. Choose one.")
        return 1

    comparisons = None
    if tester_gt_path_str:
        tester_gt_path = (
            Path(tester_gt_path_str).resolve()
            if Path(tester_gt_path_str).is_absolute()
            else (cwd / tester_gt_path_str).resolve()
        )
        print(f"Tester GT        : {tester_gt_path}")
        tester_entries = load_tester_gt(tester_gt_path)
        baseline_results = tester_gt_to_run_results(tester_entries, selected_cases, services)
        comparisons = compare_runs(run_results, baseline_results)
        persist_run_artifacts(destination, baseline_results, "baseline")
        persist_comparison_artifacts(destination, comparisons)
    elif args.baseline:
        baseline_path = (
            Path(args.baseline).resolve()
            if Path(args.baseline).is_absolute()
            else (cwd / args.baseline).resolve()
        )
        baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_results = [RunResult(**item) for item in baseline_data]
        comparisons = compare_runs(run_results, baseline_results)
        persist_run_artifacts(destination, baseline_results, "baseline")
        persist_comparison_artifacts(destination, comparisons)

    paths = save_reports(
        destination, args.label, services, run_results, comparisons,
        getattr(args, "baseline_label", None),
    )
    print(f"Run results JSON   : {paths['run_results']}")
    if "comparison_results" in paths:
        print(f"Comparison JSON    : {paths['comparison_results']}")
    else:
        print(f"GT snapshot JSON   : {destination / 'gt_snapshot.json'}")
    print(f"Dashboard          : {paths['dashboard']}")
    print(f"Raw report         : {paths['raw_report']}")

    # ------------------------------------------------------------------
    # Load test (optional — runs each case N times)
    # ------------------------------------------------------------------
    if getattr(args, "load_test", False):
        lt_runs: int = args.load_test_runs
        print(f"\nLoad test          : {lt_runs} runs per case …")

        lt_stats = run_load_test(
            selected_cases, services, env_values,
            runs=lt_runs, target_env=target_env,
        )
        persist_load_test_artifacts(destination, lt_stats)

        lt_comparisons = None
        lt_gt_path_str: str | None = getattr(args, "load_test_gt", None)
        if lt_gt_path_str:
            lt_gt_path = (
                Path(lt_gt_path_str).resolve()
                if Path(lt_gt_path_str).is_absolute()
                else (cwd / lt_gt_path_str).resolve()
            )
            gt_data = json.loads(lt_gt_path.read_text(encoding="utf-8"))
            gt_stats = [LoadTestStats(**item) for item in gt_data]
            lt_comparisons = compare_load_tests(
                lt_stats, gt_stats,
                latency_threshold_pct=args.latency_threshold,
            )
            print(f"Latency threshold  : {args.latency_threshold}% (p95 regression flag)")

        lt_paths = save_load_test_reports(
            destination, args.label, services, lt_stats, lt_comparisons,
            getattr(args, "baseline_label", None),
        )
        print(f"Load test GT JSON  : {lt_paths['load_test_gt']}")
        if "load_test_comparison" in lt_paths:
            print(f"Load test comp JSON: {lt_paths['load_test_comparison']}")
        print(f"Load Test Dashboard: {lt_paths['load_test_dashboard']}")

    # ------------------------------------------------------------------
    # Exit code
    # ------------------------------------------------------------------
    if comparisons and args.fail_on_diff:
        return 1 if any(item.outcome != "passed" for item in comparisons) else 0
    return 0
