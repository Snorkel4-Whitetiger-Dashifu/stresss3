# SOC escalation recovery contract

This guide connects the incident-recovery request to the precise machine contract in `report_spec.json`. The JSON specification remains authoritative whenever this guide summarizes a rule.

## Operator commands

Create `/app/log_audit.py` with these commands:

- `diagnose --dossier PATH --report PATH`
- `repair --output-dir PATH`

An explicit `diagnose` call is stateless. It always writes a diagnosed report, regardless of the current workflow or existing repair artifacts. A repair reinstalls the corrected workflow, runs it in the requested directory, reruns it to prove idempotency, and leaves a repaired diagnosis at the output location.

Use `/app/incident/export_dossier.md` and the frozen bytes at `/app/workflow/.export_report.original` as audit evidence. Never modify the frozen file.

## Diagnostic evidence

The diagnosis covers these six deployment defects:

- `wrong_observed_field`
- `severity_filter`
- `recency_order`
- `severity_normalization`
- `dedupe_alert`
- `muted_filter`

Each finding contains `id`, `severity`, `description`, `resolution`, and an `evidence` object containing `dossier_quote`, `pipeline_evidence`, and `repair_action`. Dossier quotations are verbatim and at least 30 characters. Pipeline evidence and repair actions are at least 10 characters. Required evidence terms are case-sensitive, including terms assembled into a repair action.

A diagnosed report contains only `pipeline_status`, `issues_found`, and `input_stats`, with status `diagnosed`. It does not contain `verified_summary` or `output_paths`. A repaired report has status `repaired`, embeds the generated summary, and uses the semantic path keys `summary_json`, `flagged_jsonl`, and `service_matrix_json`.

## Escalation processing

The workflow canonicalizes alerts and deterministically deduplicates them by `alert_id`. Muted or actively overridden candidates do not enter the responder queue. Override windows are normalized and compacted before overlap and pressure calculations.

Related alerts form transitive campaigns. Campaigns then participate in a directed influence graph whose strongest path is propagated in deterministic order. Campaign influence affects final row ordering, escalation digests, and summary checksums. Use `report_spec.json` for the exact normalization rules, tie-break cascade, interval boundaries, edge rules, equations, and hash serialization.

## Repair audit and artifacts

Every repair reads the pre-repair SHA-256 from the frozen bytes before replacing the active workflow. `repair_audit.json` contains `patched_workflow`, `processing_steps`, `removed_tokens`, `pre_repair`, and `post_repair`. Copy `processing_steps` from the JSON specification without paraphrasing it.

The removed-token map uses the exact source literals `event["observed_at"]` and `severity == "critical"`. The post-repair section records integer `escalated_count` and `rerun_escalated_count`.

The requested output directory contains exactly:

- `summary.json`
- `service_matrix.json`
- `flagged.jsonl`
- `diagnosis.json`
- `repair_audit.json`

Write `flagged.jsonl` as compact JSON Lines. All schemas, sort keys, field domains, checksums, and digest payloads are defined in `/app/docs/report_spec.json`.
