SOC escalation rollup is broken again — `/app/workflow/export_report.py` is not producing the alert queue that security operations expects from `/app/data/events.json`.

Operational notes are in `/app/incident/export_dossier.md` (high-noise incident archive). Use it for evidence quotes, but treat `/app/docs/report_spec.json` as the normative source for schemas, required issue IDs, and acceptance constraints.

Build `/app/log_audit.py` with `diagnose` and `repair` subcommands per spec. `diagnose` uses `/app/data/events.json` for `input_stats` and must not include `verified_summary` or `output_paths`. `repair` patches `/app/workflow/export_report.py` in place, runs the repaired workflow, and writes outputs under `--output-dir` (default `/app/output`).

Keep `/app/workflow/.export_report.original` unchanged. Evidence must cite literal substrings from that frozen file and verbatim dossier quotes. `issues_found` must include every allowed issue id from spec on both diagnose and repair.

After repair, write exactly: `summary.json`, `service_matrix.json`, `flagged.jsonl`, `diagnosis.json`, and `repair_audit.json`. `service_matrix.json` is a flat map `{asset_group: {critical, high, medium, low}}` with no wrapper metadata. `flagged.jsonl` must be compact JSON lines (`json.dumps(..., separators=(",", ":"))`).

Canonicalization and queueing (per spec): coerce `observed_ms` to int (`str(...).strip()` before conversion; invalid values -> `0`), normalize `severity` and `asset_group` with `strip().lower()`, normalize `signature` by collapsing whitespace, and coerce muted-like strings (`"true"`, `"1"`, `"yes"`) to true. Deduplicate by `alert_id` keeping highest `observed_ms`; tie-break by severity rank `critical > high > medium > low`, then prefer non-muted rows, then lexicographically larger normalized `signature`, then normalized `asset_group`. `total_alerts` counts deduped rows (muted rows still counted in totals). Escalations include only high+critical, exclude muted rows, and sort by observed_ms descending, then severity rank descending, then alert_id ascending.
