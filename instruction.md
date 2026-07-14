# Recover the SOC escalation export

The escalation rollup deployed during an incident is producing an unreliable responder queue. Please investigate the failure, restore `/app/workflow/export_report.py`, and add `/app/log_audit.py` with `diagnose` and `repair` subcommands so an operator can diagnose the broken deployment and perform a repeatable repair. A repair run must leave `summary.json`, `service_matrix.json`, `flagged.jsonl`, `diagnosis.json`, and `repair_audit.json` in the output directory (`/app/output` by default).

Use the incident dossier at `/app/incident/export_dossier.md` and the frozen workflow snapshot as your evidence. Do not modify the frozen snapshot. The repaired export must handle alternate alert inputs, remain deterministic across reruns, and preserve the existing command and file locations used by operations.

The implementation guide is `/app/docs/output_contract.md`. It describes the diagnostic and repair commands, required artifacts, evidence rules, and processing stages. `/app/docs/report_spec.json` is the authoritative reference for exact schemas, key sets, checksum serialization, and digest payloads. How the export actually behaves — normalization, tie-breaks, override handling, campaign and influence calculations — was settled during the SOC review and lives in the dossier's ticketed decision notes; most of the dossier is noise, and earlier triage proposals were partly reversed, so let later decisions govern.

When you are done, run:

`python3 /app/log_audit.py repair --output-dir /app/output`

Leave that repaired result in `/app/output` for the responder handoff.
