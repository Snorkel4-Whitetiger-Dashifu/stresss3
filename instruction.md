SOC operations needs a deterministic escalation rollup pipeline for on-call handoff. Repair `/app/workflow/export_report.py` so it produces escalation output expected from `/app/data/events.json`.

Context is in `/app/incident/export_dossier.md` (noisy archive). Use it for evidence quotes, but treat `/app/docs/report_spec.json` as the source of truth for IDs, schemas, and output contracts.

Build `/app/log_audit.py` with `diagnose` and `repair` subcommands:
- `diagnose` CLI must be `python3 /app/log_audit.py diagnose --dossier PATH --report PATH`
- `repair` CLI must be `python3 /app/log_audit.py repair --output-dir PATH`
- `diagnose` reads `/app/data/events.json` for input stats and must set `pipeline_status` to `diagnosed`
- `diagnose` must not include remediation-only keys `verified_summary` or `output_paths`
- `repair` must be self-contained: patch `/app/workflow/export_report.py` in place from the repaired template every run, then execute it, then write outputs under `--output-dir`
- `repair` must set `pipeline_status` to `repaired`
- in repair mode, `diagnosis.json` must include `verified_summary` as a full copy of `summary.json`
- in repair mode, `output_paths` keys are exact: `summary_json`, `flagged_jsonl`, `service_matrix_json`

Non-negotiable repair-audit contract:
- `repair_audit.json` top-level keys are exact: `patched_workflow`, `processing_steps`, `removed_tokens`, `pre_repair`, `post_repair`
- in both `removed_tokens` and `pre_repair.pipeline_tokens_present`, keys must be exact literal forbidden token strings:
  - `event["observed_at"]`
  - `severity == "critical"`
- `pre_repair.pipeline_source_sha256` and `pre_repair.pipeline_tokens_present` must be captured from `/app/workflow/.export_report.original` before any write to `/app/workflow/export_report.py`
- `repair` must honor provided `--output-dir` exactly for all generated outputs (`summary.json`, `service_matrix.json`, `flagged.jsonl`, `diagnosis.json`, `repair_audit.json`) and must not hardcode `/app/output`

Do not modify `/app/workflow/.export_report.original`. Audit evidence must include literal snippets from the frozen original workflow and verbatim dossier quotes. Include every allowed issue ID from spec in both modes.

Core processing requirements:
- canonicalize alerts (observed_ms, severity, asset_group, signature, muted) exactly per spec
- dedupe by `alert_id` with deterministic tie-break chain from spec
- keep muted alerts in canonical totals, but exclude muted alerts from escalation rows
- escalate only `high` and `critical` severities
- before escalation filtering, load escalation overrides from `/app/data/escalation_overrides.json` and apply severity-scoped suppression windows:
  - normalize override `asset_group` like alerts
  - normalize `severity_scope` with supported values `all`, `high`, `critical` (`all` matches both escalation severities)
  - normalize `start_ms`/`end_ms` with observed_ms coercion rules; drop overrides with `end_ms <= start_ms`
  - compact overlapping or touching intervals per `(asset_group, severity_scope)`
  - suppress escalation when `start_ms <= observed_ms < end_ms` for matching asset_group and scope
- sort escalations by observed_ms descending, severity rank descending, alert_id ascending
- `flagged.jsonl` must be compact JSON lines (`json.dumps(..., separators=(",", ":"))`, no space after `:`)

Summary requirements:
- include `override_excluded_count` (escalation candidates removed by override windows after muted exclusion)
- include `override_compaction_checksum` as lowercase SHA256 over compacted override rows in canonical order (`asset_group`, `severity_scope`, `start_ms`, `end_ms`) with line format:
  - `asset_group|severity_scope|start_ms|end_ms`

After remediation, write exactly these files in the output dir:
- `summary.json`
- `service_matrix.json`
- `flagged.jsonl`
- `diagnosis.json`
- `repair_audit.json`
