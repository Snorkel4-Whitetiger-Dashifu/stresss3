# Recover the SOC escalation export

The escalation rollup deployed during an incident is producing an unreliable responder queue. Please investigate the failure, restore `/app/workflow/export_report.py`, and add `/app/log_audit.py` so an operator can diagnose the broken deployment and perform a repeatable repair.

Use the incident dossier and the frozen workflow snapshot as your evidence. Do not modify the frozen snapshot. The repaired export must handle alternate alert inputs, remain deterministic across reruns, and preserve the existing command and file locations used by operations.

The implementation guide is `/app/docs/output_contract.md`. It describes the diagnostic and repair commands, required artifacts, evidence rules, and processing stages. `/app/docs/report_spec.json` is the authoritative reference for exact schemas, tie-breaks, interval behavior, campaign and influence calculations, ordering, and checksum payloads. Follow both documents rather than inferring behavior from the sample data.

When you are done, run:

`python3 /app/log_audit.py repair --output-dir /app/output`

Leave that repaired result in `/app/output` for the responder handoff.
