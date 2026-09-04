from __future__ import annotations

import hashlib
import ast
import json
from pathlib import Path
import zipfile

import nbformat


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts" / "model_selection" / "t1_gate_c1_compact_screen_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_gate_c1_reader_scripts_are_artifact_only() -> None:
    scripts = (
        ROOT / "scripts" / "build_gate_c1_figures.py",
        ROOT / "scripts" / "build_gate_c1_notebook.py",
        ROOT / "scripts" / "build_gate_c1_reader_materials.py",
    )
    imported = set()
    called = set()
    for path in scripts:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
    assert "torch" not in imported
    assert "skru1.gate_c1_models" not in imported
    assert "skru1.gate_c1_worker" not in imported
    assert "skru1.splits" not in imported
    assert "load_split_dataset" not in called


def test_gate_c1_reader_artifacts_reconcile() -> None:
    figure_manifest = json.loads((ARTIFACT / "figure_manifest.json").read_text(encoding="utf-8"))
    notebook_report = json.loads(
        (ARTIFACT / "notebook_execution_report.json").read_text(encoding="utf-8")
    )
    reader_manifest = json.loads(
        (ARTIFACT / "reader_materials_manifest.json").read_text(encoding="utf-8")
    )
    visual = json.loads((ARTIFACT / "visual_qa_report.json").read_text(encoding="utf-8"))
    assert figure_manifest["model_training_calls"] == 0
    assert len(figure_manifest["figures"]) == 4
    assert visual["status"] == "PASS"
    assert visual["manual_visual_review_completed"] is True
    assert notebook_report["status"] == "PASS"
    assert notebook_report["error_outputs"] == 0
    assert notebook_report["model_training_calls"] == 0
    assert reader_manifest["status"] == "PASS"
    assert reader_manifest["model_training_calls"] == 0
    for record in (*figure_manifest["figures"], *reader_manifest["outputs"]):
        path = ROOT / record["path"]
        assert path.is_file()
        assert _sha256(path) == record["sha256"]
    source_names = set(reader_manifest["sources"])
    assert {"checkpoints", "execution_incident"}.issubset(source_names)
    report = (ROOT / "docs" / "reports" / "GATE_C1_COMPACT_SEQUENCE_SCREEN_RU.md").read_text(
        encoding="utf-8"
    )
    assert "Checkpoint policy и CUDA-ускорение" in report
    assert "3\u00a0860" in report
    assert "outer labels" in report


def test_gate_c1_notebook_is_executed_and_contains_no_training_calls() -> None:
    path = ROOT / "notebooks" / "09_gate_c1_compact_sequence_screen.ipynb"
    notebook = nbformat.read(path, as_version=4)
    code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
    assert code_cells
    assert all(cell.execution_count is not None for cell in code_cells)
    assert not [
        output
        for cell in code_cells
        for output in cell.get("outputs", [])
        if output.get("output_type") == "error"
    ]
    source = "\n".join(cell.source.lower() for cell in code_cells)
    assert "import torch" not in source
    assert ".fit(" not in source
    assert "load_split_dataset" not in source
    assert notebook.metadata["artifact_only"] is True
    assert notebook.metadata["model_training_calls"] == 0


def test_special_section_source_map_reconciles_gate_c1() -> None:
    docx = ROOT / "docs" / "thesis" / "SPECIAL_SECTION_SKRU1_RU.docx"
    source_map = json.loads(
        (ROOT / "docs" / "thesis" / "SPECIAL_SECTION_SKRU1_RU_SOURCE_MAP.json").read_text(
            encoding="utf-8"
        )
    )
    assert zipfile.is_zipfile(docx)
    assert source_map["document_sha256"] == _sha256(docx)
    assert source_map["gate_c1_model_training_calls"] > 0
    assert source_map["gate_c1_reporting_training_calls"] == 0
    assert source_map["historical_validation_loaded_for_gate_c1"] is False
    assert source_map["current_test_loaded_for_gate_c1"] is False
    assert source_map["new_holdout_seen_for_gate_c1"] is False
    assert source_map["profile_zone_transition_audit_executed"] is False
    names = {item["name"] for item in source_map["sources"]}
    assert {
        "gate_c1_validation",
        "gate_c1_admission",
        "gate_c1_temporal_metrics",
        "gate_c1_seed_stability",
        "gate_c1_checkpoints",
        "gate_c1_execution_incident",
        "c1_temporal",
        "c1_rolling",
        "c1_seed",
    }.issubset(names)
