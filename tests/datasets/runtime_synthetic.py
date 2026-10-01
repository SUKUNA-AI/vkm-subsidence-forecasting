"""Standalone GDAL/xlrd acceptance; run only in an existing vendor runtime, with an explicit scratch root."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import json
import unittest
from osgeo import ogr, osr

from vkm_datasets import DatasetVersion, Policy, discover
from vkm_datasets.gis import convert_to_gpkg, inspect_vector, verify_conversion_bundle
from vkm_datasets.workbooks import inspect_workbook

ROOT = Path(sys.argv.pop(1))
ROOT.mkdir(parents=True, exist_ok=False)


def manifest(root, name):
    return DatasetVersion("SYNTHETIC", discover(root, (name,)), (name,),
                          Policy("PRIVATE_CLOUD_ALLOWED", "INPUT", "synthetic-rehearsal"),
                          "synthetic", "synthetic", "2026-10-01T00:00:00Z")


def make_tab(root, duplicate=False, invalid=False):
    root.mkdir()
    driver = ogr.GetDriverByName("MapInfo File")
    ds = driver.CreateDataSource(str(root / "native.tab"))
    srs = osr.SpatialReference()
    srs.SetLocalCS("SYNTHETIC NonEarth")
    srs.SetLinearUnits("metre", 1.0)
    layer = ds.CreateLayer("native", srs=srs, geom_type=ogr.wkbPolygon,
                           options=["ENCODING=CP1251", "STRICT_FIELDS_NAME_LAUNDERING=NO"])
    for name, typ in (("Номер_зоны", ogr.OFTInteger), ("Участок", ogr.OFTInteger),
                      ("Название", ogr.OFTString), ("Дата", ogr.OFTDate)):
        f = ogr.FieldDefn(name, typ)
        if typ == ogr.OFTString:
            f.SetWidth(254)
        assert layer.CreateField(f) == 0
    for i in range(2):
        f = ogr.Feature(layer.GetLayerDefn())
        f.SetField("Номер_зоны", 1)
        f.SetField("Участок", 10 if duplicate else i + 10)
        f.SetField("Название", "Кириллица " + "д" * 170)
        f.SetField("Дата", "2026/10/01")
        wkt = "POLYGON ((0 0,10 0,10 10,0 10,0 0))"
        if invalid:
            wkt = "POLYGON ((0 0,10 0,10 10,0 10,0 0),(20 20,21 20,21 21,20 21,20 20))"
        f.SetGeometry(ogr.CreateGeometryFromWkt(wkt))
        f.SetStyleString('PEN(w:2px,c:#ff0000)')
        assert layer.CreateFeature(f) == 0
    ds = None
    return manifest(root, "native.tab")


class NativeAcceptance(unittest.TestCase):
    def test_sparse_fid_roundtrip(self):
        root = ROOT / "fid"; root.mkdir()
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(root / "source.gpkg"))
        layer = ds.CreateLayer("features", geom_type=ogr.wkbPoint, options=["FID=native_feature_id"])
        layer.CreateField(ogr.FieldDefn("zone", ogr.OFTInteger))
        for fid in (5, 500):
            feature = ogr.Feature(layer.GetLayerDefn())
            feature.SetFID(fid)
            feature.SetField("zone", fid)
            feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({fid} 10)"))
            self.assertEqual(layer.CreateFeature(feature), 0)
        feature = layer = ds = None
        v = manifest(root, "source.gpkg")
        out = ROOT / "fid-converted"
        receipt = convert_to_gpkg(root, v, "source.gpkg", out, keys={"features": ("zone",)})
        self.assertEqual(receipt["status"], "PASS_CONVERSION", receipt["errors"])
        source, target = receipt["source_layers"][0], receipt["target_layers"][0]
        self.assertEqual(source["fid_sha256"], target["fid_sha256"])
        self.assertEqual(source["fid_column"], "native_feature_id")
        output = ogr.Open(str(out / "data.gpkg"))
        self.assertEqual([f.GetFID() for f in output.GetLayerByName("features")], [5, 500])
        output = None
        self.assertEqual(convert_to_gpkg(root, v, "source.gpkg", out, keys={"features": ("zone",)}), receipt)

    def test_nonearth_tab_roundtrip_and_styles(self):
        root = ROOT / "tab"
        v = make_tab(root)
        before = inspect_vector(root, v, "native.tab", keys={"native": ("Номер_зоны", "Участок")})
        self.assertEqual(before["layers"][0]["crs"]["status"], "LOCAL")
        self.assertEqual(len(v.files), 4)
        out = ROOT / "tab-converted"
        receipt = convert_to_gpkg(root, v, "native.tab", out, keys={"native": ("Номер_зоны", "Участок")})
        self.assertEqual(receipt["status"], "PASS_CONVERSION", receipt["errors"])
        metadata = json.loads((out / "native_metadata.json").read_text(encoding="utf-8"))
        self.assertTrue(metadata["styles"])
        self.assertTrue((out / "data.gpkg").is_file())
        self.assertFalse((out / "candidate.gpkg").exists())
        self.assertEqual(receipt["target_layers"][0]["crs"]["status"], "LOCAL")
        self.assertEqual(verify_conversion_bundle(out, v.digest)["status"], "PASS")
        self.assertEqual(convert_to_gpkg(root, v, "native.tab", out,
                                        keys={"native": ("Номер_зоны", "Участок")}), receipt)
        with self.assertRaisesRegex(ValueError, "different input, context, rules or config"):
            convert_to_gpkg(root, v, "native.tab", out)
        (out / "native_metadata.json").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_conversion_bundle(out, v.digest)

    def test_mif_direct_conversion_and_missing_declared_companion(self):
        from dataclasses import replace
        root = ROOT / "mif"; root.mkdir()
        (root / "native.mif").write_text('Version 450\nCharset "WindowsCyrillic"\nDelimiter ","\n'
             'CoordSys NonEarth Units "m" Bounds (0, 0) (10000, 10000)\nColumns 2\n'
             '  Номер Integer\n  Имя Char(40)\nData\nPoint 10 20\nSymbol (35,16711680,12)\n', encoding="cp1251")
        (root / "native.mid").write_text('1,"Кириллица"\n', encoding="cp1251")
        v = manifest(root, "native.mif")
        incomplete = replace(v, files=tuple(f for f in v.files if f.path.endswith(".mif")))
        with self.assertRaisesRegex(ValueError, "companion"):
            inspect_vector(root, incomplete, "native.mif")
        receipt = convert_to_gpkg(root, v, "native.mif", ROOT / "mif-converted")
        self.assertEqual(receipt["status"], "PASS_CONVERSION", receipt["errors"])
        self.assertEqual(receipt["target_layers"][0]["fields"][1]["name"], "Имя")

    def test_failed_comparison_never_publishes_data(self):
        from unittest.mock import patch
        root = ROOT / "failed-compare"
        v = make_tab(root)
        out = ROOT / "failed-compare-bundle"
        with patch("vkm_datasets.gis._compare", return_value=(["INJECTED_MISMATCH"], [])):
            receipt = convert_to_gpkg(root, v, "native.tab", out)
        self.assertEqual(receipt["status"], "REJECTED")
        self.assertTrue((out / "candidate.gpkg").is_file())
        self.assertFalse((out / "data.gpkg").exists())
        with self.assertRaises(ValueError):
            verify_conversion_bundle(out, v.digest)

    def test_composite_duplicate_rejected(self):
        root = ROOT / "duplicates"
        v = make_tab(root, duplicate=True)
        out = ROOT / "duplicate-rejected"
        receipt = convert_to_gpkg(root, v, "native.tab", out, keys={"native": ("Номер_зоны", "Участок")})
        self.assertEqual(receipt["status"], "REJECTED")
        self.assertIn("NON_UNIQUE", str(receipt["errors"]))
        self.assertFalse((out / "data.gpkg").exists())

    def test_257_fields_unknown_crs_stays_unknown(self):
        root = ROOT / "unknown"; root.mkdir()
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(root / "source.gpkg"))
        layer = ds.CreateLayer("широкая", geom_type=ogr.wkbPoint)
        for i in range(257):
            self.assertEqual(layer.CreateField(ogr.FieldDefn("Поле_" + str(i), ogr.OFTString)), 0)
        f = ogr.Feature(layer.GetLayerDefn())
        for i in range(257):
            f.SetField(i, "Синтетика " + str(i))
        f.SetGeometry(ogr.CreateGeometryFromWkt("POINT (10000 25000)"))
        self.assertEqual(layer.CreateFeature(f), 0)
        layer = f = ds = None
        v = manifest(root, "source.gpkg")
        receipt = convert_to_gpkg(root, v, "source.gpkg", ROOT / "unknown-converted")
        self.assertEqual(receipt["status"], "PASS_CONVERSION", receipt["errors"])
        self.assertEqual(receipt["target_layers"][0]["crs"]["status"], "UNKNOWN")
        self.assertEqual(len(receipt["target_layers"][0]["fields"]), 257)

    def test_invalid_polygon_is_not_repaired(self):
        root = ROOT / "invalid"; root.mkdir()
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(root / "source.gpkg"))
        layer = ds.CreateLayer("invalid", geom_type=ogr.wkbPolygon)
        f = ogr.Feature(layer.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON ((0 0,10 0,10 10,0 10,0 0),(20 20,21 20,21 21,20 21,20 20))"))
        self.assertEqual(layer.CreateFeature(f), 0)
        layer = f = ds = None
        v = manifest(root, "source.gpkg")
        out = ROOT / "invalid-rejected"
        receipt = convert_to_gpkg(root, v, "source.gpkg", out)
        self.assertEqual(receipt["status"], "REJECTED")
        self.assertIn("INVALID_SOURCE_GEOMETRY", str(receipt["errors"]))
        self.assertFalse((out / "data.gpkg").exists())

    def test_xls_native_formulas_and_hidden_sheet(self):
        import xlwt
        root = ROOT / "xls"; root.mkdir()
        book = xlwt.Workbook()
        sheet = book.add_sheet("Кириллица")
        sheet.write(0, 0, "Зона")
        sheet.write(1, 0, "12,50")
        sheet.write(1, 1, xlwt.Formula("1+2"))
        sheet.write_merge(2, 2, 0, 1, "0001...")
        hidden = book.add_sheet("target"); hidden.visibility = 2
        hidden.write(0, 0, "SYNTHETIC_SEALED_CANARY")
        book.save(str(root / "source.xls"))
        v = manifest(root, "source.xls")
        report = inspect_workbook(root, v, "source.xls", include_values=True)
        self.assertEqual(report["sheets"][1]["state"], "veryHidden")
        self.assertEqual(report["formula_representation"], "BIFF_RPN_UNINTERPRETED")
        self.assertTrue(report["native_formula_records"][0]["native_biff_payload_hex"])
        self.assertNotIn("SYNTHETIC_SEALED_CANARY", json.dumps(inspect_workbook(root, v, "source.xls")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
