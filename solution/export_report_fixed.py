#!/usr/bin/env python3
"""Export corrected SOC summary and escalation rows."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION = "siem-rollup-v2"
ESCALATION_SEVERITIES = {"high", "critical"}
SEVERITY_ORDER = ("critical", "high", "medium", "low")
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
OVERRIDES_PATH = Path("/app/data/escalation_overrides.json")
SUPPORTED_OVERRIDE_SCOPES = {"all", "high", "critical"}


def load_events(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def load_overrides(path: Path = OVERRIDES_PATH) -> list[dict]:
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


def _normalize_override_scope(value: object) -> str:
    normalized = str(value if value is not None else "").strip().lower()
    return normalized if normalized in SUPPORTED_OVERRIDE_SCOPES else ""


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


def _compact_overrides(
    rows: list[dict],
) -> dict[tuple[str, str], list[tuple[int, int]]]:
    by_key: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for row in rows:
        asset_group = _normalize_asset_group(row.get("asset_group", ""))
        scope = _normalize_override_scope(row.get("severity_scope", ""))
        if not scope:
            continue
        start_ms = _normalize_observed_ms(row.get("start_ms", 0))
        end_ms = _normalize_observed_ms(row.get("end_ms", 0))
        if end_ms <= start_ms:
            continue
        by_key.setdefault((asset_group, scope), []).append((start_ms, end_ms))

    compacted: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for key, intervals in by_key.items():
        merged: list[list[int]] = []
        for start_ms, end_ms in sorted(intervals):
            if not merged or start_ms > merged[-1][1]:
                merged.append([start_ms, end_ms])
            else:
                merged[-1][1] = max(merged[-1][1], end_ms)
        compacted[key] = [(start_ms, end_ms) for start_ms, end_ms in merged]
    return compacted


def _is_override_suppressed(
    event: dict,
    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]],
) -> bool:
    asset_group = _normalize_asset_group(event.get("asset_group", ""))
    severity = _normalize_severity(event.get("severity", ""))
    observed_ms = _normalize_observed_ms(event.get("observed_ms", 0))
    for scope in ("all", severity):
        for start_ms, end_ms in compacted_overrides.get((asset_group, scope), []):
            if start_ms <= observed_ms < end_ms:
                return True
    return False


def _override_compaction_checksum(
    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]]
) -> str:
    return hashlib.sha256(
        "\n".join(
            f"{asset_group}|{scope}|{start_ms}|{end_ms}"
            for asset_group, scope in sorted(compacted_overrides)
            for start_ms, end_ms in compacted_overrides[(asset_group, scope)]
        ).encode("utf-8")
    ).hexdigest()


def export_report(events: list[dict], output_dir: Path, override_rows: list[dict]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical = canonicalize_events(events)
    compacted_overrides = _compact_overrides(override_rows)

    severity_counts = {severity: 0 for severity in SEVERITY_ORDER}
    asset_groups: set[str] = set()
    for event in canonical:
        severity = _normalize_severity(event.get("severity", ""))
        if severity in severity_counts:
            severity_counts[severity] += 1
        asset_groups.add(_normalize_asset_group(event.get("asset_group", "")))

    escalations = []
    override_excluded_count = 0
    for event in canonical:
        if not is_escalation(event):
            continue
        if _is_override_suppressed(event, compacted_overrides):
            override_excluded_count += 1
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
        "override_excluded_count": override_excluded_count,
        "override_compaction_checksum": _override_compaction_checksum(compacted_overrides),
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
    override_rows = load_overrides()
    export_report(events, Path(args.output_dir), override_rows)
    print(f"Wrote report to {args.output_dir}")


if __name__ == "__main__":
    main()
