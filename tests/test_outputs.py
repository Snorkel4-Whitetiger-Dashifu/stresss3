"""Verify SOC audit CLI and repaired escalation workflow."""

from __future__ import annotations

import ast
import hashlib
import json
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
REPORT_SPEC_PATH = Path("/app/docs/report_spec.json")
FIXTURES = Path("/tests/fixtures/expected_summary.json")
SPEC_DATA = json.loads(REPORT_SPEC_PATH.read_text())
FIXTURE_DATA = json.loads(FIXTURES.read_text())
ISSUE_EVIDENCE_TERMS = SPEC_DATA["diagnosis_report"]["issues_found_item"]["evidence"][
    "required_terms_by_issue"
]
REQUIRED_ISSUE_IDS = SPEC_DATA["diagnosis_report"]["issues_found_item"]["allowed_ids"]
FORBIDDEN_TOKENS = tuple(SPEC_DATA["repair_audit"]["forbidden_executable_tokens"])
REQUIRED_TOKENS = tuple(SPEC_DATA["workflow_repair"]["required_executable_tokens"])
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


def _compute_summary(events: list[dict]) -> dict:
    canonical = _canonicalize_events(events)
    severity_counts = {severity: 0 for severity in SEVERITY_ORDER}
    asset_groups: set[str] = set()
    escalations = _compute_flagged(events)
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
    }


def _compute_flagged(events: list[dict]) -> list[dict]:
    rows = []
    for event in _canonicalize_events(events):
        if not _is_escalation(event):
            continue
        rows.append(
            {
                "alert_id": event["alert_id"],
                "observed_ms": event["observed_ms"],
                "severity": _normalize_severity(event["severity"]),
                "asset_group": _normalize_asset_group(event["asset_group"]),
                "signature": _normalize_signature(event["signature"]),
            }
        )
    rows.sort(
        key=lambda row: (
            -row["observed_ms"],
            -_severity_rank(row["severity"]),
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
    return FIXTURE_DATA


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


def test_verified_summary_matches_fixture(diagnosis: dict, expected: dict):
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
    ):
        assert verified[key] == expected[key]
    assert list(verified["severity_counts"].keys()) == list(SEVERITY_ORDER)


def test_summary_computed_from_events(summary: dict):
    assert summary == _compute_summary(_load_events(INPUT_PATH))


def test_service_matrix_matches_fixture(expected: dict):
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


def test_broken_snapshot_produces_wrong_export(expected: dict):
    with tempfile.TemporaryDirectory() as tmp:
        broken = Path(tmp) / "export_report.py"
        out = Path(tmp) / "out"
        shutil.copy(ORIGINAL_PIPELINE, broken)
        result = _run_pipeline(pipeline=broken, output_dir=out)
        assert result.returncode == 0, result.stderr
        summary = json.loads((out / "summary.json").read_text())
        flagged = _flagged_rows(out / "flagged.jsonl")
        assert summary["escalated_count"] == expected["broken_flagged_count"]
        assert [row["alert_id"] for row in flagged] == expected["broken_flagged_ids_asc"]
        assert all(row["observed_ms"] == 0 for row in flagged)


def test_pipeline_patched():
    ast.parse(PIPELINE.read_text())
    code = _executable_text(PIPELINE.read_text())
    for token in FORBIDDEN_TOKENS:
        assert token not in code
    for token in REQUIRED_TOKENS:
        assert token in code


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


def test_repair_supports_custom_output_dir(tmp_path_factory, expected: dict):
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
