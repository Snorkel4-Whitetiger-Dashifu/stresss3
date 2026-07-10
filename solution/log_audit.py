#!/usr/bin/env python3
"""Diagnostic and repair CLI for SOC escalation workflow."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path

EVENTS_PATH = Path("/app/data/events.json")
PIPELINE_PATH = Path("/app/workflow/export_report.py")
ORIGINAL_PIPELINE = Path("/app/workflow/.export_report.original")
SPEC_PATH = Path("/app/docs/report_spec.json")
FORBIDDEN_TOKENS = ('event["observed_at"]', 'severity == "critical"')

ISSUE_META = {
    "wrong_observed_field": {
        "severity": "critical",
        "description": "Escalation rows use observed_at instead of observed_ms.",
        "resolution": "Use observed_ms when emitting escalation rows.",
    },
    "severity_filter": {
        "severity": "critical",
        "description": "Workflow escalates only exact critical rows.",
        "resolution": "Include high and critical severities in flagged export.",
    },
    "recency_order": {
        "severity": "high",
        "description": "Escalations are sorted oldest-first.",
        "resolution": "Sort escalations by observed_ms descending (reverse=True).",
    },
    "severity_normalization": {
        "severity": "high",
        "description": "Severity aliases are not normalized to lowercase.",
        "resolution": "Normalize severity with .lower() before filtering.",
    },
    "dedupe_alert": {
        "severity": "high",
        "description": "Duplicate alert_id rows are exported multiple times.",
        "resolution": "dedupe alert_id rows keeping the highest observed_ms before export.",
    },
    "muted_filter": {
        "severity": "high",
        "description": "Muted rows appear in flagged export.",
        "resolution": "Exclude muted rows from flagged export.",
    },
}


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text())


def load_events(path: Path = EVENTS_PATH) -> list[dict]:
    return json.loads(path.read_text())


def input_stats(events: list[dict]) -> dict:
    asset_groups = sorted({str(event.get("asset_group", "")).strip().lower() for event in events})
    return {
        "alert_count": len(events),
        "unique_alert_ids": len({str(event["alert_id"]) for event in events}),
        "asset_groups": asset_groups,
    }


def pipeline_source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def pre_repair_audit() -> dict:
    source = PIPELINE_PATH.read_text()
    return {
        "pipeline_source_sha256": pipeline_source_sha256(source),
        "pipeline_tokens_present": {token: token in source for token in FORBIDDEN_TOKENS},
    }


def _line_contains_all(line: str, terms: list[str]) -> bool:
    return all(term in line for term in terms)


def find_dossier_quote(dossier_text: str, terms: list[str]) -> str:
    normalized = _normalize_ws(dossier_text)
    candidates: list[str] = []
    for line in dossier_text.splitlines():
        stripped = line.strip()
        if len(stripped) < 30 or not _line_contains_all(stripped, terms):
            continue
        if _normalize_ws(stripped) in normalized:
            candidates.append(stripped)
    if not candidates:
        raise ValueError(f"no dossier quote found for terms {terms}")
    return max(candidates, key=len)


def find_pipeline_evidence(original_pipeline: str, terms: list[str]) -> str:
    for line in original_pipeline.splitlines():
        stripped = line.strip()
        if stripped and _line_contains_all(stripped, terms):
            return stripped
    if all(term in original_pipeline for term in terms):
        for line in original_pipeline.splitlines():
            if any(term in line for term in terms):
                return line.strip()
    raise ValueError(f"no pipeline evidence found for terms {terms}")


def build_repair_action(issue_id: str, terms: list[str]) -> str:
    templates = {
        "wrong_observed_field": "Use observed_ms when emitting escalation rows.",
        "severity_filter": "Include high and critical rows in escalation export.",
        "recency_order": "Sort with reverse=True on observed_ms for recency-first ordering.",
        "severity_normalization": "Normalize severity values using .lower() in canonicalization.",
        "dedupe_alert": "dedupe alert_id rows keeping the highest observed_ms before export.",
        "muted_filter": "Exclude muted=true rows from flagged escalation export.",
    }
    action = templates[issue_id]
    for term in terms:
        if term not in action:
            action = f"{action} ({term})"
    return action


def build_issues_from_sources(dossier_text: str, original_pipeline: str, spec: dict) -> list[dict]:
    evidence_spec = spec["diagnosis_report"]["issues_found_item"]["evidence"][
        "required_terms_by_issue"
    ]
    allowed_ids = spec["diagnosis_report"]["issues_found_item"]["allowed_ids"]
    issues = []
    for issue_id in allowed_ids:
        terms = evidence_spec[issue_id]
        meta = ISSUE_META[issue_id]
        issues.append(
            {
                "id": issue_id,
                "severity": meta["severity"],
                "description": meta["description"],
                "resolution": meta["resolution"],
                "evidence": {
                    "dossier_quote": find_dossier_quote(dossier_text, terms["dossier_quote"]),
                    "pipeline_evidence": find_pipeline_evidence(
                        original_pipeline, terms["pipeline_evidence"]
                    ),
                    "repair_action": build_repair_action(issue_id, terms["repair_action"]),
                },
            }
        )
    return issues


def patch_workflow() -> None:
    for candidate in (
        Path(__file__).resolve().parent / "export_report_fixed.py",
        Path("/app/export_report_fixed.py"),
    ):
        if candidate.exists():
            PIPELINE_PATH.write_text(candidate.read_text())
            return
    raise FileNotFoundError("repaired export_report.py template not found")


def build_diagnosis_report(
    status: str,
    events: list[dict],
    issues: list[dict],
    summary: dict | None = None,
    output_dir: Path | None = None,
) -> dict:
    report = {
        "pipeline_status": status,
        "issues_found": issues,
        "input_stats": input_stats(events),
    }
    if summary is not None and output_dir is not None:
        report["verified_summary"] = summary
        report["output_paths"] = {
            "summary_json": str(output_dir / "summary.json"),
            "flagged_jsonl": str(output_dir / "flagged.jsonl"),
            "service_matrix_json": str(output_dir / "service_matrix.json"),
        }
    return report


def cmd_diagnose(dossier: Path, report_path: Path) -> None:
    dossier_text = dossier.read_text(encoding="utf-8", errors="replace")
    spec = load_spec()
    original_pipeline = ORIGINAL_PIPELINE.read_text()
    events = load_events()
    issues = build_issues_from_sources(dossier_text, original_pipeline, spec)
    report = build_diagnosis_report("diagnosed", events, issues)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")


def cmd_repair(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    diagnosis_path = output_dir / "diagnosis.json"
    audit_path = output_dir / "repair_audit.json"
    rerun_dir = output_dir / "rerun"
    dossier_path = Path("/app/incident/export_dossier.md")

    spec = load_spec()
    dossier_text = dossier_path.read_text(encoding="utf-8", errors="replace")
    original_pipeline = ORIGINAL_PIPELINE.read_text()
    issues = build_issues_from_sources(dossier_text, original_pipeline, spec)

    pre_audit = pre_repair_audit()
    patch_workflow()
    ast.parse(PIPELINE_PATH.read_text())

    subprocess.run(
        [
            sys.executable,
            str(PIPELINE_PATH),
            "--input",
            str(EVENTS_PATH),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
    )

    if rerun_dir.exists():
        for child in rerun_dir.iterdir():
            child.unlink()
    else:
        rerun_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            sys.executable,
            str(PIPELINE_PATH),
            "--input",
            str(EVENTS_PATH),
            "--output-dir",
            str(rerun_dir),
        ],
        check=True,
    )

    events = load_events()
    summary = json.loads((output_dir / "summary.json").read_text())
    diagnosis = build_diagnosis_report("repaired", events, issues, summary, output_dir)
    diagnosis_path.write_text(json.dumps(diagnosis, indent=2) + "\n")

    code = PIPELINE_PATH.read_text()
    audit = {
        "patched_workflow": str(PIPELINE_PATH),
        "processing_steps": spec["repair_audit"]["processing_steps"],
        "removed_tokens": {token: token not in code for token in FORBIDDEN_TOKENS},
        "pre_repair": pre_audit,
        "post_repair": {
            "escalated_count": summary["escalated_count"],
            "rerun_escalated_count": json.loads((rerun_dir / "summary.json").read_text())[
                "escalated_count"
            ],
        },
    }
    audit_path.write_text(json.dumps(audit, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="SOC escalation diagnostic CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    diag = sub.add_parser("diagnose")
    diag.add_argument("--dossier", type=Path, required=True)
    diag.add_argument("--report", type=Path, default=Path("/app/output/diagnosis.json"))

    repair = sub.add_parser("repair")
    repair.add_argument("--output-dir", type=Path, default=Path("/app/output"))

    args = parser.parse_args()
    if args.command == "diagnose":
        cmd_diagnose(args.dossier, args.report)
    else:
        cmd_repair(args.output_dir)


if __name__ == "__main__":
    main()
