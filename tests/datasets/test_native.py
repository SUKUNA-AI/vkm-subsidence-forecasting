"""Every byte here is generated synthetic input; no corpus data is opened."""
from dataclasses import replace
import json
import os
from pathlib import Path
import zipfile

import pytest

from vkm_datasets import AccessClass, DatasetVersion, ExperimentalRole, Policy, Registry, discover, verify_members
from vkm_datasets.manifest import FileMember, confined
from vkm_datasets.workbooks import Limits, inspect_workbook


def version(root, entry, **kwargs):
    return DatasetVersion("SYNTHETIC", discover(root, (entry,)), (entry,),
                          Policy(AccessClass.PRIVATE_CLOUD_ALLOWED, ExperimentalRole.INPUT, "synthetic-owner"),
                          "synthetic", "synthetic-only", "2026-10-01T00:00:00Z", **kwargs)


def xlsx(path, *, malicious=False):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    def column(n):
        out = ""
        while n:
            n, digit = divmod(n - 1, 26)
            out = chr(65 + digit) + out
        return out
    # 257 columns, Cyrillic headers, a target in a hidden sheet, lexical strings and a cached formula.
    headers = ''.join(f'<c r="{column(i)}1" t="inlineStr"><is><t>Поле_{i}</t></is></c>' for i in range(1, 258))
    sheet = f'''<worksheet {ns}><dimension ref="A1:IW2"/><cols><col min="2" max="2" hidden="1"/></cols>
      <sheetData><row r="1">{headers}</row><row r="2" hidden="1">
      <c r="A2"><f>1+2</f><v>3</v></c><c r="B2" t="inlineStr"><is><t>12,50</t></is></c>
      <c r="C2" s="1"><v>45000</v></c><c r="D2"/><c r="E2" t="inlineStr"><is><t>000012...</t></is></c>
      <c r="F2" t="e"><v>#N/A</v></c></row></sheetData><mergeCells><mergeCell ref="G2:H2"/></mergeCells></worksheet>'''
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><workbookPr date1904="1"/><sheets>'
                   '<sheet name="Кириллица" sheetId="1" r:id="r1"/><sheet name="target" sheetId="2" state="veryHidden" r:id="r2"/>'
                   '</sheets><definedNames><definedName name="Target">target!A1</definedName></definedNames></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="r1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="r2" Target="worksheets/sheet2.xml"/>'
                   '<Relationship Id="ext" Target="https://invalid.example/never-request" TargetMode="External"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet {ns}><sheetData><row r="1"><c r="A1" t="inlineStr">'
                   '<is><t>SYNTHETIC_TARGET_CANARY</t></is></c></row></sheetData></worksheet>')
        z.writestr("xl/styles.xml", f'<styleSheet {ns}><numFmts><numFmt numFmtId="165" formatCode="yyyy-mm-dd"/>'
                   '</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="165"/></cellXfs></styleSheet>')
        if malicious:
            z.writestr("../escape", "never extracted")


def test_manifest_identity_immutable_replay_and_fresh_hash(tmp_path):
    root = tmp_path / "original"; root.mkdir()
    xlsx(root / "a.xlsx")
    v = version(root, "a.xlsx")
    registry = Registry(tmp_path / "registry")
    p = registry.register(v)
    assert registry.register(v) == p
    assert registry.load(v.dataset_id, v.digest) == v
    changed_policy = replace(v, policy=Policy("PRIVATE_LOCAL_ONLY", "INPUT", "new-owner-decision"), parents=(v.digest,))
    assert changed_policy.digest != v.digest
    registry.register(changed_policy)
    # Same length and mtime do not preserve integrity.
    file = root / "a.xlsx"; old = file.stat(); raw = file.read_bytes()
    file.write_bytes(bytes([raw[0] ^ 1]) + raw[1:]); os.utime(file, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_members(root, v.files)
    p.write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError):
        registry.load(v.dataset_id, v.digest)


def test_companions_are_complete_and_case_safe(tmp_path):
    (tmp_path / "Данные.TAB").write_text('!table\nDefinition Table\nType NATIVE Charset "WindowsCyrillic"', encoding="ascii")
    with pytest.raises(ValueError, match="missing MapInfo"):
        discover(tmp_path, ("Данные.TAB",))
    for ext in ("DAT", "MAP", "ID", "IND"):
        (tmp_path / ("Данные." + ext)).write_bytes(b"synthetic")
    files = discover(tmp_path, ("Данные.TAB",))
    assert len(files) == 5 and sum(f.role == "ORIGINAL" for f in files) == 1
    (tmp_path / "line.mif").write_text("synthetic", encoding="ascii")
    with pytest.raises(ValueError, match=".mid"):
        discover(tmp_path, ("line.mif",))


@pytest.mark.parametrize("bad", ["../bad", "a/../bad", "C:/bad", "a\\b", "a/CON.xlsx", "a./b", "/bad", "./bad"])
def test_rejects_traversal_aliases(bad, tmp_path):
    with pytest.raises(ValueError):
        confined(tmp_path, bad, must_exist=False)


def test_symlink_rejected(tmp_path):
    target = tmp_path / "target.xlsx"; target.write_bytes(b"x")
    link = tmp_path / "link.xlsx"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("NOT_RUN: host lacks symlink creation privilege")
    with pytest.raises(ValueError, match="symlinks"):
        discover(tmp_path, ("link.xlsx",))


def test_xlsx_preserves_native_semantics_and_hidden_inventory(tmp_path):
    xlsx(tmp_path / "a.xlsx")
    v = version(tmp_path, "a.xlsx")
    report = inspect_workbook(tmp_path, v, "a.xlsx", include_values=True)
    a, hidden = report["sheets"]
    assert hidden["state"] == "veryHidden" and report["date_system"] == "1904"
    assert len([c for c in a["cells"] if c["address"].endswith("1")]) == 257
    cells = {c["address"]: c for c in a["cells"]}
    assert cells["A2"]["formula"] == "1+2" and cells["A2"]["raw_value"] == "3"
    assert cells["B2"]["value"] == "12,50" and cells["C2"]["raw_value"] == "45000"
    assert cells["D2"]["cached_value_present"] is False and cells["D2"]["value"] is None
    assert cells["E2"]["value"] == "000012..." and cells["F2"]["native_type"] == "e"
    assert a["merged_cells"] == ["G2:H2"] and a["rows"][1]["hidden"] == "1"
    meta = inspect_workbook(tmp_path, v, "a.xlsx")
    assert "SYNTHETIC_TARGET_CANARY" not in json.dumps(meta)
    assert meta["formula_evaluation"] == "NOT_RUN"


def test_policy_denies_before_opening_and_roles_are_independent(tmp_path):
    xlsx(tmp_path / "a.xlsx")
    v = version(tmp_path, "a.xlsx")
    sealed = replace(v, policy=Policy("PRIVATE_CLOUD_ALLOWED", "TEST_SEALED", "experiment"))
    (tmp_path / "a.xlsx").unlink()
    with pytest.raises(PermissionError):
        inspect_workbook(tmp_path, sealed, "a.xlsx", include_values=True)
    Policy("PRIVATE_CLOUD_ALLOWED", "TARGET", "owner").require("cloud")
    with pytest.raises(PermissionError):
        Policy("PRIVATE_LOCAL_ONLY", "INPUT", "owner").require("cloud")
    with pytest.raises(PermissionError):
        v.policy.require("public")


def test_package_and_memory_guards(tmp_path):
    xlsx(tmp_path / "bad.xlsx", malicious=True)
    with pytest.raises(ValueError, match="unsafe"):
        inspect_workbook(tmp_path, version(tmp_path, "bad.xlsx"), "bad.xlsx")
    xlsx(tmp_path / "a.xlsx")
    v = version(tmp_path, "a.xlsx")
    with pytest.raises(ValueError, match="cell memory guard"):
        inspect_workbook(tmp_path, v, "a.xlsx", limits=Limits(max_cells=2))
    with pytest.raises(ValueError, match="uncompressed"):
        inspect_workbook(tmp_path, v, "a.xlsx", limits=Limits(max_uncompressed_bytes=10))


def test_unknown_crs_unit_stays_unknown():
    from vkm_world.spatial.crs import CoordinateSystem
    crs = CoordinateSystem(id="UNKNOWN", kind="UNKNOWN", provenance={"status": "UNKNOWN"})
    assert crs.units == "UNKNOWN"
    known = CoordinateSystem(id="KNOWN", kind="LOCAL_MINE_GRID", units="m", provenance={"status": "UNKNOWN"})
    assert known.units == "m"


def test_gcp_response_reports_actual_units(tmp_path, monkeypatch):
    import sqlite3
    from vkm_qgis import worker
    path = tmp_path / "synthetic.gpkg"
    with sqlite3.connect(path) as db:
        fields = {**worker.COMMON, **worker.GCP_FIELDS, "geometry": "blob"}
        columns = ','.join('"' + key + '" ' + ('BLOB' if key == 'geometry' else 'TEXT') for key in fields)
        for table in ("control_points", "control_point_matches"):
            db.execute('CREATE TABLE ' + table + ' (' + columns + ')')
        db.execute('CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, srs_id INTEGER)')
        db.execute("INSERT INTO gpkg_geometry_columns VALUES ('control_points','geometry',-1)")
        db.execute('CREATE TABLE gpkg_contents (table_name TEXT, min_x REAL, min_y REAL, max_x REAL, max_y REAL, last_change TEXT)')
    monkeypatch.setattr(worker, "_layer_meta", lambda *_: {"frame_id": "SYNTHETIC", "unit": "px"})
    class Paths:
        def resolve(self, *args, **kwargs):
            return path
    result = worker.register_gcps(Paths(), {"path": "synthetic.gpkg", "source_frame_id": "PIXEL",
                 "target_frame_id": "SYNTHETIC", "target_unit": "px", "target_unit_verified": False,
                 "points": [{"id": "a", "source": [0, 0], "target": [10, 10]},
                            {"id": "b", "source": [1, 1], "target": [20, 20]}]})
    assert result["target_unit"] == "px" and result["target_unit_verified"] is False


def test_xml_entities_rejected_even_in_utf16():
    from vkm_datasets.workbooks import _xml
    with pytest.raises(ValueError, match="DTD"):
        _xml('<!DOCTYPE test [<!ENTITY example "bad">]><test>&example;</test>'.encode("utf-16"))


def test_cli_integrity_failure_is_nonzero(tmp_path, capsys):
    from vkm_datasets.cli import main
    xlsx(tmp_path / "a.xlsx")
    v = version(tmp_path, "a.xlsx")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(v.as_dict()), encoding="utf-8")
    args = ["verify", "--root", str(tmp_path), "--manifest", str(manifest)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    (tmp_path / "a.xlsx").write_bytes(b"changed")
    assert main(args) == 2
    assert "hash mismatch" in capsys.readouterr().err


def test_unsupported_worksheet_namespace_is_not_an_empty_success(tmp_path):
    path = tmp_path / "a.xlsx"
    xlsx(path)
    with zipfile.ZipFile(path) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(
        b"http://schemas.openxmlformats.org/spreadsheetml/2006/main", b"urn:unsupported:sheet")
    with zipfile.ZipFile(path, "w") as target:
        for name, payload in parts.items():
            target.writestr(name, payload)
    with pytest.raises(ValueError, match="worksheet namespace/type"):
        inspect_workbook(tmp_path, version(tmp_path, "a.xlsx"), "a.xlsx")


def test_conversion_final_hash_failure_is_rejected_and_cli_nonzero(tmp_path, monkeypatch, capsys):
    """Fault after successful comparison, before the receipt can commit. No GDAL workload."""
    import sqlite3
    from types import SimpleNamespace
    from vkm_datasets import gis
    from vkm_datasets.cli import main
    root = tmp_path / "original"
    root.mkdir()
    (root / "a.gpkg").write_bytes(b"synthetic mocked native input")
    v = version(root, "a.gpkg")
    def translate(path, *args, **kwargs):
        db = sqlite3.connect(path)
        try:
            db.execute("CREATE TABLE synthetic(id INTEGER)")
            db.commit()
        finally:
            db.close()
        return object()
    monkeypatch.setattr(gis, "_engine", lambda: (SimpleNamespace(
        VersionInfo=lambda _: "synthetic", VectorTranslate=translate), None))
    monkeypatch.setattr(gis, "_open", lambda _: object())
    monkeypatch.setattr(gis, "_scan", lambda *args: ([], []))
    monkeypatch.setattr(gis, "_compare", lambda *args: ([], []))
    def failed_hash(path):
        raise OSError("synthetic finalization fault")
    monkeypatch.setattr(gis, "sha256", failed_hash)
    out = tmp_path / "bundle"
    receipt = gis.convert_to_gpkg(root, v, "a.gpkg", out)
    assert receipt["status"] == "REJECTED"
    assert "synthetic finalization fault" in str(receipt["errors"])
    assert not (out / "data.gpkg").exists()
    assert (out / "candidate.gpkg").exists()
    with pytest.raises(ValueError, match="incomplete, rejected"):
        gis.verify_conversion_bundle(out, v.digest)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(v.as_dict()), encoding="utf-8")
    code = main(["convert", "--root", str(root), "--manifest", str(manifest), "--entrypoint", "a.gpkg",
                 "--output-directory", str(tmp_path / "cli-bundle")])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "REJECTED"
