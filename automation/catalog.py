from __future__ import annotations

import json
from pathlib import Path

from automation.io_utils import safe_json_loads, slugify
from automation.models import ServiceConfig, TestCase, WorkbookImport
from automation.workbook import parse_xlsx_sheet_rows


def apply_service_config(services: dict[str, ServiceConfig], config_path: Path) -> None:
    """Override service.enabled from config.json (keyed by alias). Missing keys keep catalog default."""
    if not config_path.exists():
        return
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    alias_map = {svc.alias: svc for svc in services.values()}
    for alias, enabled in cfg.get("services", {}).items():
        if alias in alias_map:
            alias_map[alias].enabled = bool(enabled)


def load_catalog(path: Path) -> tuple[dict[str, ServiceConfig], list[TestCase], list[WorkbookImport]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    services = {entry["service_id"]: ServiceConfig(**entry) for entry in payload["services"]}
    cases = [TestCase(**entry) for entry in payload["manual_cases"]]
    imports = [WorkbookImport(**entry) for entry in payload.get("workbook_imports", [])]
    return services, cases, imports


def build_workbook_cases(
    workbook_path: Path,
    services: dict[str, ServiceConfig],
    imports: list[WorkbookImport],
) -> list[TestCase]:
    cases: list[TestCase] = []
    for rule in imports:
        rows = parse_xlsx_sheet_rows(workbook_path, rule.sheet)
        for index, row in enumerate(rows, start=1):
            endpoint = row.get("EndPoint", "")
            raw_name = row.get("TestCaseName", "")
            if raw_name.startswith("http") and endpoint:
                raw_name = ""
            name = raw_name or f"{rule.sheet}: {endpoint or f'row-{index}'}"
            payload_text = row.get("Payload", "")
            lowered = f"{name} {endpoint}".lower()

            if rule.include_patterns and not any(pattern.lower() in lowered for pattern in rule.include_patterns):
                continue
            if rule.exclude_patterns and any(pattern.lower() in lowered for pattern in rule.exclude_patterns):
                continue

            service = services[rule.service]
            protocol = "graphql" if "/graphql" in endpoint.lower() or "query" in payload_text.lower() else "rest"
            parsed_body = safe_json_loads(payload_text) if payload_text else None
            notes = rule.notes
            if "mutation" in payload_text.lower():
                notes = (notes + " ").strip() + "Workbook mutation retained for reference; disabled by default to avoid state drift."

            cases.append(
                TestCase(
                    case_id=f"wb_{slugify(rule.sheet)}_{index:03d}",
                    name=name,
                    service=rule.service,
                    protocol=protocol,
                    method=row.get("Method", "GET").upper(),
                    endpoint=endpoint,
                    enabled=rule.enabled and "mutation" not in payload_text.lower(),
                    body=parsed_body,
                    tags=list(dict.fromkeys(rule.tags + ["workbook", slugify(rule.sheet)])),
                    sources=[f"Excel workbook: {workbook_path.name} [{rule.sheet}] row {index + 1}"],
                    execution_mode=rule.execution_mode,
                    notes=notes.strip(),
                    expected_status=int(row.get("ExpectedStatusCode", "200") or "200"),
                )
            )
    return cases


def select_cases(cases: list[TestCase], only_services: set[str], only_cases: set[str], services: dict[str, "ServiceConfig"] | None = None) -> list[TestCase]:
    selected: list[TestCase] = []
    for case in cases:
        if only_services and case.service not in only_services:
            continue
        if only_cases and case.case_id not in only_cases:
            continue
        if services:
            svc = services.get(case.service)
            if svc and not svc.enabled:
                continue
        selected.append(case)
    return selected


def print_case_table(cases: list[TestCase], services: dict[str, ServiceConfig]) -> None:
    for case in cases:
        service = services.get(case.service)
        svc_disabled = service and not service.enabled
        state = "svc-off" if svc_disabled else ("enabled" if case.enabled else "disabled")
        alias = service.alias if service else case.service
        print(f"{case.case_id:36} {alias:18} {case.method:5} {case.protocol:7} {state:8} {case.name}")

