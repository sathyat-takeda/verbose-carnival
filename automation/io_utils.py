from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(("'", '"')):
            value = value[1:-1]
        values[key] = value
    return values


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return slug or "run"


def iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def json_block(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, ensure_ascii=True)


def safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def truncate_text(value: str | None, limit: int = 300) -> str:
    if value is None:
        return ""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def load_tester_gt(path: Path) -> list[dict]:
    """Load a tester-supplied GT file in JSON or CSV format.

    **JSON format** — list of objects, one per case:

    .. code-block:: json

        [
          {
            "case_id": "po_adh_sdn_status_fetch",
            "expected_status": 200,
            "expected_response": { "status": "Success", "data": { ... } },
            "notes": "optional tester note"
          },
          {
            "case_id": "po_adh_count_by_batch",
            "expected_status": 200
          }
        ]

    Omit ``expected_response`` to perform a status-code-only check for that case.

    **CSV format** — one row per case:

    .. code-block::

        case_id,expected_status,expected_response_file,notes
        po_adh_sdn_status_fetch,200,expected/sdn_status.json,SDN status check
        po_adh_count_by_batch,200,,Status-code only

    ``expected_response_file`` is optional; when given it must be a path
    (relative to the CSV file's directory) pointing to a JSON file containing
    the expected response body.
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if suffix == ".csv":
        entries: list[dict] = []
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                case_id = row.get("case_id", "").strip()
                if not case_id:
                    continue
                entry: dict = {
                    "case_id": case_id,
                    "expected_status": int((row.get("expected_status") or "200").strip()),
                    "notes": (row.get("notes") or "").strip(),
                }
                resp_file = (row.get("expected_response_file") or "").strip()
                if resp_file:
                    resp_path = path.parent / resp_file
                    if resp_path.exists():
                        entry["expected_response"] = json.loads(resp_path.read_text(encoding="utf-8"))
                    else:
                        raise FileNotFoundError(
                            f"expected_response_file not found: {resp_path} (referenced from {path})"
                        )
                entries.append(entry)
        return entries
    raise ValueError(f"Unsupported tester GT format '{suffix}'. Use .json or .csv")

