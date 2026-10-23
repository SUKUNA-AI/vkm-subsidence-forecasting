"""SKRU-1 mining history in time: rules of ``vkm_world.mining.skru1_history`` and the public catalogue.

The catalogue part reads only the committed public projection ``evidence/world/skru1_mine/`` (no PRIVATE access).
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pytest

from vkm_world.governance.leakage import FORBIDDEN_COLUMNS
from vkm_world.mining.skru1_history import (YearInterval, areal_extraction_ratio, ascii_id, combine_intervals,
                                            decade_state, interval_from_row, normalize_objects, normalize_seams,
                                            overlapping_same_seam, plan_key)

ROOT = Path(__file__).resolve().parents[2]
PUB = ROOT / "evidence" / "world" / "skru1_mine"
csv.field_size_limit(sys.maxsize)


@pytest.mark.parametrize("text, keys", [
    ("блок 27", ["BLK:27"]),
    ("Бл.49-51", ["BLK:49", "BLK:51"]),
    ("1ЮЗП", ["PAN:1ЮЗП"]),
    ("4ЗП-6ЗП", ["PAN:4ЗП", "PAN:5ЗП", "PAN:6ЗП"]),
    ("1-5 юго-западные панели", [f"PAN:{n}ЮЗП" for n in range(1, 6)]),
    ("10 и 12 западные панели", ["PAN:10ЗП", "PAN:12ЗП"]),
    ("12 ЗП - 5 ЮЗП", ["PAN:12ЗП", "PAN:5ЮЗП"]),
    ("1 западный блок 1 СЗП", ["SUB:1СЗП:1W"]),
    ("1 СЗП / 6 западный блок", ["SUB:1СЗП:6W"]),
    ("1 СЗП, блок 6, зона 2", ["SUB:1СЗП:6?"]),
    ("18П блок, зона 208", ["BLK:18"]),
    ("северная часть 201 блока", ["BLK:201"]),
])
def test_object_keys(text, keys):
    assert normalize_objects(text)[0] == keys


def test_printed_block_range_is_not_filled_in():
    keys, flags = normalize_objects("бл.129-187")
    assert keys == ["BLK:129", "BLK:187"] and "RANGE_ENDS_ONLY" in flags


@pytest.mark.parametrize("text, seams", [
    ("Кр.II", ("КрII",)), ("Кр.2", ("КрII",)), ("АБ; КрII", ("КрII", "АБ")), ("В (карналлитовый)", ("Вк",)),
    ("В", ("В?",)), ("сильвинит (КрII, АБ, Вс без разбивки)", ("SYLV",)), ("не указан", ()),
])
def test_seams(text, seams):
    assert normalize_seams(text) == seams


def test_plan_keys_and_ascii_ids():
    assert plan_key("BLOCK", "27") == "BLK:27"
    assert plan_key("PANEL", "1 ЮЗП") == "PAN:1ЮЗП"
    assert plan_key("SUBBLOCK", "6 западный блок", "1 СЗП") == "SUB:1СЗП:6W"
    assert ascii_id("SKU-PAN-1ЮЗП-КрII") == "SKU-PAN-1YuZP-KR2"
    assert re.fullmatch(r"[A-Za-z0-9_.:/+-]+", ascii_id("SKU-SUB-1СЗП-6?-В?"))


def test_intervals_keep_open_bounds_open():
    iv = interval_from_row(1977, 1977, None, None)          # a start only: the end is not bounded from above
    assert iv.start_min == iv.start_max == 1977 and iv.end_max is None and iv.envelope() is None
    f = iv.filled(1930, 2026)
    assert (f.start_min, f.end_max) == (1977, 2026)
    by = interval_from_row(None, None, None, 2019)          # «не позднее 2019»: the end is an upper bound only
    assert by.end_min is None and by.start_max == 2019 and by.filled(1930, 2026).end_min == 1930
    with pytest.raises(ValueError):
        YearInterval(1990, 1985, None, None)


def test_combine_flags_disjoint_periods_and_never_averages():
    c = combine_intervals([("a", interval_from_row(1955, 1955, 1957, 1957)),
                           ("b", interval_from_row(1961, 1961, 1962, 1962))])
    assert c.conflict and c.core is None and c.envelope == (1955, 1962)


def test_decade_state():
    assert decade_state((1955, 1962), (1956, 1957), 1950) == "ACTIVE"
    assert decade_state((1955, 1962), None, 1960) == "POSSIBLE"
    assert decade_state((1955, 1962), (1956, 1957), 1970) == "NO"
    assert decade_state(None, None, 1990) == "UNKNOWN"


def test_extraction_ratio_and_overlap():
    assert areal_extraction_ratio(5.5, 8.0) == pytest.approx(0.4074, abs=1e-4)
    from shapely.geometry import box
    units = [{"id": "a", "seam": "КрII", "geom": box(0, 0, 100, 100), "core": (1960, 1965)},
             {"id": "b", "seam": "КрII", "geom": box(50, 0, 150, 100), "core": (1964, 1970)},
             {"id": "c", "seam": "АБ", "geom": box(0, 0, 100, 100), "core": (1960, 1965)}]
    issues = overlapping_same_seam(units)
    assert [(i["unit_a"], i["unit_b"]) for i in issues] == [("a", "b")]


# ----------------------------------------------------------------------------- public catalogue


def _rows(name: str) -> list[dict[str, str]]:
    path = PUB / name
    if not path.is_file():
        pytest.skip(f"public catalogue {name} is not published in this checkout")
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_public_catalogue_has_no_quotes_or_geometry():
    for p in sorted(PUB.glob("*.csv")) if PUB.is_dir() else []:
        with p.open(encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh))
        assert not {h.strip().lower() for h in header} & FORBIDDEN_COLUMNS, p.name
        assert not any(h.lower().startswith(("wkt", "centroid")) for h in header), p.name


def test_units_intervals_and_vocabularies():
    rows = _rows("skru1_mining_units.csv")
    assert rows
    classes = {"TEXT_DATED", "TEXT_PARTLY_DATED", "TEXT_OPEN_ENDED", "TEXT_UNDATED", "DERIVED_DATES",
               "PLAN_SNAPSHOT_ONLY", "PLANNED_ONLY", "UNKNOWN"}
    for r in rows:
        assert r["timing_class"] in classes, r["unit_id"]
        b = [r[c] for c in ("mining_start_min", "mining_start_max", "mining_end_min", "mining_end_max")]
        if all(b):
            s0, s1, e0, e1 = map(int, b)
            assert s0 <= s1 and e0 <= e1 and s0 <= e1, r["unit_id"]
        if r["timing_class"] in ("UNKNOWN", "DERIVED_DATES", "PLAN_SNAPSHOT_ONLY", "PLANNED_ONLY"):
            assert not r["mining_core"], r["unit_id"]   # a certain period comes only from printed (FACT) dates
        for p in filter(None, r["mining_core"].split(";")):
            a, b_ = (int(x) for x in p.split("–"))
            assert a <= b_
        for s in filter(None, r["source_ids"].split(";")):
            assert re.fullmatch(r"(VKM|EXT)-SRC-\d{3}", s), (r["unit_id"], s)


def test_planned_rows_do_not_date_actual_mining():
    units = _rows("skru1_mining_units.csv")
    assert all(u["design_or_actual"] != "ACTUAL" for u in units if u["timing_class"] == "PLANNED_ONLY")


def test_evidence_rows_are_attributed_and_located():
    rows = _rows("skru1_mine_evidence.csv")
    ids = [r["evidence_id"] for r in rows]
    assert len(ids) == len(set(ids))
    for r in rows:
        assert re.fullmatch(r"(VKM|EXT)-SRC-\d{3}", r["source_id"]), r["evidence_id"]
        assert r["page_id"] and r["site_attribution"], r["evidence_id"]
        if r["status"] == "UNKNOWN":
            assert not r["value_min"] and not r["value_max"], r["evidence_id"]
