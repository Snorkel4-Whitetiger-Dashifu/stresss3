"""Verify SOC audit CLI and repaired escalation workflow."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

OUTPUT_DIR = Path("/app/output")
DIAGNOSIS_PATH = OUTPUT_DIR / "diagnosis.json"
SUMMARY_PATH = OUTPUT_DIR / "summary.json"
MATRIX_PATH = OUTPUT_DIR / "service_matrix.json"
FLAGGED_PATH = OUTPUT_DIR / "flagged.jsonl"
REPAIR_AUDIT_PATH = OUTPUT_DIR / "repair_audit.json"
CLI = Path("/app/log_audit.py")
PIPELINE = Path("/app/workflow/export_report.py")
ORIGINAL_PIPELINE = Path("/app/workflow/.export_report.original")
DOSSIER_PATH = Path("/app/incident/export_dossier.md")
INPUT_PATH = Path("/app/data/events.json")
OVERRIDES_PATH = Path("/app/data/escalation_overrides.json")
REPORT_SPEC_PATH = Path("/app/docs/report_spec.json")
ALT_INPUT = Path("/tests/fixtures/alt_events.json")
BROKEN_PIPELINE_SHA256 = "ffb6652557a3021cea0b63c2c67b218a6c554b429d969c38efce23663fbe0fae"
SPEC_DATA = json.loads(REPORT_SPEC_PATH.read_text())
ISSUE_EVIDENCE_TERMS = SPEC_DATA["diagnosis_report"]["issues_found_item"]["evidence"][
    "required_terms_by_issue"
]
REQUIRED_ISSUE_IDS = SPEC_DATA["diagnosis_report"]["issues_found_item"]["allowed_ids"]
FORBIDDEN_TOKENS = ('event["observed_at"]', 'severity == "critical"')
ESCALATION_SEVERITIES = {"high", "critical"}
SEVERITY_ORDER = ("critical", "high", "medium", "low")
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _normalize_ws(text: str) -> str:
    return " ".join(text.split())


def _executable_text(src: str) -> str:
    docstring_lines: set[int] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                end = getattr(first, "end_lineno", first.lineno)
                docstring_lines.update(range(first.lineno, end + 1))

    lines: list[str] = []
    for line_number, line in enumerate(src.splitlines(), start=1):
        if line_number in docstring_lines:
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def _load_events(path: Path) -> list[dict]:
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
    return normalized if normalized in {"all", "high", "critical"} else ""


def _normalize_muted(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


def _severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _canonicalize_events(events: list[dict]) -> list[dict]:
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


def _is_escalation(event: dict) -> bool:
    if _normalize_muted(event.get("muted", False)):
        return False
    return _normalize_severity(event.get("severity", "")) in ESCALATION_SEVERITIES


def _build_service_matrix(events: list[dict]) -> dict[str, dict[str, int]]:
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
        start = _normalize_observed_ms(row.get("start_ms", 0))
        end = _normalize_observed_ms(row.get("end_ms", 0))
        if end <= start:
            continue
        by_key.setdefault((asset_group, scope), []).append((start, end))

    compacted: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for key, intervals in by_key.items():
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        compacted[key] = [(start, end) for start, end in merged]
    return compacted


def _is_override_suppressed(
    event: dict,
    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]],
) -> bool:
    asset_group = _normalize_asset_group(event.get("asset_group", ""))
    severity = _normalize_severity(event.get("severity", ""))
    observed_ms = _normalize_observed_ms(event.get("observed_ms", 0))
    for scope in ("all", severity):
        for start, end in compacted_overrides.get((asset_group, scope), []):
            if start <= observed_ms < end:
                return True
    return False


def _override_compaction_checksum(
    compacted_overrides: dict[tuple[str, str], list[tuple[int, int]]]
) -> str:
    return hashlib.sha256(
        "\n".join(
            f"{asset_group}|{scope}|{start}|{end}"
            for asset_group, scope in sorted(compacted_overrides)
            for start, end in compacted_overrides[(asset_group, scope)]
        ).encode("utf-8")
    ).hexdigest()


def _probe_overlap_ms(observed_ms: int, spans: list[tuple[int, int]], lookback_ms: int = 120) -> int:
    probe_start = observed_ms - lookback_ms
    probe_end = observed_ms + 1
    total = 0
    for start, end in spans:
        overlap_start = max(probe_start, start)
        overlap_end = min(probe_end, end)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return total


def _annotate_campaigns(rows: list[dict]) -> None:
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    tokens = [set(str(row["signature"]).lower().split()) for row in rows]
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            if abs(rows[left]["observed_ms"] - rows[right]["observed_ms"]) > 600:
                continue
            if (
                rows[left]["asset_group"] == rows[right]["asset_group"]
                or len(tokens[left] & tokens[right]) >= 2
            ):
                union(left, right)

    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(find(index), []).append(index)
    for indexes in components.values():
        alert_ids = sorted(str(rows[index]["alert_id"]) for index in indexes)
        observed = [rows[index]["observed_ms"] for index in indexes]
        assets = {rows[index]["asset_group"] for index in indexes}
        span_ms = max(observed) - min(observed)
        risk_score = (
            sum(_severity_rank(rows[index]["severity"]) for index in indexes)
            + (len(assets) * 2)
            + (span_ms // 60)
        )
        campaign_id = hashlib.sha1(",".join(alert_ids).encode("utf-8")).hexdigest()[:10]
        campaign_digest = hashlib.sha256(
            (
                f"{campaign_id}|{len(indexes)}|{span_ms}|{risk_score}|"
                f"{','.join(alert_ids)}"
            ).encode("utf-8")
        ).hexdigest()[:12]
        for index in indexes:
            rows[index]["campaign_id"] = campaign_id
            rows[index]["campaign_size"] = len(indexes)
            rows[index]["campaign_span_ms"] = span_ms
            rows[index]["campaign_risk_score"] = risk_score
            rows[index]["campaign_digest"] = campaign_digest


def _annotate_campaign_influence(rows: list[dict]) -> None:
    campaigns: dict[str, dict] = {}
    for index, row in enumerate(rows):
        campaign = campaigns.setdefault(
            row["campaign_id"],
            {
                "indexes": [],
                "start_ms": row["observed_ms"],
                "end_ms": row["observed_ms"],
                "assets": set(),
                "tokens": set(),
                "risk_score": row["campaign_risk_score"],
            },
        )
        campaign["indexes"].append(index)
        campaign["start_ms"] = min(campaign["start_ms"], row["observed_ms"])
        campaign["end_ms"] = max(campaign["end_ms"], row["observed_ms"])
        campaign["assets"].add(row["asset_group"])
        campaign["tokens"].update(str(row["signature"]).lower().split())

    finalized: list[tuple[str, dict]] = []
    for campaign_id, campaign in sorted(
        campaigns.items(),
        key=lambda item: (item[1]["start_ms"], item[1]["end_ms"], item[0]),
    ):
        best_score = campaign["risk_score"]
        best_path = (campaign_id,)
        for predecessor_id, predecessor in finalized:
            gap_ms = campaign["start_ms"] - predecessor["end_ms"]
            if gap_ms <= 0 or gap_ms > 3000:
                continue
            shared_assets = len(campaign["assets"] & predecessor["assets"])
            shared_tokens = len(campaign["tokens"] & predecessor["tokens"])
            if shared_assets == 0 and shared_tokens == 0:
                continue
            edge_weight = (
                1
                + (2 * shared_assets)
                + shared_tokens
                + max(0, 3 - (gap_ms // 1000))
            )
            candidate_score = (
                predecessor["influence_score"] + edge_weight + campaign["risk_score"]
            )
            candidate_path = predecessor["influence_path"] + (campaign_id,)
            if candidate_score > best_score or (
                candidate_score == best_score and candidate_path < best_path
            ):
                best_score = candidate_score
                best_path = candidate_path
        campaign["influence_score"] = best_score
        campaign["influence_path"] = best_path
        campaign["influence_depth"] = len(best_path) - 1
        campaign["influence_digest"] = hashlib.sha256(
            (
                f"{campaign_id}|{best_score}|{campaign['influence_depth']}|"
                f"{','.join(best_path)}"
            ).encode("utf-8")
        ).hexdigest()[:12]
        finalized.append((campaign_id, campaign))

    for _, campaign in finalized:
        for index in campaign["indexes"]:
            rows[index]["campaign_influence_score"] = campaign["influence_score"]
            rows[index]["campaign_influence_depth"] = campaign["influence_depth"]
            rows[index]["campaign_influence_path"] = list(campaign["influence_path"])
            rows[index]["campaign_influence_digest"] = campaign["influence_digest"]


def _compute_summary(events: list[dict], override_rows: list[dict] | None = None) -> dict:
    canonical = _canonicalize_events(events)
    severity_counts = {severity: 0 for severity in SEVERITY_ORDER}
    asset_groups: set[str] = set()
    override_rows = (
        json.loads(OVERRIDES_PATH.read_text()) if override_rows is None else override_rows
    )
    compacted_overrides = _compact_overrides(override_rows)
    escalations = _compute_flagged(events, override_rows=override_rows)
    for event in canonical:
        severity = _normalize_severity(event.get("severity", ""))
        if severity in severity_counts:
            severity_counts[severity] += 1
        asset_groups.add(_normalize_asset_group(event.get("asset_group", "")))
    return {
        "schema_version": "siem-rollup-v2",
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
        "override_excluded_count": sum(
            1
            for event in canonical
            if _normalize_severity(event.get("severity", "")) in ESCALATION_SEVERITIES
            and not _normalize_muted(event.get("muted", False))
            and _is_override_suppressed(event, compacted_overrides)
        ),
        "override_compaction_checksum": _override_compaction_checksum(compacted_overrides),
        "max_override_pressure_score": max(
            (row["override_pressure_score"] for row in escalations),
            default=0,
        ),
        "campaign_count": len({row["campaign_id"] for row in escalations}),
        "max_campaign_risk_score": max(
            (row["campaign_risk_score"] for row in escalations),
            default=0,
        ),
        "campaign_digest_checksum": hashlib.sha256(
            "|".join(row["campaign_digest"] for row in escalations).encode("utf-8")
        ).hexdigest(),
        "max_campaign_influence_score": max(
            (row["campaign_influence_score"] for row in escalations),
            default=0,
        ),
        "campaign_influence_digest_checksum": hashlib.sha256(
            "|".join(
                row["campaign_influence_digest"] for row in escalations
            ).encode("utf-8")
        ).hexdigest(),
        "escalation_digest_checksum": hashlib.sha256(
            "|".join(row["escalation_digest"] for row in escalations).encode("utf-8")
        ).hexdigest(),
    }


def _compute_flagged(events: list[dict], override_rows: list[dict] | None = None) -> list[dict]:
    override_rows = (
        json.loads(OVERRIDES_PATH.read_text()) if override_rows is None else override_rows
    )
    compacted_overrides = _compact_overrides(override_rows)
    rows = []
    for event in _canonicalize_events(events):
        if not _is_escalation(event):
            continue
        if _is_override_suppressed(event, compacted_overrides):
            continue
        asset_group = _normalize_asset_group(event.get("asset_group", ""))
        severity = _normalize_severity(event.get("severity", ""))
        observed_ms = _normalize_observed_ms(event.get("observed_ms", 0))
        all_overlap_ms = _probe_overlap_ms(
            observed_ms, compacted_overrides.get((asset_group, "all"), [])
        )
        severity_overlap_ms = _probe_overlap_ms(
            observed_ms, compacted_overrides.get((asset_group, severity), [])
        )
        override_pressure_score = (all_overlap_ms // 30) + (severity_overlap_ms // 20)
        rows.append(
            {
                "alert_id": event["alert_id"],
                "observed_ms": observed_ms,
                "severity": severity,
                "asset_group": asset_group,
                "signature": _normalize_signature(event["signature"]),
                "override_pressure_score": override_pressure_score,
            }
        )
    _annotate_campaigns(rows)
    _annotate_campaign_influence(rows)
    for row in rows:
        row["escalation_digest"] = hashlib.sha1(
            (
                f"{row['alert_id']}|{row['observed_ms']}|{row['severity']}|"
                f"{row['asset_group']}|{row['signature']}|{row['override_pressure_score']}|"
                f"{row['campaign_id']}|{row['campaign_size']}|{row['campaign_span_ms']}|"
                f"{row['campaign_risk_score']}|{row['campaign_digest']}|"
                f"{row['campaign_influence_score']}|{row['campaign_influence_depth']}|"
                f"{','.join(row['campaign_influence_path'])}|"
                f"{row['campaign_influence_digest']}"
            ).encode("utf-8")
        ).hexdigest()[:12]
    rows.sort(
        key=lambda row: (
            -row["observed_ms"],
            -_severity_rank(row["severity"]),
            -row["campaign_risk_score"],
            -row["campaign_influence_score"],
            -row["override_pressure_score"],
            str(row["alert_id"]),
        )
    )
    return rows


def _run_pipeline(
    pipeline: Path = PIPELINE,
    input_path: Path = INPUT_PATH,
    output_dir: Path = OUTPUT_DIR,
) -> subprocess.CompletedProcess[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            "python3",
            str(pipeline),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _flagged_rows(path: Path = FLAGGED_PATH) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


@pytest.fixture(scope="module")
def expected() -> dict:
    """Compute every expected value independently from the operational inputs.

    Nothing here is hardcoded output: summaries, matrices, flagged rows and
    checksums are all derived from /app/data at test time, for both the primary
    and the alternate input.
    """
    events = _load_events(INPUT_PATH)
    summary = _compute_summary(events)
    flagged = _compute_flagged(events)
    alternate_events = _load_events(ALT_INPUT)
    alternate_summary = _compute_summary(alternate_events)
    alternate_flagged = _compute_flagged(alternate_events)
    return {
        **summary,
        "alert_count": len(events),
        "unique_ids": len({str(event["alert_id"]) for event in events}),
        "expected_service_matrix": _build_service_matrix(_canonicalize_events(events)),
        "expected_flagged_ids_desc": [row["alert_id"] for row in flagged],
        "expected_flagged_ms_desc": [row["observed_ms"] for row in flagged],
        "broken_pipeline_sha256": BROKEN_PIPELINE_SHA256,
        "alternate_input": str(ALT_INPUT),
        "alternate_expected": {
            **alternate_summary,
            "flagged_ids_desc": [row["alert_id"] for row in alternate_flagged],
        },
    }


@pytest.fixture(scope="module")
def dossier_text() -> str:
    return _normalize_ws(DOSSIER_PATH.read_text())


@pytest.fixture(scope="module")
def diagnosis() -> dict:
    assert DIAGNOSIS_PATH.exists(), (
        f"Missing {DIAGNOSIS_PATH}. Run: python3 {CLI} repair --output-dir /app/output"
    )
    return json.loads(DIAGNOSIS_PATH.read_text())


@pytest.fixture(scope="module")
def summary(diagnosis: dict) -> dict:
    assert SUMMARY_PATH.exists(), "missing summary.json"
    data = json.loads(SUMMARY_PATH.read_text())
    assert data == diagnosis["verified_summary"]
    return data


@pytest.fixture(scope="module")
def flagged_rows() -> list[dict]:
    assert FLAGGED_PATH.exists(), "missing flagged.jsonl"
    return _flagged_rows()


def test_override_checksum_contract_and_touching_merge():
    """Verify touching-window compaction and checksum serialization."""
    contract = SPEC_DATA["outputs"]["summary_json"]["override_checksum_serialization"]
    assert hashlib.sha256(
        contract["test_vector_payload"].encode("utf-8")
    ).hexdigest() == contract["test_vector_sha256"]
    compacted = _compact_overrides(
        [
            {
                "asset_group": "edge",
                "severity_scope": "high",
                "start_ms": 100,
                "end_ms": 160,
            },
            {
                "asset_group": "edge",
                "severity_scope": "high",
                "start_ms": 160,
                "end_ms": 220,
            },
        ]
    )
    assert compacted[("edge", "high")] == [(100, 220)]


def test_cli_exists():
    assert CLI.exists(), f"CLI not found at {CLI}"


def test_dossier_has_context():
    minimum = SPEC_DATA["context"]["minimum_line_count"]
    assert len(DOSSIER_PATH.read_text().splitlines()) >= minimum


def test_repair_produces_required_outputs():
    for path in (SUMMARY_PATH, MATRIX_PATH, FLAGGED_PATH, REPAIR_AUDIT_PATH):
        assert path.exists(), f"missing required output: {path}"


def test_diagnosis_schema_repaired(diagnosis: dict):
    for key in ("pipeline_status", "issues_found", "input_stats", "verified_summary", "output_paths"):
        assert key in diagnosis
    assert diagnosis["pipeline_status"] == "repaired"


def test_output_paths_exact(diagnosis: dict):
    paths = diagnosis["output_paths"]
    assert paths["summary_json"] == str(SUMMARY_PATH)
    assert paths["flagged_jsonl"] == str(FLAGGED_PATH)
    assert paths["service_matrix_json"] == str(MATRIX_PATH)


def test_issues_found_exactly_six_allowed_ids(diagnosis: dict):
    assert len(diagnosis["issues_found"]) == 6
    assert {item["id"] for item in diagnosis["issues_found"]} == set(REQUIRED_ISSUE_IDS)


def test_issue_item_required_fields(diagnosis: dict):
    for issue in diagnosis["issues_found"]:
        for key in ("id", "severity", "description", "resolution", "evidence"):
            assert key in issue


def test_issue_evidence(diagnosis: dict):
    original_pipeline = ORIGINAL_PIPELINE.read_text()
    issues = {item["id"]: item for item in diagnosis["issues_found"]}
    for issue_id, terms in ISSUE_EVIDENCE_TERMS.items():
        evidence = issues[issue_id]["evidence"]
        for key in ("dossier_quote", "pipeline_evidence", "repair_action"):
            assert key in evidence
            assert len(evidence[key]) >= 10
        assert len(evidence["dossier_quote"]) >= 30
        for term in terms["dossier_quote"]:
            assert term in evidence["dossier_quote"]
        for term in terms["pipeline_evidence"]:
            assert term in evidence["pipeline_evidence"]
        assert evidence["pipeline_evidence"] in original_pipeline
        for term in terms["repair_action"]:
            assert term in evidence["repair_action"]


def test_dossier_quotes_are_verbatim(diagnosis: dict, dossier_text: str):
    for issue in diagnosis["issues_found"]:
        quote = _normalize_ws(issue["evidence"]["dossier_quote"])
        assert quote in dossier_text


def test_input_stats(diagnosis: dict, expected: dict):
    stats = diagnosis["input_stats"]
    assert stats["alert_count"] == expected["alert_count"]
    assert stats["unique_alert_ids"] == expected["unique_ids"]
    assert stats["asset_groups"] == expected["asset_groups"]


def test_verified_summary_matches_independent_computation(diagnosis: dict, expected: dict):
    verified = diagnosis["verified_summary"]
    for key in (
        "schema_version",
        "raw_alert_count",
        "unique_alert_ids",
        "total_alerts",
        "severity_counts",
        "asset_groups",
        "escalated_count",
        "muted_excluded_count",
        "override_excluded_count",
        "override_compaction_checksum",
        "max_override_pressure_score",
        "campaign_count",
        "max_campaign_risk_score",
        "campaign_digest_checksum",
        "max_campaign_influence_score",
        "campaign_influence_digest_checksum",
        "escalation_digest_checksum",
    ):
        assert verified[key] == expected[key]
    assert list(verified["severity_counts"].keys()) == list(SEVERITY_ORDER)
    assert len(verified["campaign_digest_checksum"]) == 64
    assert len(verified["campaign_influence_digest_checksum"]) == 64
    assert len(verified["escalation_digest_checksum"]) == 64


def test_summary_computed_from_events(summary: dict):
    assert summary == _compute_summary(_load_events(INPUT_PATH))


def test_service_matrix_matches_independent_computation(expected: dict):
    matrix = json.loads(MATRIX_PATH.read_text())
    assert matrix == expected["expected_service_matrix"]
    assert matrix == _build_service_matrix(_canonicalize_events(_load_events(INPUT_PATH)))


def test_flagged_computed_from_events(flagged_rows: list[dict]):
    assert flagged_rows == _compute_flagged(_load_events(INPUT_PATH))


def test_flagged_sorted_descending(flagged_rows: list[dict], expected: dict):
    assert [row["alert_id"] for row in flagged_rows] == expected["expected_flagged_ids_desc"]
    assert [row["observed_ms"] for row in flagged_rows] == expected["expected_flagged_ms_desc"]


def test_flagged_severities(flagged_rows: list[dict]):
    for row in flagged_rows:
        assert row["severity"] in ESCALATION_SEVERITIES
        assert isinstance(row["override_pressure_score"], int)
        assert len(row["campaign_id"]) == 10
        assert isinstance(row["campaign_size"], int)
        assert isinstance(row["campaign_span_ms"], int)
        assert isinstance(row["campaign_risk_score"], int)
        assert len(row["campaign_digest"]) == 12
        assert isinstance(row["campaign_influence_score"], int)
        assert isinstance(row["campaign_influence_depth"], int)
        assert isinstance(row["campaign_influence_path"], list)
        assert len(row["campaign_influence_digest"]) == 12
        assert len(row["escalation_digest"]) == 12


def test_flagged_jsonl_compact_format():
    for line in FLAGGED_PATH.read_text().splitlines():
        if not line.strip():
            continue
        assert ": " not in line
        parsed = json.loads(line)
        assert json.dumps(parsed, separators=(",", ":")) == line


def test_original_snapshot_preserved(expected: dict):
    assert ORIGINAL_PIPELINE.exists()
    digest = hashlib.sha256(ORIGINAL_PIPELINE.read_bytes()).hexdigest()
    assert digest == expected["broken_pipeline_sha256"]
    original = ORIGINAL_PIPELINE.read_text()
    for token in FORBIDDEN_TOKENS:
        assert token in original
    assert ".lower(" not in original


def test_pipeline_does_not_reference_test_or_solution_artifacts():
    combined = PIPELINE.read_text() + "\n" + CLI.read_text()
    for token in (
        "/tests",
        "expected_summary.json",
        "alt_events.json",
        "/solution",
    ):
        assert token not in combined


def test_repair_runtime_does_not_read_tests_tree():
    with tempfile.TemporaryDirectory() as tmp:
        guard = Path(tmp) / "sitecustomize.py"
        guard.write_text(
            "\n".join(
                [
                    "import builtins",
                    "from pathlib import Path",
                    "_open = builtins.open",
                    "_text = Path.read_text",
                    "_bytes = Path.read_bytes",
                    "def _blocked(value):",
                    "    try: return '/tests' in str(Path(value).resolve())",
                    "    except Exception: return False",
                    "def guarded_open(file, *args, **kwargs):",
                    "    if _blocked(file): raise PermissionError(file)",
                    "    return _open(file, *args, **kwargs)",
                    "def guarded_text(self, *args, **kwargs):",
                    "    if _blocked(self): raise PermissionError(self)",
                    "    return _text(self, *args, **kwargs)",
                    "def guarded_bytes(self, *args, **kwargs):",
                    "    if _blocked(self): raise PermissionError(self)",
                    "    return _bytes(self, *args, **kwargs)",
                    "builtins.open = guarded_open",
                    "Path.read_text = guarded_text",
                    "Path.read_bytes = guarded_bytes",
                ]
            )
            + "\n"
        )
        out = Path(tmp) / "out"
        env = dict(os.environ)
        env["PYTHONPATH"] = tmp
        result = subprocess.run(
            [
                "python3",
                str(CLI),
                "repair",
                "--output-dir",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, result.stderr


def test_broken_snapshot_produces_wrong_export(expected: dict):
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "export_report.py"
        out = Path(tmp) / "out"
        shutil.copy(ORIGINAL_PIPELINE, broken)
        result = _run_pipeline(pipeline=broken, output_dir=out)
        assert result.returncode == 0, result.stderr
        summary = json.loads((out / "summary.json").read_text())
        flagged = _flagged_rows(out / "flagged.jsonl")
        assert summary != _compute_summary(_load_events(INPUT_PATH))
        assert flagged != _compute_flagged(_load_events(INPUT_PATH))
        assert all(row["observed_ms"] == 0 for row in flagged)


def test_pipeline_patched():
    ast.parse(PIPELINE.read_text())
    code = _executable_text(PIPELINE.read_text())
    for token in FORBIDDEN_TOKENS:
        assert token not in code


def test_repair_audit(diagnosis: dict, expected: dict, summary: dict):
    audit = json.loads(REPAIR_AUDIT_PATH.read_text())
    code = _executable_text(PIPELINE.read_text())
    assert audit["patched_workflow"] == str(PIPELINE)
    assert audit["processing_steps"] == SPEC_DATA["repair_audit"]["processing_steps"]
    assert audit["removed_tokens"] == {token: token not in code for token in FORBIDDEN_TOKENS}
    assert all(audit["removed_tokens"].values())
    assert audit["pre_repair"]["pipeline_source_sha256"] == expected["broken_pipeline_sha256"]
    assert audit["pre_repair"]["pipeline_tokens_present"] == {token: True for token in FORBIDDEN_TOKENS}
    assert audit["post_repair"]["escalated_count"] == summary["escalated_count"]
    assert audit["post_repair"]["rerun_escalated_count"] == summary["escalated_count"]


def test_pipeline_reruns_idempotently(summary: dict, flagged_rows: list[dict], tmp_path_factory):
    rerun_dir = tmp_path_factory.mktemp("rerun")
    result = _run_pipeline(output_dir=rerun_dir)
    assert result.returncode == 0, result.stderr
    rerun_summary = json.loads((rerun_dir / "summary.json").read_text())
    rerun_flagged = _flagged_rows(rerun_dir / "flagged.jsonl")
    assert rerun_summary == summary
    assert rerun_flagged == flagged_rows


def test_patched_pipeline_supports_alternate_input(expected: dict, tmp_path_factory):
    alt_dir = tmp_path_factory.mktemp("alt")
    alt_input = Path(expected["alternate_input"])
    result = _run_pipeline(input_path=alt_input, output_dir=alt_dir)
    assert result.returncode == 0, result.stderr
    summary = json.loads((alt_dir / "summary.json").read_text())
    flagged = _flagged_rows(alt_dir / "flagged.jsonl")
    events = _load_events(alt_input)
    assert summary == _compute_summary(events)
    assert flagged == _compute_flagged(events)
    alt = expected["alternate_expected"]
    assert summary["raw_alert_count"] == alt["raw_alert_count"]
    assert summary["escalated_count"] == alt["escalated_count"]
    assert summary["muted_excluded_count"] == alt["muted_excluded_count"]
    assert summary["override_excluded_count"] == alt["override_excluded_count"]
    assert summary["override_compaction_checksum"] == alt["override_compaction_checksum"]
    assert summary["campaign_count"] == alt["campaign_count"]
    assert summary["max_campaign_risk_score"] == alt["max_campaign_risk_score"]
    assert summary["campaign_digest_checksum"] == alt["campaign_digest_checksum"]
    assert summary["max_campaign_influence_score"] == alt[
        "max_campaign_influence_score"
    ]
    assert summary["campaign_influence_digest_checksum"] == alt[
        "campaign_influence_digest_checksum"
    ]
    assert summary["escalation_digest_checksum"] == alt[
        "escalation_digest_checksum"
    ]
    assert [row["alert_id"] for row in flagged] == alt["flagged_ids_desc"]


def test_cli_diagnose_subcommand(expected: dict, dossier_text: str):
    report = OUTPUT_DIR / "diagnosis_redundant.json"
    if report.exists():
        report.unlink()
    result = subprocess.run(
        [
            "python3",
            str(CLI),
            "diagnose",
            "--dossier",
            str(DOSSIER_PATH),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert report.exists(), f"diagnose failed (rc={result.returncode}): {result.stderr}"
    data = json.loads(report.read_text())
    assert data["pipeline_status"] == "diagnosed"
    assert "input_stats" in data
    assert data["input_stats"]["alert_count"] == expected["alert_count"]
    assert data["input_stats"]["unique_alert_ids"] == expected["unique_ids"]
    assert data["input_stats"]["asset_groups"] == expected["asset_groups"]
    for key in ("verified_summary", "output_paths"):
        assert key not in data
    assert {item["id"] for item in data["issues_found"]} == set(REQUIRED_ISSUE_IDS)
    for issue in data["issues_found"]:
        for key in ("id", "severity", "description", "resolution", "evidence"):
            assert key in issue
        for key in ("dossier_quote", "pipeline_evidence", "repair_action"):
            assert key in issue["evidence"]
            assert len(issue["evidence"][key]) >= 10
        quote = _normalize_ws(issue["evidence"]["dossier_quote"])
        assert quote in dossier_text


def test_repair_repatches_reset_workflow_with_custom_output_dir(
    tmp_path_factory, expected: dict
):
    custom_dir = tmp_path_factory.mktemp("custom_output")
    current = PIPELINE.read_text()
    try:
        shutil.copy(ORIGINAL_PIPELINE, PIPELINE)
        result = subprocess.run(
            ["python3", str(CLI), "repair", "--output-dir", str(custom_dir)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        repaired_source = PIPELINE.read_text()
        assert 'event["observed_at"]' not in repaired_source
        summary = json.loads((custom_dir / "summary.json").read_text())
        flagged = _flagged_rows(custom_dir / "flagged.jsonl")
        diagnosis = json.loads((custom_dir / "diagnosis.json").read_text())
        assert summary == _compute_summary(_load_events(INPUT_PATH))
        assert flagged == _compute_flagged(_load_events(INPUT_PATH))
        assert diagnosis["output_paths"]["summary_json"] == str(custom_dir / "summary.json")
        assert diagnosis["output_paths"]["flagged_jsonl"] == str(custom_dir / "flagged.jsonl")
        assert diagnosis["output_paths"]["service_matrix_json"] == str(custom_dir / "service_matrix.json")
        assert summary["escalated_count"] == expected["escalated_count"]
    finally:
        PIPELINE.write_text(current)


def test_dedupe_tie_break_severity_and_signature():
    events = [
        {
            "alert_id": "x1",
            "observed_ms": 100,
            "severity": "medium",
            "asset_group": "edge",
            "signature": "aaa",
            "muted": False,
        },
        {
            "alert_id": "x1",
            "observed_ms": 100,
            "severity": "HIGH",
            "asset_group": "edge",
            "signature": "bbb",
            "muted": False,
        },
        {
            "alert_id": "x1",
            "observed_ms": 100,
            "severity": "high",
            "asset_group": "edge",
            "signature": "zzz",
            "muted": False,
        },
    ]
    canonical = _canonicalize_events(events)
    assert len(canonical) == 1
    assert canonical[0]["severity"] == "high"
    assert canonical[0]["signature"] == "zzz"


def test_muted_string_normalization_excludes_escalation():
    events = [
        {
            "alert_id": "m1",
            "observed_ms": 100,
            "severity": "critical",
            "asset_group": "beta",
            "signature": "x",
            "muted": "true",
        },
        {
            "alert_id": "m2",
            "observed_ms": 110,
            "severity": "high",
            "asset_group": "beta",
            "signature": "y",
            "muted": "1",
        },
        {
            "alert_id": "m3",
            "observed_ms": 120,
            "severity": "critical",
            "asset_group": "beta",
            "signature": "z",
            "muted": False,
        },
    ]
    flagged = _compute_flagged(events)
    assert [row["alert_id"] for row in flagged] == ["m3"]


def test_flagged_sort_tie_breaks_by_severity_then_alert_id():
    events = [
        {
            "alert_id": "c2",
            "observed_ms": 500,
            "severity": "critical",
            "asset_group": "m",
            "signature": "c2",
            "muted": False,
        },
        {
            "alert_id": "h1",
            "observed_ms": 500,
            "severity": "high",
            "asset_group": "m",
            "signature": "h1",
            "muted": False,
        },
        {
            "alert_id": "c1",
            "observed_ms": 500,
            "severity": "critical",
            "asset_group": "m",
            "signature": "c1",
            "muted": False,
        },
    ]
    flagged = _compute_flagged(events)
    assert [row["alert_id"] for row in flagged] == ["c1", "c2", "h1"]


def test_pipeline_coerces_observed_ms_and_normalizes_outputs(tmp_path_factory):
    events = [
        {
            "alert_id": "p1",
            "observed_ms": " 200 ",
            "severity": " CRITICAL ",
            "asset_group": " Core ",
            "signature": " first   signature ",
            "muted": "no",
        },
        {
            "alert_id": "p2",
            "observed_ms": "not-a-number",
            "severity": "high",
            "asset_group": "core",
            "signature": "second",
            "muted": False,
        },
        {
            "alert_id": "p3",
            "observed_ms": 150,
            "severity": "high",
            "asset_group": "core",
            "signature": "muted row",
            "muted": "yes",
        },
    ]
    input_path = tmp_path_factory.mktemp("coerce") / "events.json"
    input_path.write_text(json.dumps(events))
    out_dir = tmp_path_factory.mktemp("coerce_out")
    result = _run_pipeline(input_path=input_path, output_dir=out_dir)
    assert result.returncode == 0, result.stderr

    summary = json.loads((out_dir / "summary.json").read_text())
    flagged = _flagged_rows(out_dir / "flagged.jsonl")
    matrix = json.loads((out_dir / "service_matrix.json").read_text())

    assert summary["asset_groups"] == ["core"]
    assert summary["escalated_count"] == 2
    assert summary["muted_excluded_count"] == 1
    assert [row["alert_id"] for row in flagged] == ["p1", "p2"]
    assert [row["observed_ms"] for row in flagged] == [200, 0]
    assert flagged[0]["signature"] == "first signature"
    assert matrix == {"core": {"critical": 1, "high": 2, "medium": 0, "low": 0}}


def test_pipeline_dedupe_tie_break_prefers_non_muted_then_signature(tmp_path_factory):
    events = [
        {
            "alert_id": "d1",
            "observed_ms": 100,
            "severity": "high",
            "asset_group": "m",
            "signature": "zzz",
            "muted": "yes",
        },
        {
            "alert_id": "d1",
            "observed_ms": 100,
            "severity": "high",
            "asset_group": "m",
            "signature": "aaa",
            "muted": False,
        },
        {
            "alert_id": "d1",
            "observed_ms": 100,
            "severity": "high",
            "asset_group": "m",
            "signature": "bbb",
            "muted": "0",
        },
    ]
    input_path = tmp_path_factory.mktemp("dedupe") / "events.json"
    input_path.write_text(json.dumps(events))
    out_dir = tmp_path_factory.mktemp("dedupe_out")
    result = _run_pipeline(input_path=input_path, output_dir=out_dir)
    assert result.returncode == 0, result.stderr

    flagged = _flagged_rows(out_dir / "flagged.jsonl")
    summary = json.loads((out_dir / "summary.json").read_text())

    assert summary["total_alerts"] == 1
    assert summary["muted_excluded_count"] == 0
    assert [row["alert_id"] for row in flagged] == ["d1"]
    assert flagged[0]["signature"] == "bbb"


def test_override_source_path_affects_output(tmp_path_factory):
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        base_dir = tmp_path_factory.mktemp("base_override")
        base_result = _run_pipeline(output_dir=base_dir)
        assert base_result.returncode == 0, base_result.stderr
        base_summary = json.loads((base_dir / "summary.json").read_text())
        base_flagged = _flagged_rows(base_dir / "flagged.jsonl")

        OVERRIDES_PATH.write_text("[]\n")
        no_override_dir = tmp_path_factory.mktemp("no_override")
        no_override_result = _run_pipeline(output_dir=no_override_dir)
        assert no_override_result.returncode == 0, no_override_result.stderr
        no_override_summary = json.loads((no_override_dir / "summary.json").read_text())
        no_override_flagged = _flagged_rows(no_override_dir / "flagged.jsonl")

        assert base_summary["override_excluded_count"] > 0
        assert no_override_summary["override_excluded_count"] == 0
        assert (
            base_summary["override_compaction_checksum"]
            != no_override_summary["override_compaction_checksum"]
        )
        assert len(no_override_flagged) > len(base_flagged)
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_override_compaction_and_scope_exercised(tmp_path_factory):
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        override_rows = [
            {"asset_group": "edge", "severity_scope": "high", "start_ms": 100, "end_ms": 160},
            {"asset_group": "edge", "severity_scope": "high", "start_ms": 160, "end_ms": 200},
            {"asset_group": "edge", "severity_scope": "all", "start_ms": 220, "end_ms": 260},
            {"asset_group": "edge", "severity_scope": "debug", "start_ms": 0, "end_ms": 1},
        ]
        OVERRIDES_PATH.write_text(json.dumps(override_rows, indent=2) + "\n")
        events = [
            {
                "alert_id": "o1",
                "observed_ms": 120,
                "severity": "high",
                "asset_group": "edge",
                "signature": "silenced high",
                "muted": False,
            },
            {
                "alert_id": "o2",
                "observed_ms": 120,
                "severity": "critical",
                "asset_group": "edge",
                "signature": "kept critical",
                "muted": False,
            },
            {
                "alert_id": "o3",
                "observed_ms": 230,
                "severity": "critical",
                "asset_group": "edge",
                "signature": "silenced all",
                "muted": False,
            },
            {
                "alert_id": "o4",
                "observed_ms": 280,
                "severity": "high",
                "asset_group": "edge",
                "signature": "kept high",
                "muted": False,
            },
        ]
        input_path = tmp_path_factory.mktemp("override_scope") / "events.json"
        input_path.write_text(json.dumps(events))
        out_dir = tmp_path_factory.mktemp("override_scope_out")
        result = _run_pipeline(input_path=input_path, output_dir=out_dir)
        assert result.returncode == 0, result.stderr

        summary = json.loads((out_dir / "summary.json").read_text())
        flagged = _flagged_rows(out_dir / "flagged.jsonl")
        assert summary["override_excluded_count"] == 2
        assert [row["alert_id"] for row in flagged] == ["o4", "o2"]
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_campaign_correlation_is_transitive_across_bridge_alerts(tmp_path_factory):
    """Require full connected components rather than direct-neighbor groups."""
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        OVERRIDES_PATH.write_text("[]\n")
        events = [
            {
                "alert_id": "c1",
                "observed_ms": 100,
                "severity": "critical",
                "asset_group": "edge",
                "signature": "alpha beta one",
                "muted": False,
            },
            {
                "alert_id": "c2",
                "observed_ms": 250,
                "severity": "high",
                "asset_group": "core",
                "signature": "alpha beta two",
                "muted": False,
            },
            {
                "alert_id": "c3",
                "observed_ms": 400,
                "severity": "high",
                "asset_group": "core",
                "signature": "gamma delta",
                "muted": False,
            },
        ]
        input_path = tmp_path_factory.mktemp("campaign") / "events.json"
        input_path.write_text(json.dumps(events))
        out_dir = tmp_path_factory.mktemp("campaign_out")
        result = _run_pipeline(input_path=input_path, output_dir=out_dir)
        assert result.returncode == 0, result.stderr
        rows = _flagged_rows(out_dir / "flagged.jsonl")
        assert {row["campaign_id"] for row in rows} == {rows[0]["campaign_id"]}
        assert {row["campaign_size"] for row in rows} == {3}
        assert {row["campaign_span_ms"] for row in rows} == {300}
        assert {row["campaign_risk_score"] for row in rows} == {19}
        summary = json.loads((out_dir / "summary.json").read_text())
        assert summary["campaign_count"] == 1
        assert summary["max_campaign_risk_score"] == 19
    finally:
        OVERRIDES_PATH.write_text(original_overrides)


def test_campaign_influence_propagates_over_strongest_directed_path(tmp_path_factory):
    """Verify strongest-path dynamic programming across campaign nodes."""
    original_overrides = OVERRIDES_PATH.read_text()
    try:
        OVERRIDES_PATH.write_text("[]\n")
        events = [
            {
                "alert_id": "i1",
                "observed_ms": 100,
                "severity": "critical",
                "asset_group": "edge",
                "signature": "alpha one",
                "muted": False,
            },
            {
                "alert_id": "i2",
                "observed_ms": 1000,
                "severity": "critical",
                "asset_group": "edge",
                "signature": "beta two",
                "muted": False,
            },
            {
                "alert_id": "i3",
                "observed_ms": 2000,
                "severity": "critical",
                "asset_group": "core",
                "signature": "beta gamma",
                "muted": False,
            },
        ]
        input_path = tmp_path_factory.mktemp("influence") / "events.json"
        input_path.write_text(json.dumps(events))
        out_dir = tmp_path_factory.mktemp("influence_out")
        result = _run_pipeline(input_path=input_path, output_dir=out_dir)
        assert result.returncode == 0, result.stderr
        rows = {
            row["alert_id"]: row
            for row in _flagged_rows(out_dir / "flagged.jsonl")
        }
        assert rows["i1"]["campaign_influence_score"] == 6
        assert rows["i2"]["campaign_influence_score"] == 18
        assert rows["i3"]["campaign_influence_score"] == 28
        assert rows["i3"]["campaign_influence_depth"] == 2
        assert rows["i3"]["campaign_influence_path"] == [
            rows["i1"]["campaign_id"],
            rows["i2"]["campaign_id"],
            rows["i3"]["campaign_id"],
        ]
        summary = json.loads((out_dir / "summary.json").read_text())
        assert summary["max_campaign_influence_score"] == 28
    finally:
        OVERRIDES_PATH.write_text(original_overrides)
