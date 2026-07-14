# Recover the SOC escalation export

The escalation rollup deployed during an incident is producing an unreliable responder queue. Please investigate the failure, restore `/app/workflow/export_report.py`, and add `/app/log_audit.py` with two subcommands: `diagnose --dossier PATH --report PATH` and `repair --output-dir PATH`. Diagnose is stateless — every explicit call writes a diagnosed report no matter what state the workflow or earlier repairs are in — and its diagnosis must cover all six known deployment defects: `wrong_observed_field`, `severity_filter`, `recency_order`, `severity_normalization`, `dedupe_alert`, and `muted_filter`. A repair run must leave `summary.json`, `service_matrix.json`, compact JSON-lines `flagged.jsonl`, `diagnosis.json`, and `repair_audit.json` in the output directory (`/app/output` by default).

Use the incident dossier at `/app/incident/export_dossier.md` and the frozen workflow snapshot as your evidence. Do not modify the frozen snapshot at `/app/workflow/.export_report.original` — the pre-repair SHA-256 in the repair audit is read from those frozen bytes, and the forbidden source tokens the repair must remove are listed in the spec. The repaired export must handle alternate alert inputs, remain deterministic across reruns, and preserve the existing command and file locations used by operations.

The implementation guide is `/app/docs/output_contract.md`. It describes the diagnostic and repair commands, required artifacts, evidence rules, and processing stages. `/app/docs/report_spec.json` is the authoritative reference for exact schemas and key sets — including `input_stats`, `verified_summary`, and `repair_audit.json` — plus checksum serialization and digest payloads; read it in full rather than skimming, since the exact key names matter. How the export actually behaves — normalization, tie-breaks, override handling, campaign and influence calculations — was settled during the SOC review and lives in the dossier's ticketed decision notes; most of the dossier is noise, and earlier positions were revisited more than once, so follow the latest decision on each point.

When you are done, run:

`python3 /app/log_audit.py repair --output-dir /app/output`

Leave that repaired result in `/app/output` for the responder handoff.
