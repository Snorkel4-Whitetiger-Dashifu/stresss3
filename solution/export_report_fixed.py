#!/usr/bin/env python3
"""Export corrected SOC summary and escalation rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SCHEMA_VERSION = "siem-rollup-v2"
ESCALATION_SEVERITIES = {"high", "critical"}
SEVERITY_ORDER = ("critical", "high", "medium", "low")
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def load_events(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def _normalize_severity(value: object) -> str:
    return str(value if value is not None else "").strip().lower()


def _normalize_asset_group(value: object) -> str:
    return str(value if value is not None else "").strip().lower()


def _normalize_observed_ms(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            return 0
    return 0


def _normalize_signature(value: object) -> str:
    return " ".join(str(value if value is not None else "").split())


def _normalize_muted(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def canonicalize_events(events: list[dict]) -> list[dict]:
    deduped: dict[str, dict] = {}
    for event in events:
        normalized = dict(event)
        normalized["observed_ms"] = _normalize_observed_ms(normalized.get("observed_ms", 0))
        normalized["severity"] = _normalize_severity(normalized.get("severity", ""))
        normalized["asset_group"] = _normalize_asset_group(normalized.get("asset_group", ""))
        normalized["muted"] = _normalize_muted(normalized.get("muted", False))
        normalized["signature"] = _normalize_signature(normalized.get("signature", ""))
        alert_id = str(normalized["alert_id"])
        current = deduped.get(alert_id)
        if current is None:
            deduped[alert_id] = normalized
            continue
        replace = False
        if normalized["observed_ms"] > current["observed_ms"]:
            replace = True
        elif normalized["observed_ms"] == current["observed_ms"]:
            if _severity_rank(normalized["severity"]) > _severity_rank(current["severity"]):
                replace = True
            elif _severity_rank(normalized["severity"]) == _severity_rank(current["severity"]):
                if int(_normalize_muted(normalized.get("muted", False))) < int(
                    _normalize_muted(current.get("muted", False))
                ):
                    replace = True
                elif int(_normalize_muted(normalized.get("muted", False))) == int(
                    _normalize_muted(current.get("muted", False))
                ):
                    if _normalize_signature(normalized.get("signature", "")) > _normalize_signature(
                        current.get("signature", "")
                    ):
                        replace = True
                    elif _normalize_signature(normalized.get("signature", "")) == _normalize_signature(
                        current.get("signature", "")
                    ):
                        if _normalize_asset_group(
                            normalized.get("asset_group", "")
                        ) > _normalize_asset_group(current.get("asset_group", "")):
                            replace = True
        if replace:
            deduped[alert_id] = normalized
    return sorted(deduped.values(), key=lambda row: row["observed_ms"])


def is_escalation(event: dict) -> bool:
    if _normalize_muted(event.get("muted", False)):
        return False
    return _normalize_severity(event.get("severity", "")) in ESCALATION_SEVERITIES


def build_service_matrix(events: list[dict]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for event in events:
        asset_group = _normalize_asset_group(event.get("asset_group", ""))
        severity = _normalize_severity(event.get("severity", ""))
        matrix.setdefault(asset_group, {name: 0 for name in SEVERITY_ORDER})
        if severity in matrix[asset_group]:
            matrix[asset_group][severity] += 1
    return {asset_group: matrix[asset_group] for asset_group in sorted(matrix)}


def export_report(events: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical = canonicalize_events(events)

    severity_counts = {severity: 0 for severity in SEVERITY_ORDER}
    asset_groups: set[str] = set()
    for event in canonical:
        severity = _normalize_severity(event.get("severity", ""))
        if severity in severity_counts:
            severity_counts[severity] += 1
        asset_groups.add(_normalize_asset_group(event.get("asset_group", "")))

    escalations = []
    for event in canonical:
        if not is_escalation(event):
            continue
        escalations.append(
            {
                "alert_id": event["alert_id"],
                "observed_ms": event["observed_ms"],
                "severity": _normalize_severity(event["severity"]),
                "asset_group": _normalize_asset_group(event["asset_group"]),
                "signature": _normalize_signature(event["signature"]),
            }
        )
    escalations.sort(key=lambda row: str(row["alert_id"]))
    escalations.sort(key=lambda row: _severity_rank(row["severity"]), reverse=True)
    escalations.sort(key=lambda row: row["observed_ms"], reverse=True)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "raw_alert_count": len(events),
        "unique_alert_ids": len({str(event["alert_id"]) for event in events}),
        "total_alerts": len(canonical),
        "severity_counts": severity_counts,
        "asset_groups": sorted(asset_groups),
        "escalated_count": len(escalations),
        "muted_excluded_count": sum(
            1
            for event in canonical
            if _normalize_muted(event.get("muted", False))
            and _normalize_severity(event.get("severity", "")) in ESCALATION_SEVERITIES
        ),
    }

    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output_dir / "service_matrix.json").write_text(
        json.dumps(build_service_matrix(canonical), indent=2) + "\n"
    )
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
