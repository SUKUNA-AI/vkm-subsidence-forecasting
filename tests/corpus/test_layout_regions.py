"""Regions only from a stored LAYOUT_RAW record (agent C, H-03): best label per query, thresholds, NMS, fine labels
(display vs inline formula), reading order, assignment of native boxes."""
from __future__ import annotations

from vkm_corpus.layout.regions import FINE_LABELS, assign, regions_from_raw


def det(i, q, label_id, score, box, rank, half=None):
    d = {"i": i, "query": q, "label_id": label_id, "label": FINE_LABELS[label_id], "score": score, "box_pt": box,
         "order_rank": rank}
    if half:
        d["half"] = half
    return d


RAW = {"detections": [
    det(0, 1, 22, 0.95, [50, 100, 500, 300], 2),        # text
    det(1, 1, 17, 0.40, [50, 100, 500, 300], 2),        # same query, weaker label → dropped
    det(2, 2, 17, 0.90, [50, 50, 500, 90], 1),          # paragraph_title
    det(3, 3, 5, 0.80, [200, 320, 400, 360], 3),        # display_formula
    det(4, 4, 15, 0.70, [60, 150, 90, 160], 2),         # inline_formula
    det(5, 5, 14, 0.45, [100, 400, 300, 600], 4),       # image below the 0.5 threshold → dropped
    det(6, 6, 21, 0.85, [50, 620, 500, 780], 5),        # table
    det(7, 7, 22, 0.60, [51, 101, 499, 299], 6),        # near-duplicate text → NMS
    det(8, 8, 16, 0.65, [280, 800, 300, 815], 7),       # number
]}


def test_regions_rule_v1():
    regs = regions_from_raw(RAW, 595, 842)
    labels = [r.label for r in regs]
    assert labels == ["paragraph_title", "text", "inline_formula", "display_formula", "table", "number"]
    assert [r.det_index for r in regs] == [2, 0, 4, 3, 6, 8]
    assert regs[1].block_type == "TEXT" and regs[0].block_type == "HEADING" and regs[-1].block_type == "PAGE_NUMBER"
    assert {r.family for r in regs} == {"text", "formula", "table"}


def test_thresholds_are_configurable():
    regs = regions_from_raw(RAW, 595, 842, thresholds={"image": 0.4})
    assert "image" in [r.label for r in regs]


def test_spread_halves_keep_order():
    raw = {"detections": [det(0, 1, 22, 0.9, [400, 10, 700, 100], 1, "R"), det(1, 1, 22, 0.9, [10, 10, 300, 100], 1,
                                                                                   "L")]}
    regs = regions_from_raw(raw, 800, 600)
    assert [r.half for r in regs] == ["L", "R"]


def test_assign_native_boxes():
    regs = regions_from_raw(RAW, 595, 842)
    got = assign([(60, 110, 490, 200), (10, 10, 20, 20)], regs, ("text",))
    assert got[0] is not None and regs[got[0]].label == "text" and got[1] is None
