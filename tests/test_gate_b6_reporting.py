from __future__ import annotations

import hashlib
import json
from pathlib import Path

import nbformat


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "model_selection" / "t1_b6_expanded_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_b6_analytics_and_figures_are_complete_and_hash_valid() -> None:
    analytics = json.loads((ARTIFACT / "analytics_summary.json").read_text(encoding="utf-8"))
    chart_map = json.loads((ARTIFACT / "chart_map.json").read_text(encoding="utf-8"))
    manifest = json.loads((ARTIFACT / "figure_manifest.json").read_text(encoding="utf-8"))
    qa = json.loads((ARTIFACT / "visual_qa_report.json").read_text(encoding="utf-8"))

    assert analytics["status"] == "PASS_NO_NEW_PRIMARY"
    assert analytics["primary_model_id"] == "B7_two_regime_imm"
    assert analytics["registry_models"] == 23
    assert analytics["executed_models"] == 22
    assert analytics["excluded_models"] == ["Z15_tabpfn_v2_6"]
    assert analytics["model_training_calls"] == 0
    assert len(chart_map["charts"]) == len(manifest["figures"]) == 7
    assert chart_map["model_training_calls"] == manifest["model_training_calls"] == 0
    for record in manifest["figures"]:
        path = ROOT / record["path"]
        assert not Path(record["path"]).is_absolute()
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert path.stat().st_size == record["bytes"]
        assert _sha256(path) == record["sha256"]
        assert record["width_px"] >= 1200
        assert record["height_px"] >= 1000
    assert qa["status"] == "PASS"
    assert qa["figure_manifest_sha256"] == _sha256(ARTIFACT / "figure_manifest.json")
    assert qa["reviewed_figures"] == 7


def test_b6_executed_notebook_is_artifact_only() -> None:
    path = ROOT / "notebooks" / "07_gate_b6_model_comparison.ipynb"
    notebook = nbformat.read(path, as_version=4)
    assert notebook.metadata["artifact_only_audit"]["model_training"] is False
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    assert code_cells and all(cell.execution_count is not None for cell in code_cells)
    assert not any(
        output.get("output_type") == "error"
        for cell in code_cells
        for output in cell.get("outputs", [])
    )
    source = "\n".join(cell.source for cell in code_cells)
    for forbidden in ("fit(", "run_gate_b6", "load_split_dataset", "t1_v1/validation", "t1_v1/test"):
        assert forbidden not in source
    assert sum(
        output.get("output_type") in {"display_data", "execute_result"}
        for cell in code_cells
        for output in cell.get("outputs", [])
    ) >= 10


def test_reader_report_and_model_cards_exist() -> None:
    report = ROOT / "docs" / "reports" / "GATE_B6_EXPANDED_SCREENING_RU.md"
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "PASS_NO_NEW_PRIMARY" in text
    assert "train_only_internal_research" in text
    assert text.count("t1_b6_expanded_v1/figures/") == 7
    for filename in (
        "B7_TWO_REGIME_IMM.md",
        "B8_STUDENT_T_ROBUST_IMM.md",
        "Z01_ELASTIC_NET.md",
    ):
        assert (ROOT / "docs" / "model_cards" / filename).is_file()
