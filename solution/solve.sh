#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "${SCRIPT_DIR}/log_audit.py" /app/log_audit.py
cp "${SCRIPT_DIR}/export_report_fixed.py" /app/export_report_fixed.py
chmod +x /app/log_audit.py

python3 /app/log_audit.py diagnose \
  --dossier /app/incident/export_dossier.md \
  --report /app/output/diagnosis.json

python3 /app/log_audit.py repair --output-dir /app/output
