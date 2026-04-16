from __future__ import annotations

import hashlib
import json
from typing import Any


VOLATILE_FIELD_NAMES = {"__typename"}


def looks_like_json_object(value: Any) -> bool:
    return isinstance(value, (dict, list))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def response_hash(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def wildcard_match(path_parts: list[str], pattern: str) -> bool:
    pattern_parts = pattern.split(".")
    if len(pattern_parts) != len(path_parts):
        return False
    for actual, expected in zip(path_parts, pattern_parts):
        if expected == "*":
            continue
        if actual != expected:
            return False
    return True


def normalize_payload(
    value: Any,
    ignore_paths: list[str],
    ignore_field_names: set[str],
    path_parts: list[str] | None = None,
) -> Any:
    path_parts = path_parts or []
    if any(wildcard_match(path_parts, pattern) for pattern in ignore_paths):
        return "__IGNORED__"

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key in sorted(value.keys()):
            if key in ignore_field_names or key in VOLATILE_FIELD_NAMES:
                continue
            child = normalize_payload(value[key], ignore_paths, ignore_field_names, path_parts + [key])
            if child != "__IGNORED__":
                normalized[key] = child
        return normalized

    if isinstance(value, list):
        normalized_items = []
        for item in value:
            child = normalize_payload(item, ignore_paths, ignore_field_names, path_parts + ["*"])
            if child != "__IGNORED__":
                normalized_items.append(child)
        return normalized_items

    return value


def extract_schema(value: Any) -> Any:
    """Convert a JSON value to its structural schema.

    Scalar leaves become their type name (str, int, float, bool, NoneType).
    Non-empty lists become a single-element list containing the schema of the
    first item.  Empty lists remain ``[]``.

    This lets us compare field presence without being sensitive to actual
    values or record counts.
    """
    if isinstance(value, dict):
        return {k: extract_schema(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        if not value:
            return []
        return [extract_schema(value[0])]
    if value is None:
        return "NoneType"
    return type(value).__name__


def compare_schemas(
    test_schema: Any,
    dev_schema: Any,
    prefix: str = "$",
    limit: int = 25,
) -> list[str]:
    """Report fields present in *test_schema* that are absent in *dev_schema*.

    Direction: test2 is the reference.  dev2 must have at least the same
    fields.  Extra fields in dev2 are *not* reported (more is fine).
    An empty list in dev2 when test2 has items is *not* flagged — missing
    records are acceptable; missing field keys are not.
    """
    diffs: list[str] = []

    def _walk(t_val: Any, d_val: Any, path: str) -> None:
        if len(diffs) >= limit:
            return

        # test has a dict — check that dev has all the same keys
        if isinstance(t_val, dict):
            if not isinstance(d_val, dict):
                diffs.append(f"{path}: test2 has object, dev2 has {type(d_val).__name__}")
                return
            for key in sorted(t_val.keys()):
                if key not in d_val:
                    diffs.append(f"{path}.{key}: field present in test2 but missing in dev2")
                else:
                    _walk(t_val[key], d_val[key], f"{path}.{key}")
                if len(diffs) >= limit:
                    return
            return

        # test has a non-empty list — if dev is also non-empty, recurse into items
        if isinstance(t_val, list):
            if not isinstance(d_val, list):
                diffs.append(f"{path}: test2 has array, dev2 has {type(d_val).__name__}")
                return
            # empty dev list is fine (records may differ)
            if t_val and d_val:
                _walk(t_val[0], d_val[0], f"{path}[*]")
            return

        # scalar: check for type mismatch only
        if type(t_val) is not type(d_val):
            diffs.append(
                f"{path}: type mismatch test2={type(t_val).__name__} dev2={type(d_val).__name__}"
            )

    _walk(test_schema, dev_schema, prefix)
    return diffs


def build_diffs(candidate: Any, baseline: Any, prefix: str = "$", limit: int = 25) -> list[str]:
    from automation.io_utils import truncate_text

    diffs: list[str] = []

    def _walk(left: Any, right: Any, path: str) -> None:
        if len(diffs) >= limit:
            return
        if type(left) is not type(right):
            diffs.append(f"{path}: type mismatch baseline={type(right).__name__} candidate={type(left).__name__}")
            return

        if isinstance(left, dict):
            all_keys = sorted(set(left.keys()) | set(right.keys()))
            for key in all_keys:
                if key not in right:
                    diffs.append(f"{path}.{key}: missing in baseline")
                elif key not in left:
                    diffs.append(f"{path}.{key}: missing in candidate")
                else:
                    _walk(left[key], right[key], f"{path}.{key}")
                if len(diffs) >= limit:
                    return
            return

        if isinstance(left, list):
            if len(left) != len(right):
                diffs.append(f"{path}: list length mismatch baseline={len(right)} candidate={len(left)}")
            for idx, (left_item, right_item) in enumerate(zip(left, right)):
                _walk(left_item, right_item, f"{path}[{idx}]")
                if len(diffs) >= limit:
                    return
            return

        if left != right:
            left_preview = truncate_text(canonical_json(left) if looks_like_json_object(left) else str(left), 120)
            right_preview = truncate_text(canonical_json(right) if looks_like_json_object(right) else str(right), 120)
            diffs.append(f"{path}: baseline={right_preview} candidate={left_preview}")

    _walk(candidate, baseline, prefix)
    return diffs

