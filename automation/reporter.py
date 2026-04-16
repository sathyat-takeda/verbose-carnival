from __future__ import annotations

import html
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from automation.io_utils import iso_now, json_block, slugify, write_json
from automation.models import (
    ComparisonResult,
    EnvCompareLoadResult,
    EnvCompareResult,
    LoadTestComparison,
    LoadTestStats,
    RunResult,
    ServiceConfig,
)


OUTPUT_DIRNAME = "output"

# Shared CSS block for diff visualization (used in both dashboard and raw report)
_DIFF_CSS = """
  .diff-block { margin-top: 12px; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .diff-line { display: flex; gap: 10px; padding: 5px 14px; line-height: 1.6; align-items: baseline; }
  .diff-line.removed { background: #fff1f0; }
  .diff-line.added   { background: #f0fff4; }
  .diff-line.changed { background: #fffbeb; }
  .diff-line.info    { background: #f8fafc; color: #5a6472; }
  .diff-sym  { font-weight: 700; flex-shrink: 0; width: 14px; user-select: none; }
  .diff-line.removed .diff-sym { color: #c0392b; }
  .diff-line.added   .diff-sym { color: #1a7f37; }
  .diff-line.changed .diff-sym { color: #92400e; }
  .diff-path { color: #374151; flex-shrink: 0; max-width: 44%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .diff-val  { flex: 1; overflow-wrap: anywhere; }
  .diff-line.removed .diff-val { color: #c0392b; }
  .diff-line.added   .diff-val { color: #1a7f37; }
  .diff-line.changed .diff-val { color: #92400e; }
"""


def _parse_diff_to_structured(diffs: list[str]) -> list[dict]:
    """Convert raw diff strings from build_diffs into structured dicts.

    Each dict has keys: ``type`` (removed | added | changed | info),
    ``path``, ``baseline``, ``candidate``.  Used by the dashboard JS renderer
    and the raw-report server-side renderer.

    Handled patterns (from build_diffs and compare_runs):
    - ``{path}: missing in baseline``
    - ``{path}: missing in candidate``
    - ``{path}: baseline={X} candidate={Y}``        (value change)
    - ``{path}: type mismatch baseline={T} candidate={T}``
    - ``{path}: list length mismatch baseline={N} candidate={N}``
    - ``HTTP status mismatch baseline={code} candidate={code}``
    """
    structured: list[dict] = []
    for d in diffs:
        # missing in baseline / candidate
        m = re.match(r"^(.+): missing in baseline$", d)
        if m:
            structured.append({"type": "added", "path": m.group(1), "baseline": None, "candidate": None})
            continue
        m = re.match(r"^(.+): missing in candidate$", d)
        if m:
            structured.append({"type": "removed", "path": m.group(1), "baseline": None, "candidate": None})
            continue
        # HTTP status mismatch (top-level, no path prefix)
        m = re.match(r"^HTTP status mismatch baseline=(.+) candidate=(.+)$", d)
        if m:
            structured.append({"type": "changed", "path": "HTTP status", "baseline": m.group(1), "candidate": m.group(2)})
            continue
        # type mismatch  →  {path}: type mismatch baseline=X candidate=Y
        m = re.match(r"^(.+): type mismatch baseline=(.+) candidate=(.+)$", d)
        if m:
            structured.append({"type": "changed", "path": m.group(1), "baseline": f"type {m.group(2)}", "candidate": f"type {m.group(3)}"})
            continue
        # list length mismatch  →  {path}: list length mismatch baseline=N candidate=N
        m = re.match(r"^(.+): list length mismatch baseline=(.+) candidate=(.+)$", d)
        if m:
            structured.append({"type": "changed", "path": m.group(1), "baseline": f"length {m.group(2)}", "candidate": f"length {m.group(3)}"})
            continue
        # generic value change  →  {path}: baseline=X candidate=Y
        if ": baseline=" in d:
            ci = d.find(": baseline=")
            path = d[:ci]
            rest = d[ci + len(": baseline="):]
            cand_i = rest.rfind(" candidate=")
            if cand_i != -1:
                structured.append({
                    "type": "changed",
                    "path": path,
                    "baseline": rest[:cand_i],
                    "candidate": rest[cand_i + len(" candidate="):],
                })
                continue
        # fallback
        structured.append({"type": "info", "path": None, "baseline": None, "candidate": d})
    return structured


def _render_diff_html(diffs: list[str]) -> str:
    """Render build_diffs output as a colored HTML block (server-side, for raw report)."""
    if not diffs:
        return ""
    lines: list[str] = []
    for d in _parse_diff_to_structured(diffs):
        t = d["type"]
        p = html.escape(d["path"] or "")
        bv = html.escape(str(d["baseline"] or ""))
        cv = html.escape(str(d["candidate"] or ""))
        info = html.escape(str(d["candidate"] or d["baseline"] or ""))
        if t == "removed":
            lines.append(
                f'<div class="diff-line removed">'
                f'<span class="diff-sym">-</span>'
                f'<span class="diff-path">{p}</span>'
                f'<span class="diff-val">missing in candidate</span>'
                f'</div>'
            )
        elif t == "added":
            lines.append(
                f'<div class="diff-line added">'
                f'<span class="diff-sym">+</span>'
                f'<span class="diff-path">{p}</span>'
                f'<span class="diff-val">missing in baseline</span>'
                f'</div>'
            )
        elif t == "changed":
            lines.append(
                f'<div class="diff-line removed">'
                f'<span class="diff-sym">-</span>'
                f'<span class="diff-path">{p}</span>'
                f'<span class="diff-val">{bv}</span>'
                f'</div>'
                f'<div class="diff-line added">'
                f'<span class="diff-sym">+</span>'
                f'<span class="diff-path">{p}</span>'
                f'<span class="diff-val">{cv}</span>'
                f'</div>'
            )
        else:
            lines.append(
                f'<div class="diff-line info">'
                f'<span class="diff-sym"> </span>'
                f'<span class="diff-val">{info}</span>'
                f'</div>'
            )
    return '<div class="diff-block">' + "".join(lines) + "</div>"


def ensure_output_dir(base_dir: Path, label: str) -> Path:
    timestamp = iso_now().replace(":", "").replace("+00:00", "Z").replace("-", "")
    destination = base_dir / OUTPUT_DIRNAME / f"{timestamp}__{slugify(label)}"
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def build_summary_cards(items: list[ComparisonResult] | list[RunResult], compare_mode: bool) -> dict[str, int]:
    counter = Counter()
    if compare_mode:
        for item in items:
            counter[item.outcome] += 1
    else:
        for item in items:
            counter[item.status] += 1
    return counter


# ---------------------------------------------------------------------------
# Regression dashboard
# ---------------------------------------------------------------------------

def render_dashboard_html(
    label: str,
    services: dict[str, ServiceConfig],
    payload: list[ComparisonResult] | list[RunResult],
    compare_mode: bool,
    baseline_label: str | None,
    generated_at: str,
) -> str:
    summary = build_summary_cards(payload, compare_mode)
    services_seen = sorted({item.service_alias for item in payload})
    total = len(payload)
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    errors = summary.get("error", 0)
    skipped = summary.get("skipped", 0) + summary.get("missing_baseline", 0) + summary.get("missing_candidate", 0)

    if compare_mode:
        records = [
            {
                "case_id": item.case_id,
                "name": item.name,
                "service": item.service_alias,
                "status": item.outcome,
                "elapsed_ms": item.elapsed_ms,
                "baseline_status": item.baseline_http_status,
                "candidate_status": item.candidate_http_status,
                "diffs_structured": _parse_diff_to_structured(item.diffs),
                "url": item.candidate_url,
                "sources": item.sources,
                "tags": item.tags,
                "notes": item.notes,
                "execution_mode": item.execution_mode,
            }
            for item in payload
        ]
    else:
        records = [
            {
                "case_id": item.case_id,
                "name": item.name,
                "service": item.service_alias,
                "status": item.status,
                "elapsed_ms": item.elapsed_ms,
                "baseline_status": None,
                "candidate_status": item.actual_status,
                "diffs_structured": _parse_diff_to_structured([item.error] if item.error else []),
                "url": item.url,
                "sources": item.sources,
                "tags": item.tags,
                "notes": item.notes,
                "execution_mode": item.execution_mode,
            }
            for item in payload
        ]

    data_blob = json.dumps(records).replace("</", "<\\/")
    title = "API Regression Dashboard"
    subtitle = f"Baseline: {baseline_label} | Candidate: {label}" if compare_mode and baseline_label else f"Snapshot: {label}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --takeda-red: #E50012;
    --takeda-dark: #8B000A;
    --bg: #f6f7f9;
    --surface: #ffffff;
    --surface-alt: #f2f4f7;
    --border: #dde2e8;
    --text: #18212f;
    --text-soft: #5a6472;
    --pass: #0c8f5a;
    --fail: #c64218;
    --error: #8f1d1d;
    --skip: #6b7280;
    --shadow: 0 10px 24px rgba(16, 24, 40, 0.08);
    --radius: 14px;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: "Segoe UI", Helvetica, Arial, sans-serif; background: linear-gradient(180deg, #fff5f5 0%, var(--bg) 220px); color: var(--text); }}
  .hero {{ padding: 32px 36px 24px; background: linear-gradient(135deg, var(--takeda-red), var(--takeda-dark)); color: white; }}
  .hero h1 {{ margin: 0; font-size: 28px; letter-spacing: -0.02em; }}
  .hero p {{ margin: 10px 0 0; color: rgba(255,255,255,0.84); }}
  .stats {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); padding: 20px 36px 8px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px; box-shadow: var(--shadow); }}
  .stat .value {{ font-size: 32px; font-weight: 700; }}
  .stat .label {{ margin-top: 6px; text-transform: uppercase; font-size: 12px; letter-spacing: 0.08em; color: var(--text-soft); }}
  .controls {{ display: flex; gap: 12px; padding: 16px 36px 8px; flex-wrap: wrap; }}
  .controls input, .controls select {{ border: 1px solid var(--border); border-radius: 999px; padding: 12px 16px; background: var(--surface); font-size: 14px; }}
  .results {{ padding: 8px 36px 40px; }}
  .case {{ background: var(--surface); border: 1px solid var(--border); border-left: 5px solid var(--border); border-radius: var(--radius); margin-bottom: 14px; box-shadow: var(--shadow); overflow: hidden; }}
  .case.passed {{ border-left-color: var(--pass); }}
  .case.failed {{ border-left-color: var(--fail); }}
  .case.error  {{ border-left-color: var(--error); }}
  .case.skipped {{ border-left-color: var(--skip); }}
  .case header {{ padding: 18px 20px 12px; display: flex; gap: 16px; justify-content: space-between; align-items: start; }}
  .case h2 {{ margin: 0 0 4px; font-size: 17px; }}
  .case small {{ color: var(--text-soft); font-size: 12px; }}
  .badge {{ border-radius: 999px; padding: 6px 12px; font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; white-space: nowrap; }}
  .badge.passed {{ background: rgba(12,143,90,0.10); color: var(--pass); }}
  .badge.failed {{ background: rgba(198,66,24,0.10); color: var(--fail); }}
  .badge.error  {{ background: rgba(143,29,29,0.10); color: var(--error); }}
  .badge.skipped, .badge.missing_baseline, .badge.missing_candidate {{ background: rgba(107,114,128,0.12); color: var(--skip); }}
  .case .content {{ padding: 0 20px 18px; }}
  .meta-row {{ display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--text-soft); font-size: 13px; margin-bottom: 10px; }}
  .http-pair {{ display: inline-flex; gap: 6px; align-items: center; }}
  .http-tag {{ border-radius: 6px; padding: 2px 7px; font-size: 12px; font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .http-tag.ok  {{ background: rgba(12,143,90,0.12); color: var(--pass); }}
  .http-tag.err {{ background: rgba(198,66,24,0.12); color: var(--fail); }}
  .http-tag.na  {{ background: var(--surface-alt); color: var(--skip); }}
  .chip {{ display: inline-block; margin: 0 6px 6px 0; padding: 4px 10px; border-radius: 999px; background: var(--surface-alt); color: var(--text-soft); font-size: 12px; }}
  .url  {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--text-soft); word-break: break-all; margin-bottom: 8px; }}
  .diff-label {{ font-size: 12px; font-weight: 600; color: var(--fail); margin-top: 12px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.06em; }}
{_DIFF_CSS}
</style>
</head>
<body>
  <section class="hero">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(subtitle)}</p>
    <p>Generated at {html.escape(generated_at)} | Services covered: {len(services_seen)}</p>
  </section>
  <section class="stats">
    <div class="stat"><div class="value">{total}</div><div class="label">Cases</div></div>
    <div class="stat"><div class="value" style="color:var(--pass)">{passed}</div><div class="label">Passed</div></div>
    <div class="stat"><div class="value" style="color:var(--fail)">{failed}</div><div class="label">Failed</div></div>
    <div class="stat"><div class="value" style="color:var(--error)">{errors}</div><div class="label">Errors</div></div>
    <div class="stat"><div class="value" style="color:var(--skip)">{skipped}</div><div class="label">Skipped</div></div>
    <div class="stat"><div class="value">{len(services_seen)}</div><div class="label">Services</div></div>
  </section>
  <section class="controls">
    <input id="search" type="search" placeholder="Search case, service, tag or diff">
    <select id="statusFilter">
      <option value="all">All statuses</option>
      <option value="passed">Passed</option>
      <option value="failed">Failed</option>
      <option value="error">Error</option>
      <option value="skipped">Skipped / Missing</option>
    </select>
    <select id="serviceFilter">
      <option value="all">All services</option>
      {''.join(f'<option value="{html.escape(alias)}">{html.escape(alias)}</option>' for alias in services_seen)}
    </select>
  </section>
  <section class="results" id="results"></section>
  <script id="records-data" type="application/json">{data_blob}</script>
  <script>
    const records = JSON.parse(document.getElementById("records-data").textContent);
    const root = document.getElementById("results");
    const search = document.getElementById("search");
    const statusFilter = document.getElementById("statusFilter");
    const serviceFilter = document.getElementById("serviceFilter");

    function normalizedStatus(value) {{
      if (value === "missing_baseline" || value === "missing_candidate") return "skipped";
      return value;
    }}

    function esc(s) {{
      return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }}

    function httpTag(code) {{
      if (code == null) return `<span class="http-tag na">n/a</span>`;
      const ok = code >= 200 && code < 300;
      return `<span class="http-tag ${{ok ? "ok" : "err"}}">${{code}}</span>`;
    }}

    function renderDiff(diffs) {{
      if (!diffs || !diffs.length) return "";
      const lines = diffs.slice(0, 20).map(d => {{
        if (d.type === "removed") {{
          return `<div class="diff-line removed"><span class="diff-sym">-</span><span class="diff-path">${{esc(d.path)}}</span><span class="diff-val">missing in candidate</span></div>`;
        }}
        if (d.type === "added") {{
          return `<div class="diff-line added"><span class="diff-sym">+</span><span class="diff-path">${{esc(d.path)}}</span><span class="diff-val">missing in baseline</span></div>`;
        }}
        if (d.type === "changed") {{
          return `<div class="diff-line removed"><span class="diff-sym">-</span><span class="diff-path">${{esc(d.path)}}</span><span class="diff-val">${{esc(d.baseline)}}</span></div>` +
                 `<div class="diff-line added"><span class="diff-sym">+</span><span class="diff-path">${{esc(d.path)}}</span><span class="diff-val">${{esc(d.candidate)}}</span></div>`;
        }}
        return `<div class="diff-line info"><span class="diff-sym"> </span><span class="diff-val">${{esc(d.candidate || d.baseline || "")}}</span></div>`;
      }});
      return `<div class="diff-block">${{lines.join("")}}</div>`;
    }}

    function render() {{
      const term = search.value.toLowerCase().trim();
      const selectedStatus = statusFilter.value;
      const selectedService = serviceFilter.value;
      const filtered = records.filter(record => {{
        const searchBlob = JSON.stringify(record).toLowerCase();
        const matchesSearch = !term || searchBlob.includes(term);
        const matchesStatus = selectedStatus === "all" || normalizedStatus(record.status) === selectedStatus;
        const matchesService = selectedService === "all" || record.service === selectedService;
        return matchesSearch && matchesStatus && matchesService;
      }});
      root.innerHTML = filtered.map(record => {{
        const chips = (record.tags || []).map(tag => `<span class="chip">${{esc(tag)}}</span>`).join("");
        const notes = record.notes ? `<div class="meta-row"><strong>Note:</strong> ${{esc(record.notes)}}</div>` : "";
        const sources = (record.sources || []).map(s => `<span style="font-size:12px;color:var(--text-soft)">${{esc(s)}}</span>`).join(" · ");
        const elapsed = record.elapsed_ms == null ? "n/a" : `${{record.elapsed_ms}} ms`;
        const hasDiff = record.diffs_structured && record.diffs_structured.length > 0;
        const diffHtml = hasDiff
          ? `<div class="diff-label">Diffs (${{record.diffs_structured.length}})</div>${{renderDiff(record.diffs_structured)}}`
          : "";
        const httpHtml = record.baseline_status != null
          ? `<span class="http-pair">Baseline: ${{httpTag(record.baseline_status)}} → Candidate: ${{httpTag(record.candidate_status)}}</span>`
          : `<span class="http-pair">HTTP: ${{httpTag(record.candidate_status)}}</span>`;
        return `
          <article class="case ${{normalizedStatus(record.status)}}">
            <header>
              <div>
                <h2>${{esc(record.name)}}</h2>
                <small>${{esc(record.service)}} · ${{esc(record.case_id)}}</small>
              </div>
              <span class="badge ${{record.status}}">${{record.status.replaceAll("_", " ")}}</span>
            </header>
            <div class="content">
              <div class="meta-row">
                ${{httpHtml}}
                <span>Latency: ${{elapsed}}</span>
                <span>Mode: ${{esc(record.execution_mode || "regression")}}</span>
              </div>
              <div class="url">${{esc(record.url || "")}}</div>
              <div>${{chips}}</div>
              ${{notes}}
              ${{sources ? `<div class="meta-row" style="margin-top:6px">${{sources}}</div>` : ""}}
              ${{diffHtml}}
            </div>
          </article>`;
      }}).join("");
    }}

    search.addEventListener("input", render);
    statusFilter.addEventListener("change", render);
    serviceFilter.addEventListener("change", render);
    render();
  </script>
</body>
</html>
"""


def render_raw_report_html(
    label: str,
    payload: list[ComparisonResult] | list[RunResult],
    compare_mode: bool,
    baseline_label: str | None,
    generated_at: str,
) -> str:
    subtitle = f"Baseline: {baseline_label} | Candidate: {label}" if compare_mode and baseline_label else f"Snapshot: {label}"

    # ------------------------------------------------------------------
    # Group payload by service alias
    # ------------------------------------------------------------------
    groups: dict[str, list] = {}
    for item in payload:
        groups.setdefault(item.service_alias, []).append(item)

    # ------------------------------------------------------------------
    # Build Table of Contents
    # ------------------------------------------------------------------
    toc_items: list[str] = []
    for alias in sorted(groups.keys()):
        items = groups[alias]
        anchor = f"svc-{slugify(alias)}"
        if compare_mode:
            counts = Counter(getattr(i, "outcome", "unknown") for i in items)
            passed_n = counts.get("passed", 0)
            fail_n = sum(v for k, v in counts.items() if k not in ("passed", "skipped", "missing_candidate"))
        else:
            counts = Counter(getattr(i, "status", "unknown") for i in items)
            passed_n = counts.get("passed", 0)
            fail_n = sum(v for k, v in counts.items() if k not in ("passed", "skipped"))
        parts = []
        if passed_n:
            parts.append(f'<span style="color:#0c8f5a">{passed_n} passed</span>')
        if fail_n:
            parts.append(f'<span style="color:#c64218">{fail_n} failed</span>')
        count_html = " · ".join(parts) if parts else '<span style="color:#6b7280">all skipped</span>'
        toc_items.append(
            f'<li><a href="#{html.escape(anchor)}">'
            f'<strong>{html.escape(alias)}</strong> '
            f'<span class="toc-count">({count_html})</span>'
            f'</a></li>'
        )
    toc_html = f'<nav class="toc"><h3>Jump to service</h3><ul>{"".join(toc_items)}</ul></nav>'

    # ------------------------------------------------------------------
    # Render each service section
    # ------------------------------------------------------------------
    def _outcome_cls(item: Any) -> str:
        if compare_mode:
            o = getattr(item, "outcome", "")
            return "passed" if o == "passed" else ("error" if o == "error" else "failed")
        s = getattr(item, "status", "")
        return s if s in ("passed", "error", "skipped") else "failed"

    def _http_pill(code: Any, label: str) -> str:
        if code is None:
            return f'<span class="pill na">{html.escape(label)}: n/a</span>'
        ok = isinstance(code, int) and 200 <= code < 300
        cls = "ok" if ok else "err"
        return f'<span class="pill {cls}">{html.escape(label)}: {code}</span>'

    sections: list[str] = []
    for alias in sorted(groups.keys()):
        items = groups[alias]
        anchor = f"svc-{slugify(alias)}"

        # Per-service summary badge row
        if compare_mode:
            counts = Counter(getattr(i, "outcome", "?") for i in items)
        else:
            counts = Counter(getattr(i, "status", "?") for i in items)
        badge_parts = [
            f'<span class="svc-badge {k}">{v} {k.replace("_", " ")}</span>'
            for k, v in sorted(counts.items())
        ]

        case_blocks: list[str] = []
        for item in items:
            oc = _outcome_cls(item)

            if compare_mode:
                meta = (
                    f'{_http_pill(item.baseline_http_status, "Baseline")}'
                    f'{_http_pill(item.candidate_http_status, "Candidate")}'
                    f'<span class="pill na">Latency: {item.elapsed_ms} ms</span>'
                )
                url_line = f'<div class="url-line"><strong>URL:</strong> <code>{html.escape(item.candidate_url or "")}</code></div>'
                diff_section = ""
                if item.diffs:
                    diff_section = f'<h4>Diffs ({len(item.diffs)})</h4>{_render_diff_html(item.diffs)}'
                req_section = f'<h4>Request Body</h4><pre class="code-block">{html.escape(json_block(item.request_body))}</pre>' if item.request_body else ""
                resp_section = f"""<h4>Responses</h4>
<div class="resp-grid">
  <div class="resp-panel">
    <div class="resp-label baseline-label">Baseline — {html.escape(baseline_label or "GT")}</div>
    <pre class="code-block">{html.escape(json_block(item.baseline_response))}</pre>
  </div>
  <div class="resp-panel">
    <div class="resp-label candidate-label">Candidate — {html.escape(label)}</div>
    <pre class="code-block">{html.escape(json_block(item.candidate_response))}</pre>
  </div>
</div>"""
                body = diff_section + req_section + resp_section
            else:
                meta = (
                    f'{_http_pill(item.actual_status, "HTTP")}'
                    f'<span class="pill na">Latency: {item.elapsed_ms} ms</span>'
                )
                url_line = f'<div class="url-line"><strong>URL:</strong> <code>{html.escape(item.url or "")}</code></div>'
                req_section = f'<h4>Request Body</h4><pre class="code-block">{html.escape(json_block(item.request_body))}</pre>' if item.request_body else ""
                body = req_section + f'<h4>Response</h4><pre class="code-block">{html.escape(json_block(item.response_json or item.response_text))}</pre>'

            outcome_label = getattr(item, "outcome", getattr(item, "status", ""))
            case_blocks.append(f"""
<div class="case-block {oc}" id="{html.escape(item.case_id)}">
  <div class="case-header">
    <div>
      <div class="case-title">{html.escape(item.name)}</div>
      <div class="case-meta">{html.escape(item.case_id)}</div>
    </div>
    <span class="outcome-badge {oc}">{html.escape(outcome_label.replace("_", " "))}</span>
  </div>
  <div class="case-body">
    <div class="pill-row">{meta}</div>
    {url_line}
    {body}
  </div>
</div>""")

        sections.append(f"""
<section class="svc-section" id="{html.escape(anchor)}">
  <div class="svc-header">
    <h2>{html.escape(alias)}</h2>
    <div class="svc-badges">{"".join(badge_parts)}</div>
  </div>
  {"".join(case_blocks)}
</section>""")

    content = toc_html + "".join(sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API Regression Raw Report</title>
<style>
  body {{ font-family: "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; background: #f6f7f9; color: #18212f; }}
  .hero {{ background: linear-gradient(135deg, #E50012, #8B000A); color: white; padding: 28px 36px 24px; }}
  .hero h1 {{ margin: 0 0 8px; font-size: 26px; letter-spacing: -0.02em; }}
  .hero p {{ margin: 4px 0 0; color: rgba(255,255,255,0.84); font-size: 14px; }}
  .content {{ max-width: 1340px; margin: 0 auto; padding: 24px 28px 60px; }}
  /* TOC */
  .toc {{ background: white; border: 1px solid #dde2e8; border-radius: 14px; padding: 18px 22px 14px; margin-bottom: 28px; box-shadow: 0 4px 14px rgba(16,24,40,0.06); }}
  .toc h3 {{ margin: 0 0 12px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.09em; color: #5a6472; }}
  .toc ul {{ margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: 8px; }}
  .toc a {{ display: block; padding: 6px 14px; border-radius: 999px; background: #f2f4f7; color: #18212f; text-decoration: none; font-size: 13px; }}
  .toc a:hover {{ background: #e2e8f0; }}
  .toc-count {{ font-weight: 400; font-size: 12px; }}
  /* Service sections */
  .svc-section {{ margin-bottom: 36px; }}
  .svc-header {{ display: flex; align-items: center; gap: 14px; padding-bottom: 10px; border-bottom: 2px solid #dde2e8; margin-bottom: 16px; }}
  .svc-header h2 {{ margin: 0; font-size: 20px; }}
  .svc-badges {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .svc-badge {{ border-radius: 999px; padding: 3px 11px; font-size: 12px; font-weight: 700; }}
  .svc-badge.passed  {{ background: rgba(12,143,90,0.12); color: #0c8f5a; }}
  .svc-badge.failed  {{ background: rgba(198,66,24,0.12); color: #c64218; }}
  .svc-badge.error   {{ background: rgba(143,29,29,0.12); color: #8f1d1d; }}
  .svc-badge.skipped {{ background: rgba(107,114,128,0.12); color: #6b7280; }}
  /* Case blocks */
  .case-block {{ background: white; border: 1px solid #dde2e8; border-left: 5px solid #dde2e8; border-radius: 12px; margin-bottom: 14px; overflow: hidden; box-shadow: 0 4px 14px rgba(16,24,40,0.06); }}
  .case-block.passed {{ border-left-color: #0c8f5a; }}
  .case-block.failed {{ border-left-color: #c64218; }}
  .case-block.error  {{ border-left-color: #8f1d1d; }}
  .case-block.skipped {{ border-left-color: #6b7280; }}
  .case-header {{ display: flex; justify-content: space-between; align-items: flex-start; padding: 14px 18px 10px; background: #f9fafb; border-bottom: 1px solid #dde2e8; }}
  .case-title {{ font-size: 15px; font-weight: 600; }}
  .case-meta  {{ font-size: 12px; color: #5a6472; margin-top: 2px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .outcome-badge {{ border-radius: 999px; padding: 4px 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; white-space: nowrap; flex-shrink: 0; }}
  .outcome-badge.passed  {{ background: rgba(12,143,90,0.12); color: #0c8f5a; }}
  .outcome-badge.failed  {{ background: rgba(198,66,24,0.12); color: #c64218; }}
  .outcome-badge.error   {{ background: rgba(143,29,29,0.12); color: #8f1d1d; }}
  .outcome-badge.skipped {{ background: rgba(107,114,128,0.12); color: #6b7280; }}
  .case-body {{ padding: 14px 18px 18px; }}
  /* Pills */
  .pill-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }}
  .pill {{ border-radius: 6px; padding: 3px 10px; font-size: 12px; font-weight: 600; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  .pill.ok  {{ background: rgba(12,143,90,0.12); color: #0c8f5a; }}
  .pill.err {{ background: rgba(198,66,24,0.12); color: #c64218; }}
  .pill.na  {{ background: #f2f4f7; color: #5a6472; }}
  .url-line {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #5a6472; margin-bottom: 14px; word-break: break-all; }}
  /* Response panels */
  h4 {{ margin: 16px 0 8px; font-size: 13px; color: #5a6472; text-transform: uppercase; letter-spacing: 0.07em; font-weight: 600; }}
  .resp-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
  .resp-panel {{ min-width: 0; }}
  .resp-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; padding: 5px 10px; border-radius: 6px 6px 0 0; }}
  .baseline-label  {{ background: #f1f5f9; color: #334155; border: 1px solid #cbd5e1; border-bottom: none; }}
  .candidate-label {{ background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; border-bottom: none; }}
  .code-block {{ background: #0f172a; color: #e2e8f0; padding: 14px 16px; overflow-x: auto; border-radius: 0 0 10px 10px; white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; margin: 0; max-height: 480px; overflow-y: auto; }}
  .resp-panel:first-child .code-block {{ border-radius: 0 10px 10px 10px; }}
  /* standalone code blocks (request body, single response) */
  .case-body > pre.code-block {{ border-radius: 10px; }}
{_DIFF_CSS}
  @media (max-width: 900px) {{
    .resp-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
  <section class="hero">
    <h1>API Regression Raw Report</h1>
    <p>{html.escape(subtitle)}</p>
    <p>Generated at {html.escape(generated_at)}</p>
  </section>
  <div class="content">
    {content}
  </div>
</body>
</html>
"""


def save_reports(
    destination: Path,
    label: str,
    services: dict[str, ServiceConfig],
    run_results: list[RunResult],
    comparisons: list[ComparisonResult] | None,
    baseline_label: str | None,
) -> dict[str, Path]:
    generated_at = iso_now()
    run_results_path = destination / "run_results.json"
    write_json(run_results_path, [asdict(item) for item in run_results])

    report_payload: list[ComparisonResult] | list[RunResult]
    compare_mode = comparisons is not None
    if compare_mode:
        comparison_path = destination / "comparison_results.json"
        write_json(comparison_path, [asdict(item) for item in comparisons])
        report_payload = comparisons
    else:
        comparison_path = None
        gt_path = destination / "gt_snapshot.json"
        write_json(gt_path, [asdict(item) for item in run_results])
        report_payload = run_results

    dashboard_path = destination / "Dashboard.html"
    raw_report_path = destination / "Raw Report.html"
    dashboard_path.write_text(
        render_dashboard_html(label, services, report_payload, compare_mode, baseline_label, generated_at),
        encoding="utf-8",
    )
    raw_report_path.write_text(
        render_raw_report_html(label, report_payload, compare_mode, baseline_label, generated_at),
        encoding="utf-8",
    )

    paths = {"run_results": run_results_path, "dashboard": dashboard_path, "raw_report": raw_report_path}
    if compare_mode and comparison_path is not None:
        paths["comparison_results"] = comparison_path
    return paths


def persist_service_case_index(destination: Path, results: list[RunResult]) -> None:
    index: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        index.setdefault(result.service_alias, []).append(
            {
                "case_id": result.case_id,
                "name": result.name,
                "status": result.status,
                "http_status": result.actual_status,
                "response_hash": result.response_hash,
            }
        )
    write_json(destination / "service_index.json", index)


def persist_comparison_artifacts(destination: Path, comparisons: list[ComparisonResult]) -> None:
    for item in comparisons:
        case_dir = destination / "comparisons" / slugify(item.service_alias) / item.case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        write_json(case_dir / "comparison.json", asdict(item))


# ---------------------------------------------------------------------------
# Load test dashboard
# ---------------------------------------------------------------------------

def render_load_test_dashboard_html(
    label: str,
    services: dict[str, ServiceConfig],
    stats: list[LoadTestStats],
    comparisons: list[LoadTestComparison] | None,
    baseline_label: str | None,
    generated_at: str,
) -> str:
    compare_mode = comparisons is not None
    title = "Load Test Dashboard"
    if compare_mode and baseline_label:
        subtitle = f"GT: {baseline_label} | Candidate: {label}"
    else:
        subtitle = f"GT Capture: {label}"

    services_seen = sorted({s.service_alias for s in stats})

    # Summary counters
    if compare_mode and comparisons:
        total = len(comparisons)
        passed = sum(1 for c in comparisons if c.outcome == "passed")
        lat_reg = sum(1 for c in comparisons if c.outcome in ("latency_regression", "both_changed"))
        resp_chg = sum(1 for c in comparisons if c.outcome in ("response_changed", "both_changed"))
        errors = sum(1 for c in comparisons if c.outcome == "error")
        missing = sum(1 for c in comparisons if c.outcome == "missing_gt")
        comp_data_blob = json.dumps([asdict(c) for c in comparisons]).replace("</", "<\\/")
    else:
        total = len(stats)
        passed = lat_reg = resp_chg = errors = missing = 0
        comp_data_blob = "null"

    stats_data_blob = json.dumps([asdict(s) for s in stats]).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --takeda-red: #E50012;
    --takeda-dark: #8B000A;
    --bg: #f6f7f9;
    --surface: #ffffff;
    --surface-alt: #f2f4f7;
    --border: #dde2e8;
    --text: #18212f;
    --text-soft: #5a6472;
    --pass: #0c8f5a;
    --fail: #c64218;
    --warn: #b45309;
    --error: #8f1d1d;
    --skip: #6b7280;
    --shadow: 0 10px 24px rgba(16,24,40,0.08);
    --radius: 14px;
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font-family: "Segoe UI", Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); }}
  .hero {{ padding: 32px 36px 24px; background: linear-gradient(135deg, var(--takeda-red), var(--takeda-dark)); color: white; }}
  .hero h1 {{ font-size: 28px; letter-spacing: -0.02em; }}
  .hero p {{ margin-top: 8px; color: rgba(255,255,255,0.84); font-size: 14px; }}
  .stats {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(130px,1fr)); padding: 20px 36px 8px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); }}
  .stat .value {{ font-size: 28px; font-weight: 700; }}
  .stat .label {{ margin-top: 4px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; color: var(--text-soft); }}
  .controls {{ display: flex; gap: 12px; padding: 14px 36px 6px; flex-wrap: wrap; }}
  .controls input, .controls select {{ border: 1px solid var(--border); border-radius: 999px; padding: 10px 16px; background: var(--surface); font-size: 14px; }}
  .table-wrap {{ padding: 10px 36px 40px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; font-size: 13px; }}
  thead th {{ background: var(--takeda-dark); color: white; padding: 11px 14px; text-align: left; white-space: nowrap; font-weight: 600; font-size: 12px; letter-spacing: 0.04em; }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--surface-alt); }}
  tbody td {{ padding: 10px 14px; vertical-align: middle; }}
  /* outcome row tinting */
  tr.passed {{ background: rgba(12,143,90,0.04); }}
  tr.latency_regression {{ background: rgba(198,66,24,0.07); }}
  tr.response_changed {{ background: rgba(180,83,9,0.07); }}
  tr.both_changed {{ background: rgba(198,66,24,0.14); }}
  tr.error {{ background: rgba(143,29,29,0.10); }}
  tr.missing_gt {{ background: rgba(107,114,128,0.08); }}
  .badge {{ display: inline-block; border-radius: 999px; padding: 4px 10px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; white-space: nowrap; }}
  .badge.passed {{ background: rgba(12,143,90,0.12); color: var(--pass); }}
  .badge.latency_regression, .badge.both_changed {{ background: rgba(198,66,24,0.12); color: var(--fail); }}
  .badge.response_changed {{ background: rgba(180,83,9,0.12); color: var(--warn); }}
  .badge.error {{ background: rgba(143,29,29,0.12); color: var(--error); }}
  .badge.missing_gt {{ background: rgba(107,114,128,0.12); color: var(--skip); }}
  .delta-pos {{ color: var(--fail); font-weight: 600; }}
  .delta-neg {{ color: var(--pass); font-weight: 600; }}
  .delta-neu {{ color: var(--text-soft); }}
  .bar-wrap {{ display: flex; align-items: center; gap: 6px; }}
  .bar {{ height: 7px; border-radius: 4px; min-width: 2px; flex-shrink: 0; }}
  .bar.gt {{ background: #94a3b8; }}
  .bar.cand {{ background: var(--takeda-red); }}
  .name-cell {{ max-width: 280px; }}
  .name-cell strong {{ display: block; font-size: 13px; }}
  .name-cell small {{ color: var(--text-soft); font-size: 11px; }}
  /* hidden rows */
  tr.hidden {{ display: none; }}
</style>
</head>
<body>
  <section class="hero">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(subtitle)}</p>
    <p>Generated at {html.escape(generated_at)} &nbsp;|&nbsp; Services: {len(services_seen)} &nbsp;|&nbsp; Runs per case: {stats[0].runs if stats else "n/a"}</p>
  </section>

  <section class="stats">
    <div class="stat"><div class="value">{total}</div><div class="label">Cases</div></div>
    {'<div class="stat"><div class="value" style="color:var(--pass)">' + str(passed) + '</div><div class="label">Passed</div></div>' if compare_mode else ''}
    {'<div class="stat"><div class="value" style="color:var(--fail)">' + str(lat_reg) + '</div><div class="label">Latency Regression</div></div>' if compare_mode else ''}
    {'<div class="stat"><div class="value" style="color:var(--warn)">' + str(resp_chg) + '</div><div class="label">Response Changed</div></div>' if compare_mode else ''}
    {'<div class="stat"><div class="value" style="color:var(--error)">' + str(errors) + '</div><div class="label">Errors</div></div>' if compare_mode else ''}
    {'<div class="stat"><div class="value" style="color:var(--skip)">' + str(missing) + '</div><div class="label">Missing GT</div></div>' if compare_mode else ''}
    <div class="stat"><div class="value">{len(services_seen)}</div><div class="label">Services</div></div>
  </section>

  <section class="controls">
    <input id="search" type="search" placeholder="Search case, service or tag&hellip;">
    {'<select id="outcomeFilter"><option value="all">All outcomes</option><option value="passed">Passed</option><option value="latency_regression">Latency Regression</option><option value="response_changed">Response Changed</option><option value="both_changed">Both Changed</option><option value="error">Error</option></select>' if compare_mode else ''}
    <select id="serviceFilter">
      <option value="all">All services</option>
      {''.join(f'<option value="{html.escape(a)}">{html.escape(a)}</option>' for a in services_seen)}
    </select>
  </section>

  <div class="table-wrap">
    <table id="lt-table">
      <thead>
        <tr>
          <th>Case</th>
          <th>Service</th>
          {'<th>Outcome</th>' if compare_mode else ''}
          <th>Runs OK</th>
          {'<th>GT avg</th><th>GT p50</th><th>GT p90</th><th>GT p95</th>' if compare_mode else ''}
          <th>{'Cand' if compare_mode else ''} avg</th>
          <th>{'Cand' if compare_mode else ''} p50</th>
          <th>{'Cand' if compare_mode else ''} p90</th>
          <th>{'Cand' if compare_mode else ''} p95</th>
          <th>min</th><th>max</th>
          {'<th>Δ p95 %</th><th>Response</th>' if compare_mode else ''}
          <th>Latency bars</th>
        </tr>
      </thead>
      <tbody id="lt-tbody"></tbody>
    </table>
  </div>

  <script id="stats-data" type="application/json">{stats_data_blob}</script>
  <script id="comp-data" type="application/json">{comp_data_blob}</script>
  <script>
    const statsRaw  = JSON.parse(document.getElementById("stats-data").textContent);
    const compRaw   = JSON.parse(document.getElementById("comp-data").textContent);
    const compareMode = compRaw !== null;
    const tbody = document.getElementById("lt-tbody");
    const search = document.getElementById("search");
    const serviceFilter = document.getElementById("serviceFilter");
    const outcomeFilter = compareMode ? document.getElementById("outcomeFilter") : null;

    // Build a lookup for comp data
    const compByCase = {{}};
    if (compareMode) compRaw.forEach(c => compByCase[c.case_id] = c);

    // Max p95 across all stats for bar scaling
    const allP95 = statsRaw.map(s => s.p95_ms || 0);
    const maxP95 = Math.max(...allP95, 1);
    const maxBarPx = 120;

    function ms(v) {{ return v == null ? "—" : v + " ms"; }}
    function deltaCell(pct) {{
      if (pct == null) return '<td class="delta-neu">—</td>';
      const sign = pct > 0 ? "+" : "";
      const cls = pct > 20 ? "delta-pos" : pct < -5 ? "delta-neg" : "delta-neu";
      return `<td class="${{cls}}">${{sign}}${{pct}}%</td>`;
    }}

    function barCell(stat, comp) {{
      const candP95 = stat.p95_ms || 0;
      const gtP95   = comp ? (comp.gt_p95_ms || 0) : 0;
      const candW   = Math.round((candP95 / maxP95) * maxBarPx);
      const gtW     = Math.round((gtP95   / maxP95) * maxBarPx);
      if (compareMode && comp) {{
        return `<td><div class="bar-wrap">
          <div class="bar gt" style="width:${{gtW}}px" title="GT p95: ${{gtP95}}ms"></div>
          <div class="bar cand" style="width:${{candW}}px" title="Cand p95: ${{candP95}}ms"></div>
          <span style="font-size:11px;color:#6b7280">${{candP95}}ms</span>
        </div></td>`;
      }}
      return `<td><div class="bar-wrap">
        <div class="bar cand" style="width:${{candW}}px" title="p95: ${{candP95}}ms"></div>
        <span style="font-size:11px;color:#6b7280">${{candP95}}ms</span>
      </div></td>`;
    }}

    function render() {{
      const term    = search.value.toLowerCase().trim();
      const svc     = serviceFilter.value;
      const outcome = outcomeFilter ? outcomeFilter.value : "all";

      const rows = statsRaw.map(stat => {{
        const comp = compByCase[stat.case_id] || null;
        const rowOutcome = comp ? comp.outcome : "gt";

        // Filters
        const blob = JSON.stringify(stat).toLowerCase();
        if (term && !blob.includes(term)) return null;
        if (svc !== "all" && stat.service_alias !== svc) return null;
        if (outcome !== "all" && compareMode && rowOutcome !== outcome) return null;

        const okCount = `${{stat.success_count}}/${{stat.runs}}`;
        const respCell = compareMode && comp
          ? (comp.response_changed
              ? `<td style="color:var(--warn);font-weight:600">Changed</td>`
              : `<td style="color:var(--pass)">Match ✓</td>`)
          : "";
        const outcomeCell = compareMode
          ? `<td><span class="badge ${{rowOutcome}}">${{rowOutcome.replaceAll("_"," ")}}</span></td>`
          : "";
        const gtCells = compareMode && comp
          ? `<td>${{ms(comp.gt_avg_ms)}}</td><td>${{ms(comp.gt_p50_ms)}}</td><td>${{ms(comp.gt_p90_ms)}}</td><td>${{ms(comp.gt_p95_ms)}}</td>`
          : (compareMode ? "<td>—</td><td>—</td><td>—</td><td>—</td>" : "");
        const deltaTd = compareMode && comp ? deltaCell(comp.latency_delta_pct) : (compareMode ? "<td>—</td>" : "");
        const minMs = compareMode && comp ? (comp.candidate_min_ms ?? stat.min_ms) : stat.min_ms;
        const maxMs = compareMode && comp ? (comp.candidate_max_ms ?? stat.max_ms) : stat.max_ms;

        return `<tr class="${{rowOutcome}}" data-service="${{stat.service_alias}}" data-outcome="${{rowOutcome}}">
          <td class="name-cell"><strong>${{stat.name}}</strong><small>${{stat.case_id}}</small></td>
          <td>${{stat.service_alias}}</td>
          ${{outcomeCell}}
          <td>${{okCount}}</td>
          ${{gtCells}}
          <td>${{ms(stat.avg_ms)}}</td>
          <td>${{ms(stat.p50_ms)}}</td>
          <td>${{ms(stat.p90_ms)}}</td>
          <td>${{ms(stat.p95_ms)}}</td>
          <td>${{ms(minMs)}}</td>
          <td>${{ms(maxMs)}}</td>
          ${{deltaTd}}
          ${{respCell}}
          ${{barCell(stat, comp)}}
        </tr>`;
      }}).filter(Boolean);

      tbody.innerHTML = rows.join("");
    }}

    search.addEventListener("input", render);
    serviceFilter.addEventListener("change", render);
    if (outcomeFilter) outcomeFilter.addEventListener("change", render);
    render();
  </script>
</body>
</html>
"""


def save_load_test_reports(
    destination: Path,
    label: str,
    services: dict[str, ServiceConfig],
    lt_stats: list[LoadTestStats],
    lt_comparisons: list[LoadTestComparison] | None,
    baseline_label: str | None,
) -> dict[str, Path]:
    """Persist load test GT file, optional comparison file, and HTML dashboard."""
    generated_at = iso_now()

    # Always write the GT/candidate stats file so the next run can use it as GT
    gt_path = destination / "load_test_gt.json"
    write_json(gt_path, [asdict(s) for s in lt_stats])
    paths: dict[str, Path] = {"load_test_gt": gt_path}

    if lt_comparisons is not None:
        comp_path = destination / "load_test_comparison.json"
        write_json(comp_path, [asdict(c) for c in lt_comparisons])
        paths["load_test_comparison"] = comp_path

    dashboard_path = destination / "Load Test Dashboard.html"
    dashboard_path.write_text(
        render_load_test_dashboard_html(
            label, services, lt_stats, lt_comparisons, baseline_label, generated_at
        ),
        encoding="utf-8",
    )
    paths["load_test_dashboard"] = dashboard_path
    return paths


# ---------------------------------------------------------------------------
# Env-compare dashboard (dev2 vs test2 side-by-side)
# ---------------------------------------------------------------------------

def render_env_compare_dashboard_html(
    label: str,
    dev_env: str,
    test_env: str,
    services: dict[str, ServiceConfig],
    results: list[EnvCompareResult],
    generated_at: str,
) -> str:
    from collections import Counter

    summary = Counter(r.outcome for r in results)
    services_seen = sorted({r.service_alias for r in results})
    total = len(results)
    passed = summary.get("passed", 0)
    dev_failed = summary.get("dev_failed", 0)
    status_mismatch = summary.get("status_mismatch", 0)
    structure_mismatch = summary.get("structure_mismatch", 0)
    latency_regression = summary.get("latency_regression", 0)
    errors = summary.get("error", 0) + summary.get("test_env_error", 0)
    skipped = summary.get("skipped", 0)

    records = [
        {
            "case_id": r.case_id,
            "name": r.name,
            "service": r.service_alias,
            "outcome": r.outcome,
            "dev_env": r.dev_env,
            "test_env": r.test_env,
            "dev_status": r.dev_status,
            "test_status": r.test_status,
            "dev_elapsed_ms": r.dev_elapsed_ms,
            "test_elapsed_ms": r.test_elapsed_ms,
            "latency_delta_pct": r.latency_delta_pct,
            "structural_match": r.structural_match,
            "structural_diffs": r.structural_diffs,
            "dev_error": r.dev_error,
            "test_error": r.test_error,
            "tags": r.tags,
            "notes": r.notes,
            "execution_mode": r.execution_mode,
            "dev_url": r.dev_url,
            "test_url": r.test_url,
        }
        for r in results
    ]
    data_blob = json.dumps(records).replace("</", "<\\/")
    title = "Env Compare Dashboard"
    subtitle = f"{dev_env}  vs  {test_env}  |  Label: {label}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --takeda-red: #E50012;
    --takeda-dark: #8B000A;
    --bg: #f6f7f9;
    --surface: #ffffff;
    --surface-alt: #f2f4f7;
    --border: #dde2e8;
    --text: #18212f;
    --text-soft: #5a6472;
    --pass: #0c8f5a;
    --fail: #c64218;
    --warn: #b45309;
    --error: #8f1d1d;
    --skip: #6b7280;
    --shadow: 0 10px 24px rgba(16,24,40,0.08);
    --radius: 14px;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: "Segoe UI", Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); }}
  .hero {{ padding: 32px 36px 24px; background: linear-gradient(135deg, var(--takeda-red), var(--takeda-dark)); color: white; }}
  .hero h1 {{ margin: 0; font-size: 28px; letter-spacing: -0.02em; }}
  .hero p {{ margin: 10px 0 0; color: rgba(255,255,255,0.84); font-size: 14px; }}
  .stats {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(130px,1fr)); padding: 20px 36px 8px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); }}
  .stat .value {{ font-size: 28px; font-weight: 700; }}
  .stat .label {{ margin-top: 4px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; color: var(--text-soft); }}
  .controls {{ display: flex; gap: 12px; padding: 14px 36px 6px; flex-wrap: wrap; }}
  .controls input, .controls select {{ border: 1px solid var(--border); border-radius: 999px; padding: 10px 16px; background: var(--surface); font-size: 14px; }}
  .table-wrap {{ padding: 10px 36px 40px; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; font-size: 13px; }}
  thead th {{ background: var(--takeda-dark); color: white; padding: 11px 14px; text-align: left; white-space: nowrap; font-weight: 600; font-size: 12px; letter-spacing: 0.04em; }}
  thead th.env-hdr {{ text-align: center; background: rgba(0,0,0,0.18); }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--surface-alt); }}
  tbody td {{ padding: 10px 14px; vertical-align: middle; }}
  tr.passed {{ background: rgba(12,143,90,0.04); }}
  tr.dev_failed {{ background: rgba(143,29,29,0.10); }}
  tr.status_mismatch, tr.structure_mismatch {{ background: rgba(198,66,24,0.07); }}
  tr.latency_regression {{ background: rgba(180,83,9,0.07); }}
  tr.error, tr.test_env_error {{ background: rgba(107,114,128,0.06); }}
  tr.skipped {{ background: rgba(107,114,128,0.04); }}
  .badge {{ display: inline-block; border-radius: 999px; padding: 4px 10px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; white-space: nowrap; }}
  .badge.passed {{ background: rgba(12,143,90,0.12); color: var(--pass); }}
  .badge.dev_failed {{ background: rgba(143,29,29,0.12); color: var(--error); }}
  .badge.status_mismatch, .badge.structure_mismatch {{ background: rgba(198,66,24,0.12); color: var(--fail); }}
  .badge.latency_regression {{ background: rgba(180,83,9,0.12); color: var(--warn); }}
  .badge.error, .badge.test_env_error {{ background: rgba(107,114,128,0.12); color: var(--skip); }}
  .badge.skipped {{ background: rgba(107,114,128,0.10); color: var(--skip); }}
  .delta-pos {{ color: var(--fail); font-weight: 600; }}
  .delta-neg {{ color: var(--pass); font-weight: 600; }}
  .delta-neu {{ color: var(--text-soft); }}
  .name-cell {{ max-width: 260px; }}
  .name-cell strong {{ display: block; font-size: 13px; }}
  .name-cell small {{ color: var(--text-soft); font-size: 11px; }}
  .diff-cell {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--fail); max-width: 300px; white-space: pre-wrap; word-break: break-word; }}
  tr.hidden {{ display: none; }}
  .struct-ok {{ color: var(--pass); }}
  .struct-fail {{ color: var(--fail); }}
</style>
</head>
<body>
  <section class="hero">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(subtitle)}</p>
    <p>Generated at {html.escape(generated_at)} &nbsp;|&nbsp; Services: {len(services_seen)}</p>
  </section>

  <section class="stats">
    <div class="stat"><div class="value">{total}</div><div class="label">Cases</div></div>
    <div class="stat"><div class="value" style="color:var(--pass)">{passed}</div><div class="label">Passed</div></div>
    <div class="stat"><div class="value" style="color:var(--error)">{dev_failed}</div><div class="label">Dev Failed</div></div>
    <div class="stat"><div class="value" style="color:var(--fail)">{status_mismatch}</div><div class="label">Status Mismatch</div></div>
    <div class="stat"><div class="value" style="color:var(--fail)">{structure_mismatch}</div><div class="label">Structure Mismatch</div></div>
    <div class="stat"><div class="value" style="color:var(--warn)">{latency_regression}</div><div class="label">Latency Regression</div></div>
    <div class="stat"><div class="value" style="color:var(--skip)">{errors}</div><div class="label">Errors</div></div>
    <div class="stat"><div class="value" style="color:var(--skip)">{skipped}</div><div class="label">Skipped</div></div>
  </section>

  <section class="controls">
    <input id="search" type="search" placeholder="Search case, service or diff&hellip;">
    <select id="outcomeFilter">
      <option value="all">All outcomes</option>
      <option value="passed">Passed</option>
      <option value="dev_failed">Dev Failed</option>
      <option value="status_mismatch">Status Mismatch</option>
      <option value="structure_mismatch">Structure Mismatch</option>
      <option value="latency_regression">Latency Regression</option>
      <option value="error">Error</option>
      <option value="skipped">Skipped</option>
    </select>
    <select id="serviceFilter">
      <option value="all">All services</option>
      {''.join(f'<option value="{html.escape(a)}">{html.escape(a)}</option>' for a in services_seen)}
    </select>
  </section>

  <div class="table-wrap">
    <table id="ec-table">
      <thead>
        <tr>
          <th>Case</th>
          <th>Service</th>
          <th>Outcome</th>
          <th class="env-hdr" colspan="2">{html.escape(dev_env)}</th>
          <th class="env-hdr" colspan="2">{html.escape(test_env)}</th>
          <th>Δ Latency</th>
          <th>Structure</th>
          <th>Diffs</th>
        </tr>
        <tr style="background:rgba(0,0,0,0.06);">
          <th colspan="3"></th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">HTTP</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">Latency</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">HTTP</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">Latency</th>
          <th colspan="3"></th>
        </tr>
      </thead>
      <tbody id="ec-tbody"></tbody>
    </table>
  </div>

  <script id="records-data" type="application/json">{data_blob}</script>
  <script>
    const records = JSON.parse(document.getElementById("records-data").textContent);
    const tbody = document.getElementById("ec-tbody");
    const search = document.getElementById("search");
    const outcomeFilter = document.getElementById("outcomeFilter");
    const serviceFilter = document.getElementById("serviceFilter");

    function ms(v) {{ return v == null ? "n/a" : v + " ms"; }}
    function deltaCell(pct) {{
      if (pct == null) return '<td class="delta-neu">n/a</td>';
      const sign = pct > 0 ? "+" : "";
      const cls = pct > 20 ? "delta-pos" : pct < -5 ? "delta-neg" : "delta-neu";
      return `<td class="${{cls}}">${{sign}}${{pct}}%</td>`;
    }}

    function render() {{
      const term = search.value.toLowerCase().trim();
      const oc = outcomeFilter.value;
      const svc = serviceFilter.value;

      const rows = records.map(r => {{
        if (term && !JSON.stringify(r).toLowerCase().includes(term)) return null;
        if (oc !== "all" && r.outcome !== oc) return null;
        if (svc !== "all" && r.service !== svc) return null;

        const diffs = (r.structural_diffs || []).slice(0, 5).join("\\n");
        const structCell = r.structural_match
          ? '<td class="struct-ok">Match ✓</td>'
          : `<td class="struct-fail">Mismatch ✗</td>`;
        return `<tr class="${{r.outcome}}">
          <td class="name-cell"><strong>${{r.name}}</strong><small>${{r.case_id}}</small></td>
          <td>${{r.service}}</td>
          <td><span class="badge ${{r.outcome}}">${{r.outcome.replaceAll("_"," ")}}</span></td>
          <td>${{r.dev_status ?? "n/a"}}</td>
          <td>${{ms(r.dev_elapsed_ms)}}</td>
          <td>${{r.test_status ?? "n/a"}}</td>
          <td>${{ms(r.test_elapsed_ms)}}</td>
          ${{deltaCell(r.latency_delta_pct)}}
          ${{structCell}}
          <td class="diff-cell">${{diffs || ""}}</td>
        </tr>`;
      }}).filter(Boolean);

      tbody.innerHTML = rows.join("");
    }}

    search.addEventListener("input", render);
    outcomeFilter.addEventListener("change", render);
    serviceFilter.addEventListener("change", render);
    render();
  </script>
</body>
</html>
"""


def render_env_compare_load_dashboard_html(
    label: str,
    dev_env: str,
    test_env: str,
    services: dict[str, ServiceConfig],
    results: list[EnvCompareLoadResult],
    generated_at: str,
) -> str:
    from collections import Counter, defaultdict

    summary = Counter(r.outcome for r in results)
    services_seen = sorted({r.service_alias for r in results})
    total = len(results)
    passed = summary.get("passed", 0)
    lat_reg = summary.get("latency_regression", 0) + summary.get("both_changed", 0)
    resp_chg = summary.get("response_changed", 0) + summary.get("both_changed", 0)
    conc_reg = summary.get("concurrency_regression", 0)
    dev_failed = summary.get("dev_failed", 0)
    errors = summary.get("error", 0) + summary.get("test_env_error", 0)

    runs = results[0].dev_runs if results else 5

    # Service-level latency aggregation
    svc_buckets: dict[str, dict[str, list]] = defaultdict(lambda: {"dev": [], "test": []})
    for r in results:
        if r.dev_avg_ms is not None:
            svc_buckets[r.service_alias]["dev"].append(r.dev_avg_ms)
        if r.test_avg_ms is not None:
            svc_buckets[r.service_alias]["test"].append(r.test_avg_ms)
    service_agg = [
        {
            "service": svc,
            "dev_avg_ms": round(sum(b["dev"]) / len(b["dev"]), 1) if b["dev"] else None,
            "test_avg_ms": round(sum(b["test"]) / len(b["test"]), 1) if b["test"] else None,
            "cases": len(b["dev"]) or len(b["test"]),
        }
        for svc, b in sorted(svc_buckets.items())
    ]
    all_dev_avgs = [r.dev_avg_ms for r in results if r.dev_avg_ms is not None]
    all_test_avgs = [r.test_avg_ms for r in results if r.test_avg_ms is not None]
    env_dev_avg = round(sum(all_dev_avgs) / len(all_dev_avgs), 1) if all_dev_avgs else None
    env_test_avg = round(sum(all_test_avgs) / len(all_test_avgs), 1) if all_test_avgs else None

    records = [
        {
            "case_id": r.case_id,
            "name": r.name,
            "service": r.service_alias,
            "outcome": r.outcome,
            "dev_env": r.dev_env,
            "test_env": r.test_env,
            "dev_runs": r.dev_runs,
            "dev_success_count": r.dev_success_count,
            "dev_error_count": r.dev_error_count,
            "dev_error_rate_pct": r.dev_error_rate_pct,
            "dev_avg_ms": r.dev_avg_ms,
            "dev_p50_ms": r.dev_p50_ms,
            "dev_p90_ms": r.dev_p90_ms,
            "dev_p95_ms": r.dev_p95_ms,
            "dev_min_ms": r.dev_min_ms,
            "dev_max_ms": r.dev_max_ms,
            "dev_raw_elapsed_ms": r.dev_raw_elapsed_ms,
            "test_runs": r.test_runs,
            "test_success_count": r.test_success_count,
            "test_error_count": r.test_error_count,
            "test_error_rate_pct": r.test_error_rate_pct,
            "test_avg_ms": r.test_avg_ms,
            "test_p50_ms": r.test_p50_ms,
            "test_p90_ms": r.test_p90_ms,
            "test_p95_ms": r.test_p95_ms,
            "test_min_ms": r.test_min_ms,
            "test_max_ms": r.test_max_ms,
            "test_raw_elapsed_ms": r.test_raw_elapsed_ms,
            "latency_delta_pct": r.latency_delta_pct,
            "latency_within_threshold": r.latency_within_threshold,
            "structural_diffs": r.structural_diffs,
            "schema_match": len(r.structural_diffs) == 0,
            "error_rate_delta_pct": r.error_rate_delta_pct,
            "concurrency_regression": r.concurrency_regression,
            "tags": r.tags,
            "notes": r.notes,
        }
        for r in results
    ]
    data_blob = json.dumps(records).replace("</", "<\\/")
    service_agg_blob = json.dumps(service_agg).replace("</", "<\\/")

    # Max p95 across both envs for bar scaling
    all_p95 = [v for r in results for v in [r.dev_p95_ms, r.test_p95_ms] if v is not None]
    max_p95_json = json.dumps(max(all_p95, default=1))

    def _avg_cell(v: float | None) -> str:
        return f"{v} ms" if v is not None else "—"

    env_dev_avg_display = html.escape(_avg_cell(env_dev_avg))
    env_test_avg_display = html.escape(_avg_cell(env_test_avg))

    svc_agg_rows = "".join(
        f'<tr><td><strong>{html.escape(s["service"])}</strong></td>'
        f'<td>{html.escape(_avg_cell(s["dev_avg_ms"]))}</td>'
        f'<td>{html.escape(_avg_cell(s["test_avg_ms"]))}</td>'
        f'<td style="color:var(--text-soft)">{s["cases"]}</td></tr>'
        for s in service_agg
    )

    title = "Env Compare Load Test Dashboard"
    subtitle = f"{dev_env}  vs  {test_env}  |  {runs} runs per case  |  Label: {label}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<style>
  :root {{
    --takeda-red: #E50012; --takeda-dark: #8B000A;
    --bg: #f6f7f9; --surface: #fff; --surface-alt: #f2f4f7;
    --border: #dde2e8; --text: #18212f; --text-soft: #5a6472;
    --pass: #0c8f5a; --fail: #c64218; --warn: #b45309;
    --error: #8f1d1d; --skip: #6b7280;
    --shadow: 0 10px 24px rgba(16,24,40,0.08); --radius: 14px;
  }}
  * {{ box-sizing: border-box; margin: 0; }}
  body {{ font-family: "Segoe UI", Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); }}
  .hero {{ padding: 32px 36px 24px; background: linear-gradient(135deg,var(--takeda-red),var(--takeda-dark)); color: white; }}
  .hero h1 {{ font-size: 28px; letter-spacing: -0.02em; }}
  .hero p {{ margin-top: 8px; color: rgba(255,255,255,0.84); font-size: 14px; }}
  .stats {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit,minmax(130px,1fr)); padding: 20px 36px 8px; }}
  .stat {{ background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); }}
  .stat .value {{ font-size: 28px; font-weight: 700; }}
  .stat .label {{ margin-top: 4px; text-transform: uppercase; font-size: 11px; letter-spacing: 0.08em; color: var(--text-soft); }}
  .section-hdr {{ padding: 20px 36px 6px; font-size: 15px; font-weight: 700; color: var(--text); }}
  .controls {{ display: flex; gap: 12px; padding: 14px 36px 6px; flex-wrap: wrap; }}
  .controls input, .controls select {{ border: 1px solid var(--border); border-radius: 999px; padding: 10px 16px; background: var(--surface); font-size: 14px; }}
  .table-wrap {{ padding: 10px 36px 40px; overflow-x: auto; }}
  .agg-table-wrap {{ padding: 0 36px 20px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow); overflow: hidden; font-size: 13px; }}
  .agg-table {{ max-width: 600px; }}
  thead th {{ background: var(--takeda-dark); color: white; padding: 11px 14px; text-align: left; white-space: nowrap; font-weight: 600; font-size: 12px; letter-spacing: 0.04em; }}
  thead th.env-hdr {{ text-align: center; background: rgba(0,0,0,0.18); }}
  tbody tr {{ border-bottom: 1px solid var(--border); }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--surface-alt); }}
  tbody td {{ padding: 9px 14px; vertical-align: middle; }}
  .agg-total td {{ background: rgba(16,24,40,0.04); font-weight: 700; border-top: 2px solid var(--border); }}
  tr.passed {{ background: rgba(12,143,90,0.04); }}
  tr.latency_regression, tr.both_changed {{ background: rgba(198,66,24,0.07); }}
  tr.response_changed {{ background: rgba(180,83,9,0.07); }}
  tr.concurrency_regression {{ background: rgba(139,0,10,0.06); }}
  tr.dev_failed {{ background: rgba(143,29,29,0.10); }}
  tr.error, tr.test_env_error {{ background: rgba(107,114,128,0.06); }}
  .badge {{ display: inline-block; border-radius: 999px; padding: 4px 10px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; white-space: nowrap; }}
  .badge.passed {{ background: rgba(12,143,90,0.12); color: var(--pass); }}
  .badge.latency_regression, .badge.both_changed {{ background: rgba(198,66,24,0.12); color: var(--fail); }}
  .badge.response_changed {{ background: rgba(180,83,9,0.12); color: var(--warn); }}
  .badge.concurrency_regression {{ background: rgba(139,0,10,0.12); color: var(--error); }}
  .badge.dev_failed {{ background: rgba(143,29,29,0.12); color: var(--error); }}
  .badge.error, .badge.test_env_error {{ background: rgba(107,114,128,0.12); color: var(--skip); }}
  .delta-pos {{ color: var(--fail); font-weight: 600; }}
  .delta-neg {{ color: var(--pass); font-weight: 600; }}
  .delta-neu {{ color: var(--text-soft); }}
  .bar-wrap {{ display: flex; align-items: center; gap: 4px; }}
  .bar {{ height: 7px; border-radius: 4px; min-width: 2px; flex-shrink: 0; }}
  .bar.dev {{ background: var(--takeda-red); }}
  .bar.tst {{ background: #94a3b8; }}
  .name-cell {{ max-width: 240px; }}
  .name-cell strong {{ display: block; font-size: 13px; }}
  .name-cell small {{ color: var(--text-soft); font-size: 11px; }}
  tr.hidden {{ display: none; }}
  details summary {{ cursor: pointer; font-size: 11px; color: var(--text-soft); }}
  .raw-times {{ font-family: ui-monospace, monospace; font-size: 11px; color: var(--text-soft); word-break: break-all; max-width: 180px; }}
  .schema-ok {{ color: var(--pass); }}
  .schema-fail {{ color: var(--fail); font-size: 11px; }}
</style>
</head>
<body>
  <section class="hero">
    <h1>{html.escape(title)}</h1>
    <p>{html.escape(subtitle)}</p>
    <p>Generated at {html.escape(generated_at)} &nbsp;|&nbsp; Services: {len(services_seen)}</p>
  </section>

  <section class="stats">
    <div class="stat"><div class="value">{total}</div><div class="label">Cases</div></div>
    <div class="stat"><div class="value" style="color:var(--pass)">{passed}</div><div class="label">Passed</div></div>
    <div class="stat"><div class="value" style="color:var(--fail)">{lat_reg}</div><div class="label">Latency Regression</div></div>
    <div class="stat"><div class="value" style="color:var(--warn)">{resp_chg}</div><div class="label">Schema Mismatch</div></div>
    <div class="stat"><div class="value" style="color:var(--error)">{conc_reg}</div><div class="label">Concurrency Regression</div></div>
    <div class="stat"><div class="value" style="color:var(--error)">{dev_failed}</div><div class="label">Dev Failed</div></div>
    <div class="stat"><div class="value" style="color:var(--skip)">{errors}</div><div class="label">Errors</div></div>
    <div class="stat"><div class="value">{len(services_seen)}</div><div class="label">Services</div></div>
  </section>

  <p class="section-hdr">Latency Summary by Service</p>
  <div class="agg-table-wrap">
    <table class="agg-table">
      <thead>
        <tr>
          <th>Service</th>
          <th>{html.escape(dev_env)} avg</th>
          <th>{html.escape(test_env)} avg</th>
          <th>Cases</th>
        </tr>
      </thead>
      <tbody>
        {svc_agg_rows}
        <tr class="agg-total">
          <td>Environment Total</td>
          <td>{env_dev_avg_display}</td>
          <td>{env_test_avg_display}</td>
          <td style="color:var(--text-soft)">{total}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <p class="section-hdr">Per-API Results</p>
  <section class="controls">
    <input id="search" type="search" placeholder="Search case, service&hellip;">
    <select id="outcomeFilter">
      <option value="all">All outcomes</option>
      <option value="passed">Passed</option>
      <option value="latency_regression">Latency Regression</option>
      <option value="response_changed">Schema Mismatch</option>
      <option value="both_changed">Both Changed</option>
      <option value="concurrency_regression">Concurrency Regression</option>
      <option value="dev_failed">Dev Failed</option>
      <option value="error">Error</option>
    </select>
    <select id="serviceFilter">
      <option value="all">All services</option>
      {''.join(f'<option value="{html.escape(a)}">{html.escape(a)}</option>' for a in services_seen)}
    </select>
  </section>

  <div class="table-wrap">
    <table id="lt-table">
      <thead>
        <tr>
          <th rowspan="2">Case</th>
          <th rowspan="2">Service</th>
          <th rowspan="2">Outcome</th>
          <th rowspan="2">OK / Runs</th>
          <th class="env-hdr" colspan="5">{html.escape(dev_env)}</th>
          <th class="env-hdr" colspan="5">{html.escape(test_env)}</th>
          <th rowspan="2">Δ p95 %</th>
          <th rowspan="2">Err rate Δ</th>
          <th rowspan="2">Schema</th>
          <th rowspan="2">Latency bars</th>
        </tr>
        <tr style="background:rgba(0,0,0,0.14);">
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">avg</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">p50</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">p90</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">p95</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">per-run</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">avg</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">p50</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">p90</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">p95</th>
          <th style="font-size:11px;font-weight:500;padding:6px 14px;">per-run</th>
        </tr>
      </thead>
      <tbody id="ec-tbody"></tbody>
    </table>
  </div>

  <script id="records-data" type="application/json">{data_blob}</script>
  <script>
    const records = JSON.parse(document.getElementById("records-data").textContent);
    const maxP95 = {max_p95_json};
    const maxBarPx = 100;
    const tbody = document.getElementById("ec-tbody");
    const search = document.getElementById("search");
    const outcomeFilter = document.getElementById("outcomeFilter");
    const serviceFilter = document.getElementById("serviceFilter");

    function ms(v) {{ return v == null ? "—" : v + " ms"; }}
    function pct(v) {{ return v == null ? "—" : v + "%"; }}
    function deltaCell(p, threshold) {{
      if (p == null) return '<td class="delta-neu">—</td>';
      const sign = p > 0 ? "+" : "";
      const cls = p > threshold ? "delta-pos" : p < -5 ? "delta-neg" : "delta-neu";
      return `<td class="${{cls}}">${{sign}}${{p}}%</td>`;
    }}
    function rawTimesCell(arr) {{
      if (!arr || arr.length === 0) return '<td>—</td>';
      const formatted = arr.map(v => v + "ms").join(", ");
      return `<td><details><summary>${{arr.length}} runs</summary><div class="raw-times">${{formatted}}</div></details></td>`;
    }}
    function barCell(r) {{
      const devP95  = r.dev_p95_ms  || 0;
      const testP95 = r.test_p95_ms || 0;
      const devW  = Math.round((devP95  / maxP95) * maxBarPx);
      const testW = Math.round((testP95 / maxP95) * maxBarPx);
      return `<td><div class="bar-wrap">
        <div class="bar dev"  style="width:${{devW}}px"  title="{html.escape(dev_env)} p95: ${{devP95}}ms"></div>
        <div class="bar tst"  style="width:${{testW}}px" title="{html.escape(test_env)} p95: ${{testP95}}ms"></div>
        <span style="font-size:11px;color:#6b7280">${{devP95}}ms / ${{testP95}}ms</span>
      </div></td>`;
    }}
    function schemaCell(r) {{
      if (r.schema_match) return '<td class="schema-ok">Match ✓</td>';
      const diffs = (r.structural_diffs || []).slice(0, 3).join("\\n");
      return `<td class="schema-fail">Mismatch ✗<br><small>${{diffs}}</small></td>`;
    }}

    function render() {{
      const term = search.value.toLowerCase().trim();
      const oc   = outcomeFilter.value;
      const svc  = serviceFilter.value;
      const rows = records.map(r => {{
        if (term && !JSON.stringify(r).toLowerCase().includes(term)) return null;
        if (oc  !== "all" && r.outcome  !== oc)  return null;
        if (svc !== "all" && r.service  !== svc) return null;

        const okLabel = `${{r.dev_success_count}}/${{r.dev_runs}} | ${{r.test_success_count}}/${{r.test_runs}}`;
        const errDelta = r.error_rate_delta_pct;
        const errDeltaCls = errDelta != null && errDelta > 5 ? "delta-pos" : "delta-neu";
        const errDeltaStr = errDelta == null ? "—" : (errDelta > 0 ? "+" : "") + errDelta + "%";
        return `<tr class="${{r.outcome}}">
          <td class="name-cell"><strong>${{r.name}}</strong><small>${{r.case_id}}</small></td>
          <td>${{r.service}}</td>
          <td><span class="badge ${{r.outcome}}">${{r.outcome.replaceAll("_"," ")}}</span></td>
          <td style="font-size:12px">${{okLabel}}</td>
          <td>${{ms(r.dev_avg_ms)}}</td>
          <td>${{ms(r.dev_p50_ms)}}</td>
          <td>${{ms(r.dev_p90_ms)}}</td>
          <td>${{ms(r.dev_p95_ms)}}</td>
          ${{rawTimesCell(r.dev_raw_elapsed_ms)}}
          <td>${{ms(r.test_avg_ms)}}</td>
          <td>${{ms(r.test_p50_ms)}}</td>
          <td>${{ms(r.test_p90_ms)}}</td>
          <td>${{ms(r.test_p95_ms)}}</td>
          ${{rawTimesCell(r.test_raw_elapsed_ms)}}
          ${{deltaCell(r.latency_delta_pct, 20)}}
          <td class="${{errDeltaCls}}">${{errDeltaStr}}</td>
          ${{schemaCell(r)}}
          ${{barCell(r)}}
        </tr>`;
      }}).filter(Boolean);
      tbody.innerHTML = rows.join("");
    }}

    search.addEventListener("input", render);
    outcomeFilter.addEventListener("change", render);
    serviceFilter.addEventListener("change", render);
    render();
  </script>
</body>
</html>
"""


def save_env_compare_reports(
    destination: Path,
    label: str,
    dev_env: str,
    test_env: str,
    services: dict[str, ServiceConfig],
    results: list[EnvCompareResult],
    load_results: list[EnvCompareLoadResult] | None = None,
) -> dict[str, Path]:
    """Persist env-compare JSON artifacts and HTML dashboards."""
    generated_at = iso_now()
    paths: dict[str, Path] = {}

    # Single-pass comparison
    ec_path = destination / "env_compare_results.json"
    write_json(ec_path, [asdict(r) for r in results])
    paths["env_compare_results"] = ec_path

    dashboard_path = destination / "Env Compare Dashboard.html"
    dashboard_path.write_text(
        render_env_compare_dashboard_html(label, dev_env, test_env, services, results, generated_at),
        encoding="utf-8",
    )
    paths["env_compare_dashboard"] = dashboard_path

    # Load test comparison (optional)
    if load_results is not None:
        lt_path = destination / "env_compare_load_results.json"
        write_json(lt_path, [asdict(r) for r in load_results])
        paths["env_compare_load_results"] = lt_path

        lt_dashboard_path = destination / "Env Compare Load Test Dashboard.html"
        lt_dashboard_path.write_text(
            render_env_compare_load_dashboard_html(
                label, dev_env, test_env, services, load_results, generated_at
            ),
            encoding="utf-8",
        )
        paths["env_compare_load_dashboard"] = lt_dashboard_path

    return paths
