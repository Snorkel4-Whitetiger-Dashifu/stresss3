#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "${SCRIPT_DIR}/log_audit.py" /app/log_audit.py
cp "${SCRIPT_DIR}/export_report_fixed.py" /app/export_report_fixed.py
chmod +x /app/log_audit.py
mkdir -p /app/output /app/audit

# Ensure frozen broken snapshot exists for pre-repair audit reads.
if [ ! -f /app/workflow/.export_report.original ]; then
  cp /app/workflow/export_report.py /app/workflow/.export_report.original
  chmod a-w /app/workflow/.export_report.original
fi

python3 /app/log_audit.py diagnose \
  --dossier /app/incident/export_dossier.md \
  --report /app/output/diagnosis.json

python3 /app/log_audit.py repair --output-dir /app/output

# Compatibility copy for harnesses that read /app/audit.
cp /app/output/diagnosis.json /app/audit/diagnosis.json
cp /app/output/repair_audit.json /app/audit/repair_audit.json
