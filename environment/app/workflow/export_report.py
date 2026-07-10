#!/usr/bin/env python3
"""Broken SOC escalation workflow used for repair task."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SCHEMA_VERSION = "siem-rollup-v2"


def load_events(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def export_report(events: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    severity_counts = {name: 0 for name in ("critical", "high", "medium", "low")}
    asset_groups: set[str] = set()
    for event in events:
        severity = str(event.get("severity", ""))
        if severity in severity_counts:
            severity_counts[severity] += 1
        asset_groups.add(str(event.get("asset_group", "")))

    escalations = []
    for event in events:
        severity = event.get("severity")
        if severity == "critical":
            escalations.append(
                {
                    "alert_id": event["alert_id"],
                    "observed_ms": event["observed_at"] if "observed_at" in event else 0,
                    "severity": event["severity"],
                    "asset_group": event["asset_group"],
                    "signature": event["signature"],
                }
            )

    escalations.sort(key=lambda row: row["observed_ms"])

    summary = {
        "schema_version": SCHEMA_VERSION,
        "raw_alert_count": len(events),
        "unique_alert_ids": len({str(event["alert_id"]) for event in events}),
        "total_alerts": len(events),
        "severity_counts": severity_counts,
        "asset_groups": sorted(asset_groups),
        "escalated_count": len(escalations),
        "muted_excluded_count": 0,
    }

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output_dir / "service_matrix.json").write_text(json.dumps({}, indent=2) + "\n")
    with (output_dir / "flagged.jsonl").open("w", encoding="utf-8") as handle:
        for row in escalations:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="/app/data/events.json")
    parser.add_argument("--output-dir", default="/app/output")
    args = parser.parse_args()

    events = load_events(Path(args.input))
    export_report(events, Path(args.output_dir))
    print(f"Wrote report to {args.output_dir}")


if __name__ == "__main__":
    main()
