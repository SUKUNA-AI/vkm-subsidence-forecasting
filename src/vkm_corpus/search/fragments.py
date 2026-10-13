"""Fragments of one figure (agent Д, 13.10): the layout model often splits one printed figure into several regions —
sub-panels «а», «б», legend swatches, labels — and the caption association gives every region the same caption block.
In a FIGURE ranking such regions tie (one caption, one embedding-unit text: label + caption + citing context) and fill
consecutive places. Snapshot snap-20261007T103222Z-2d71e9e8 (read-only count over the API): 1 897 groups «page +
caption block» of 2–11 regions hold 4 396 of 18 297 figures; in a sample of 15 figure queries 49 of 196 top-20 places
of ``search_text`` and 67 of 300 of ``search_hybrid`` (late) were taken by a second, third … region of one group.

Rule (on by default for the FIGURE kind of ``search_text`` and ``search_hybrid`` in the API; ``group_fragments=false``
lists every detected region separately; the library default is off, so other callers keep today's lists):

* FIGURE hits of one page with the same ``caption_block_id`` are one position — the place of the group's best-ranked
  member; the other members are listed in ``fragments`` (ids). A region without a caption block is never grouped:
  nothing says that two uncaptioned regions are one figure (such regions have no caption text, and in the sample they
  took 1 of 300 places).
* Representative (MODEL_CHOICE): the member with the largest region — not the best-ranked one: the members share the
  caption and the unit text, so their scores tie and the best rank is the id tie-break (in the live example
  «план шахтного поля рис. 3.1» a 30 × 14 pt legend swatch). Preference: a non-tiny region with a page bbox (largest
  area first), then a region of unknown size (no bbox; its size is UNKNOWN, not small), then a tiny region (largest
  first); ties → the better rank. So a tiny region never takes a place of its own and never stands for a group in which
  a larger region of the same caption is ranked.
* Tiny (MODEL_CHOICE, :data:`TINY_AREA_PT2`, :data:`TINY_SIDE_PT`): area < 1500 pt² or shorter side < 20 pt
  (``PAGE_PT_TL`` bboxes; valleys of the snapshot's size histograms: area 1000–1500 pt² — 73 regions against 184 at
  600–1000; shorter side 15–20 pt — 162 against 72 at 20–25); 345 of the 17 852 regions with a page bbox, 64 of them in
  groups (25 groups, 8 of them tiny only — the live example among them).
* Scope: the grouping runs over the ranked list the search holds — ``search_text``: its FIGURE hits from the top, at
  least :data:`FRAGMENT_DEPTH` deep (deeper while the grouped list is shorter than the asked window); hybrid: the whole
  final order (after G1, before the per-source cap). The window is cut from the grouped list, so offsets page through
  one list; members beyond the list are not looked up. Totals still count index objects.

A grouping is a presentation of search candidates (derived, AUTO_EXTRACTED_UNREVIEWED caption association), never an
assertion that the regions are one canonical object; the canon keeps every region.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

TINY_AREA_PT2 = 1500.0          # MODEL_CHOICE: a region below this area (pt²) is tiny
TINY_SIDE_PT = 20.0             # MODEL_CHOICE: … or with its shorter side below this (pt)
FRAGMENT_DEPTH = 200            # search_text: FIGURE hits grouped from the top (E's single-kind size limit)
PAGE_SPACE = "PAGE_PT_TL"
REGION_FIELDS = ("id", "page_id", "caption_block_id", "bbox")
RULE = (f"FIGURE hits of one page sharing a caption block are one position (the best-ranked member's place); "
        f"representative: the largest region that is not tiny (tiny: area < {TINY_AREA_PT2:g} pt² or shorter side < "
        f"{TINY_SIDE_PT:g} pt; unknown size before tiny); the others in fragments")


@dataclass(frozen=True)
class Region:
    """What the grouping needs of a figure: its page, its caption block and its page bbox (pt, top-left)."""
    page_id: str | None
    caption_block_id: str | None
    bbox: tuple[float, float, float, float] | None = None

    @classmethod
    def of(cls, page_id: str | None, fields: Mapping[str, Any] | None) -> "Region":
        """From index fields (``caption_block_id``, ``bbox`` = {x0, y0, x1, y1, space}) or canonical columns
        (``bbox_x0`` … ``bbox_space``); a bbox in another space than ``PAGE_PT_TL`` counts as unknown size."""
        fields = fields or {}
        raw = fields.get("bbox")
        box = None
        if isinstance(raw, Mapping):
            values, space = [raw.get(k) for k in ("x0", "y0", "x1", "y1")], raw.get("space")
        else:
            values, space = [fields.get(f"bbox_{k}") for k in ("x0", "y0", "x1", "y1")], fields.get("bbox_space")
        if all(v is not None for v in values) and space in (None, PAGE_SPACE):
            box = (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
        return cls(page_id or fields.get("page_id"), fields.get("caption_block_id") or None, box)

    @property
    def area(self) -> float | None:
        if self.bbox is None:
            return None
        return max(0.0, self.bbox[2] - self.bbox[0]) * max(0.0, self.bbox[3] - self.bbox[1])

    @property
    def tiny(self) -> bool:
        """Area < :data:`TINY_AREA_PT2` or shorter side < :data:`TINY_SIDE_PT`; unknown size is not tiny."""
        if self.bbox is None:
            return False
        side = min(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1])
        return (self.area or 0.0) < TINY_AREA_PT2 or side < TINY_SIDE_PT


def _preference(region: Region, pos: int) -> tuple[int, float, int]:
    """Sort key of a representative (smallest first): non-tiny with a bbox, unknown size, tiny; larger area; rank."""
    tier = 2 if region.tiny else (1 if region.bbox is None else 0)
    return tier, -(region.area or 0.0), pos


def group(keys: list[str], regions: Mapping[str, Region]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """``keys`` in ranked order (any kinds: only keys with a region that has a page and a caption block are grouped)
    → (the order with one key per group at the place of its best-ranked member, representative → {caption_block_id,
    fragments, members, rank_before, position_from, tiny}). Groups of one member are unchanged and not reported."""
    members: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for pos, key in enumerate(keys):
        r = regions.get(key)
        if r is not None and r.page_id and r.caption_block_id:
            members.setdefault((r.page_id, r.caption_block_id), []).append((pos, key))
    rep_of: dict[str, str] = {}
    info: dict[str, dict[str, Any]] = {}
    for (_page, block), ms in members.items():
        if len(ms) < 2:
            continue
        rep = min(ms, key=lambda m: _preference(regions[m[1]], m[0]))[1]
        info[rep] = {"caption_block_id": block, "fragments": [k for _p, k in ms if k != rep], "members": len(ms),
                     "rank_before": ms[0][0] + 1, "position_from": ms[0][1],
                     "tiny": [k for _p, k in ms if regions[k].tiny]}
        for _p, k in ms:
            rep_of[k] = rep
    out: list[str] = []
    placed: set[str] = set()
    for key in keys:
        rep = rep_of.get(key)
        if rep is None:
            out.append(key)
        elif rep not in placed:
            placed.add(rep)
            out.append(rep)
    return out, info


def group_hits(hits: list[Any]) -> list[Any]:
    """E's FIGURE hits (``SearchHit``) of one ranking → the grouped ranking, ranks renumbered from 1; a representative
    carries ``fields.fragments`` (ids) and ``fields.fragment_group`` (caption block, members, rank before, the member
    whose place it takes, tiny members)."""
    by_id = {h.id: h for h in hits}
    order, info = group([h.id for h in hits], {h.id: Region.of(h.page_id, h.fields) for h in hits})
    out = []
    for i, key in enumerate(order, 1):
        h = by_id[key]
        if key in info:
            g = info[key]
            h.fields["fragments"] = list(g["fragments"])
            h.fields["fragment_group"] = {k: g[k] for k in ("caption_block_id", "members", "rank_before",
                                                             "position_from", "tiny")}
        h.rank = i
        h.rank_in_kind = i
        out.append(h)
    return out


def lookup_regions(client: Any, index: str, ids: Iterable[str]) -> dict[str, Region]:
    """Regions of figures by id from E's figures index (one ``ids`` query; ``_source`` = page, caption block, bbox)."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    resp = client.search(index=index, body={"size": len(wanted), "_source": {"includes": list(REGION_FIELDS)},
                                            "query": {"ids": {"values": wanted}}})
    out = {}
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        out[str(src.get("id") or h.get("_id"))] = Region.of(src.get("page_id"), src)
    return out


def group_ranked(order: list[tuple[str, float]], kind_of: Mapping[str, str], known: Mapping[str, Region],
                 lookup: Callable[[list[str]], dict[str, Region]]
                 ) -> tuple[list[tuple[str, float]], dict[str, dict[str, Any]], dict[str, Any]]:
    """The hybrid final order → (grouped order, representative → info, stage record). ``known``: regions of FIGURE keys
    the BM25 leg returned with their fields; ``lookup`` fetches the others (dense / late-only keys). A failed lookup
    groups the known regions only and says so in the stage."""
    figures = [key for key, _s in order if kind_of.get(key) == "FIGURE"]
    regions = {key: known[key] for key in figures if key in known}
    missing = [key for key in figures if key not in regions]
    stage: dict[str, Any] = {"rule": RULE, "tiny": {"area_pt2": TINY_AREA_PT2, "side_pt": TINY_SIDE_PT},
                             "figures": len(figures), "looked_up": len(missing)}
    if missing:
        try:
            regions.update(lookup(missing))
        except Exception as exc:  # noqa: BLE001 - grouping is a presentation step; E's answer stands without it
            stage["lookup_error"] = type(exc).__name__
    keys, info = group([key for key, _s in order], regions)
    score_of = dict(order)
    stage.update({"status": "APPLIED" if info else "NO_GROUPS", "groups": len(info),
                  "fragments": sum(len(g["fragments"]) for g in info.values())})
    return [(key, score_of[key]) for key in keys], info, stage
