SOC escalation rollup is broken again. Repair `/app/workflow/export_report.py` so it produces the escalation queue expected from `/app/data/events.json`.

Context lives in `/app/incident/export_dossier.md` (noisy incident archive). Use it for evidence quotes, but treat `/app/docs/report_spec.json` as the source of truth for required IDs, schemas, and report fields.

Build `/app/log_audit.py` with `diagnose` and `repair` subcommands. `diagnose` computes input stats from `/app/data/events.json` and must not include repaired-only fields. `repair` patches the workflow in place, runs it, and writes outputs under `--output-dir` (default `/app/output`).

Do not modify `/app/workflow/.export_report.original`. Diagnosis and repair evidence must include literal snippets from the frozen original workflow and verbatim dossier quotes. Include every allowed issue ID from spec in both modes.

After repair, write exactly:
- `summary.json`
- `service_matrix.json`
- `flagged.jsonl`
- `diagnosis.json`
- `repair_audit.json`

Processing requirements:
- Canonicalize alert records (timestamp normalization, severity/asset-group normalization, signature normalization, muted normalization).
- Deduplicate by `alert_id` using deterministic tie-breaks defined in spec.
- Keep muted alerts in canonical totals, but exclude muted alerts from escalation output.
- Escalate only `high` and `critical` severities.
- Use deterministic descending recency ordering for escalations with documented tie-breaks.
- Keep output formatting and schemas exactly as specified.
