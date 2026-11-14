import copy
from decimal import Decimal
import json
from pathlib import Path
import statistics
import unittest

import duckdb
from vkm_corpus.figures.scoped_correspondence import (apply_overlay, compare_scoped, coverage_errors, identity_guard,
                                   sha256_json, split_trace, validate_overlay)
from vkm_corpus.api.scientific_snapshot_verification import encoded, normalized, full_checksum, historical_receipt_audit


def points(values):
    return [{"index": i, "x": float(x), "y": float(y)} for i, (x, y) in enumerate(values)]


class GeometryScopeTests(unittest.TestCase):
    def test_tolerance_must_be_finite_positive_number(self):
        can, nav = points([(0,0),(1,0),(2,0)]), points([(0,100),(1,100),(2,100)])
        for value in (float("inf"), float("-inf"), float("nan"), 0, -1, True, False, None, "0.75", complex(1,0)):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                compare_scoped(can, nav, [0,2], tolerance=value)

    def test_finite_positive_tolerance_bounds_support(self):
        can = points([(0,0),(1,0),(2,0)])
        self.assertTrue(compare_scoped(can, can, [0,2], tolerance=0.001)["scoped_geometry_support"])
        self.assertFalse(compare_scoped(can, points([(0,0.002),(1,0.002),(2,0.002)]), [0,2], tolerance=0.001)["scoped_geometry_support"])

    def test_window_bounds_must_be_finite_numeric(self):
        p = points([(0,0),(1,0)])
        for window in ([0,float("inf")], [float("nan"),1], [False,1], [0,"1"]):
            with self.subTest(window=repr(window)), self.assertRaises(ValueError):
                compare_scoped(p,p,window)

    def test_interpolation_gap_must_be_finite_positive_number(self):
        p = points([(0,0),(1,0)])
        for value in (float("inf"),float("-inf"),float("nan"),0,-1,None,False,True,"4"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                split_trace(p,value)

    def test_gap_boundary_remains_unbridged_only_above_limit(self):
        self.assertEqual(split_trace(points([(0,0),(4,0)]),4)[1],[])
        self.assertEqual(split_trace(points([(0,0),(4.001,0)]),4)[1][0]["reason"],"UNBRIDGED_GAP")

    def test_one_big_outlier_is_not_hidden_by_zero_median(self):
        can = points([(i, 0) for i in range(9)])
        nav = points([(i, 100 if i == 4 else 0) for i in range(9)])
        self.assertEqual(statistics.median([abs(a["y"]-b["y"]) for a,b in zip(can,nav)]), 0)
        result = compare_scoped(can, nav, [0, 8])
        self.assertEqual([r["index"] for r in result["canonical_to_nav"]["rejected"]], [4])
        self.assertNotIn(4, result["scoped_nav_point_indices"])
        self.assertFalse(result["whole_trace_identity"])

    def test_perfect_overlap_with_foreign_tail_stays_partial(self):
        can = points([(0, 0), (1, 1), (2, 2)])
        nav = points([(-1, 99), (0, 0), (1, 1), (2, 2), (3, 99)])
        result = compare_scoped(can, nav, [0, 2])
        self.assertEqual(result["scoped_nav_point_indices"], [1,2,3])
        self.assertEqual(result["nav_to_canonical"]["outside_scope_indices"], [0,4])
        self.assertFalse(result["complete_series_or_figure"])

    def test_gap_is_not_interpolated(self):
        result = compare_scoped(points([(2,0),(3,0),(9,0)]), points([(0,0),(1,0),(10,0),(11,0)]), [0,11])
        self.assertEqual(result["scoped_canonical_point_indices"], [])
        self.assertEqual(result["nav_trace_breaks"][0]["reason"], "UNBRIDGED_GAP")

    def test_duplicate_point_index_rejected(self):
        p = points([(0,0),(1,1)])
        p[1]["index"] = 0
        with self.assertRaises(ValueError):
            split_trace(p, 4)

    def test_nan_coordinate_rejected(self):
        with self.assertRaises(ValueError):
            split_trace(points([(0,0),(1,float("nan"))]), 4)

    def test_reverse_x_is_explicit_break(self):
        _, breaks = split_trace(points([(1,0),(2,0),(1,1)]), 4)
        self.assertEqual(breaks[0]["reason"], "X_REVERSES_OR_DUPLICATES")

    def test_different_epoch_despite_same_shape(self):
        a = dict(source_id="s",page_id="p",figure="2.7",panel="a",frame_id="f",source_sha256="h",epoch="2010",unit="mm")
        self.assertEqual(identity_guard(a, dict(a,epoch="2011")), ["DIFFERENT_EPOCH"])

    def test_different_unit_despite_same_shape(self):
        a = dict(source_id="s",page_id="p",figure="2.7",panel="a",frame_id="f",source_sha256="h",epoch="2010",unit="mm")
        self.assertEqual(identity_guard(a, dict(a,unit="m")), ["DIFFERENT_UNIT"])

    def test_same_page_different_figure_is_not_identity(self):
        a = dict(source_id="s",page_id="p",figure="2.7",panel="a",frame_id="f",source_sha256="h",epoch="2010",unit="mm")
        self.assertEqual(identity_guard(a, dict(a,figure="2.8")), ["DIFFERENT_FIGURE"])

    def test_missing_nav_metadata_stays_unverified(self):
        a = dict(source_id="s",page_id="p",figure="2.7",panel="a",frame_id="f",source_sha256="h",epoch="2010",unit="mm")
        self.assertEqual(identity_guard(a, dict(a,epoch=None,unit=None)), ["UNVERIFIED_EPOCH","UNVERIFIED_UNIT"])


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.base = [{"canonical_series_id":"s1", "link_status":"historical", "value": 12}]
        self.rec = {"correction_id":"c1","canonical_series_id":"s1","old_row_sha256":sha256_json(self.base[0]), "new_scope_status":"SCOPED_ONLY",
                    "whole_trace_identity":False,"scientific_admission":"NOT_GRANTED","basis":"scoped","precedence":10}

    def test_overlay_preserves_historical_bytes_and_is_repeatable(self):
        old = copy.deepcopy(self.base)
        one, two = apply_overlay(self.base,[self.rec]), apply_overlay(self.base,[self.rec])
        self.assertEqual(one,two)
        self.assertEqual(self.base,old)
        self.assertEqual(one[0]["historical_link_status"],"historical")
        self.assertEqual(one[0]["link_status"],"SCOPED_ONLY")
        self.assertFalse(one[0]["whole_trace_identity_permitted"])

    def test_stale_old_row_rejected(self):
        with self.assertRaises(ValueError):
            apply_overlay([dict(self.base[0], value=13)],[self.rec])

    def test_duplicate_correction_id_rejected(self):
        with self.assertRaises(ValueError):
            validate_overlay([self.rec,self.rec])

    def test_false_whole_trace_upgrade_rejected(self):
        with self.assertRaises(ValueError):
            validate_overlay([dict(self.rec,whole_trace_identity=True)])

    def test_admission_upgrade_rejected(self):
        with self.assertRaises(ValueError):
            validate_overlay([dict(self.rec,scientific_admission="FACT")])


class SnapshotTests(unittest.TestCase):
    def test_unknown_links_full_read_is_not_proof_even_if_not_truncated(self):
        for value in (None,False,0,1,"true"):
            with self.subTest(value=value):
                receipt=dict(self.complete_receipt(),links_full_read=value)
                self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])
        for truncation in ("absent",False):
            receipt=self.complete_receipt()
            del receipt["links_full_read"]
            if truncation is False:
                receipt["links_truncated"]=False
            self.assertIn("INCOMPLETE_LINKS_FULL_READ",historical_receipt_audit(receipt)["issues"])

    def test_positive_links_full_read_and_contradictory_truncation(self):
        receipt=self.complete_receipt()
        self.assertTrue(historical_receipt_audit(receipt)["current_full_proof"])
        self.assertTrue(historical_receipt_audit(dict(receipt,links_truncated=False))["current_full_proof"])
        for value in (True,None,0,"false"):
            with self.subTest(value=value):
                self.assertFalse(historical_receipt_audit(dict(receipt,links_truncated=value))["current_full_proof"])

    def test_decimal_tag_retains38digits_and_scale_without_float(self):
        for text in ("12345678901234567890.123456789012345678","12345678901234567890123456789012345678","1.2300","-0.0000"):
            value=Decimal(text)
            tagged=normalized(value)
            self.assertEqual(tagged,{"$decimal":text})
            self.assertEqual(Decimal(tagged["$decimal"]).as_tuple(),value.as_tuple())
            self.assertEqual(json.loads(encoded(value)),tagged)

    def test_real_duckdb_decimal38_both_scales_and_captured_type(self):
        with duckdb.connect(":memory:") as con:
            for text,scale in (("12345678901234567890.123456789012345678",18),("12345678901234567890123456789012345678",0)):
                with self.subTest(scale=scale):
                    result=full_checksum(con,f"SELECT '{text}'::DECIMAL(38,{scale}) AS d",capture=True)
                    self.assertIsInstance(result["captured_rows"][0]["d"],Decimal)
                    self.assertEqual(str(result["captured_rows"][0]["d"]),text)
                    self.assertEqual(json.loads(encoded(result))["captured_rows"][0]["d"],{"$decimal":text})

    def test_decimal_row_after500_and4096_changes_full_multiset(self):
        old="12345678901234567890.123456789012345678"
        new="12345678901234567890.123456789012345679"
        with duckdb.connect(":memory:") as con:
            for rows in (600,4100):
                with self.subTest(rows=rows):
                    before=full_checksum(con,f"SELECT i,'{old}'::DECIMAL(38,18) d FROM range({rows}) t(i)")
                    after=full_checksum(con,f"SELECT i,CASE WHEN i={rows-1} THEN '{new}' ELSE '{old}' END::DECIMAL(38,18) d FROM range({rows}) t(i)")
                    self.assertEqual(before["rows"],rows)
                    self.assertNotEqual(before["multiset_sha256"],after["multiset_sha256"])

    def test_decimal_scale_and_multiplicity_remain_part_of_digest(self):
        with duckdb.connect(":memory:") as con:
            two=full_checksum(con,"SELECT '1.23'::DECIMAL(38,2) AS d")
            four=full_checksum(con,"SELECT '1.2300'::DECIMAL(38,4) AS d")
            duplicate=full_checksum(con,"SELECT '1.23'::DECIMAL(38,2) AS d FROM range(2)")
        self.assertNotEqual(two["multiset_sha256"],four["multiset_sha256"])
        self.assertNotEqual(two["multiset_sha256"],duplicate["multiset_sha256"])

    @staticmethod
    def complete_receipt():
        # Shape of the actual recorded full proof; no private values or data.
        return {"differing":[], "parent_queries":873, "parent_queries_executed":873,
                "parent_queries_full_read":873, "partition_rows":10421, "partition_unique_points":10421,
                "partition_content_digest_matches_batch":True, "links_content_digest_matches_batch":True,
                "links_full_read":True, "parent_query_truncated_preview_ids":["preview-only"]}

    def test_explicit_complete_counts_pass_without_mutating_receipt(self):
        receipt = self.complete_receipt()
        before = copy.deepcopy(receipt)
        self.assertTrue(historical_receipt_audit(receipt)["current_full_proof"])
        self.assertEqual(receipt,before)
        receipt.update(parent_query_full_read_count=873, parent_query_truncated_count=0,
                       links_truncated=False, series_summary_truncated=False)
        self.assertEqual(historical_receipt_audit(receipt)["exit_code"],0)

    def test_full_read_aliases_are_counts_not_flags_or_presence(self):
        for field in ("parent_query_full_read_count", "parent_queries_full_read"):
            for value in (None,-1,False,True,0,872,874,873.0,"873"):
                receipt = self.complete_receipt()
                receipt[field] = value
                with self.subTest(field=field,value=value):
                    self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])

    def test_inconsistent_full_read_aliases_cannot_pass(self):
        receipt = dict(self.complete_receipt(),parent_query_full_read_count=872)
        self.assertEqual(historical_receipt_audit(receipt)["exit_code"],1)

    def test_truncated_read_count_is_explicit_integer_zero(self):
        for value in (1,None,-1,False,True,0.0,"0"):
            with self.subTest(value=value):
                self.assertFalse(historical_receipt_audit(dict(self.complete_receipt(),parent_query_truncated_count=value))["current_full_proof"])

    def test_zero_truncated_count_does_not_establish_full_read(self):
        receipt = self.complete_receipt()
        del receipt["parent_queries_full_read"]
        receipt["parent_query_truncated_count"] = 0
        self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])

    def test_required_counts_are_exact_nonnegative_integers(self):
        for field in ("parent_queries", "parent_queries_executed", "partition_rows", "partition_unique_points"):
            for value in (None,-1,False,True,1.0,"1"):
                receipt = self.complete_receipt()
                receipt[field] = value
                with self.subTest(field=field,value=value):
                    self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])
            receipt = self.complete_receipt()
            del receipt[field]
            with self.subTest(missing=field):
                self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])

    def test_required_digests_missing_or_truthy_are_not_proof(self):
        for field in ("partition_content_digest_matches_batch", "links_content_digest_matches_batch"):
            for value in (None,False,1,"true"):
                with self.subTest(field=field,value=value):
                    self.assertFalse(historical_receipt_audit(dict(self.complete_receipt(),**{field:value}))["current_full_proof"])
            receipt = self.complete_receipt()
            del receipt[field]
            self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])

    def test_all_missing_fields_and_zero_query_count_cannot_pass(self):
        self.assertFalse(historical_receipt_audit({})["current_full_proof"])
        receipt = dict(self.complete_receipt(),parent_queries=0,parent_queries_executed=0,parent_queries_full_read=0)
        self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])

    def test_differing_requires_explicit_empty_list(self):
        for value in (None,False,"",{},["changed-query"]):
            with self.subTest(value=repr(value)):
                self.assertFalse(historical_receipt_audit(dict(self.complete_receipt(),differing=value))["current_full_proof"])
        receipt = self.complete_receipt()
        del receipt["differing"]
        self.assertFalse(historical_receipt_audit(receipt)["current_full_proof"])

    def test_explicit_bad_child_read_flags_cannot_pass(self):
        for field,value in (("links_full_read",False),("links_truncated",True),("links_truncated",None),
                            ("series_summary_truncated",True),("series_summary_truncated",0)):
            with self.subTest(field=field,value=value):
                self.assertFalse(historical_receipt_audit(dict(self.complete_receipt(),**{field:value}))["current_full_proof"])

    def test_row_after_500_changes_full_checksum(self):
        with duckdb.connect() as con:
            a = full_checksum(con,"SELECT range as id, 0 as v FROM range(600)")
            b = full_checksum(con,"SELECT range as id, case when range=599 then 1 else 0 end as v FROM range(600)")
        self.assertEqual(a["rows"],600)
        self.assertTrue(a["preview_500_would_truncate"])
        self.assertNotEqual(a["multiset_sha256"],b["multiset_sha256"])

    def test_multiplicity_affects_checksum(self):
        with duckdb.connect() as con:
            a = full_checksum(con,"SELECT 1 as id FROM range(2)")
            b = full_checksum(con,"SELECT 1 as id FROM range(3)")
        self.assertNotEqual(a["multiset_sha256"],b["multiset_sha256"])

    def test_full_multiset_not_row_order(self):
        with duckdb.connect() as con:
            a = full_checksum(con,"SELECT range as id FROM range(3) ORDER BY id")
            b = full_checksum(con,"SELECT range as id FROM range(3) ORDER BY id desc")
        self.assertEqual(a,b)

    def test_duplicate_and_missing_partition_points_detected(self):
        e = coverage_errors([{"id":"a"},{"id":"a"}], {"a","b"}, key="id")
        self.assertEqual(e["duplicates"],["a"])
        self.assertEqual(e["missing"],["b"])

    def test_differing_query_old_verifier_cannot_pass(self):
        self.assertEqual(historical_receipt_audit({"differing":["q"], "parent_queries":1,"parent_queries_executed":1})["exit_code"],1)

    def test_wrong_digest_cannot_pass(self):
        r = {"differing":[], "parent_queries":1,"parent_queries_executed":1,
             "parent_query_full_read_count":1,"partition_content_digest_matches_batch":False}
        self.assertEqual(historical_receipt_audit(r)["exit_code"],1)

    def test_unknown_old_parent_truncation_cannot_pass(self):
        r = {"differing":[], "parent_queries":873,"parent_queries_executed":873,
             "partition_content_digest_matches_batch":True,"links_content_digest_matches_batch":True}
        self.assertIn("HISTORICAL_PARENT_QUERY_TRUNCATION_NOT_RECORDED",historical_receipt_audit(r)["issues"])

    def test_incomplete_parent_query_coverage_cannot_pass(self):
        r = {"differing":[], "parent_queries":2,"parent_queries_executed":1,"parent_query_full_read_count":1}
        self.assertIn("INCOMPLETE_PARENT_QUERY_COVERAGE",historical_receipt_audit(r)["issues"])

    def test_partition_duplicates_cannot_pass(self):
        r = {"differing":[], "parent_queries":1,"parent_queries_executed":1,"parent_query_full_read_count":1,
             "partition_rows":2,"partition_unique_points":1}
        self.assertIn("PARTITION_DUPLICATES",historical_receipt_audit(r)["issues"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
