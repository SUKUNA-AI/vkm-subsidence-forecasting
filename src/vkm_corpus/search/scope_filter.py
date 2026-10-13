"""What a ``source_scope`` filter leaves out (agent Б, 13.10): sources that may hold the requested area but are not
mapped to it — never resolved here, only named.

The scope of a source is the versioned mapping of its register value (``vkm_corpus.contracts.site_scope``; not changed
here). A filter ``source_scope = [SKRU1]`` takes the sources whose ``site_scope`` holds SKRU1 under the *current* map —
on a snapshot built with an older map those whose stored scope lacks it are added by ``source_id``
(:func:`remapped_sources`; e.g. ``SKRU1_SKRU2_SKRU3`` → SKRU1, SKRU2, SKRU3 since map v2, owner 13.10.2026). It does
not take:

* **AMBIGUOUS** sources (``site_scope = []``) whose ``site_scope_candidates`` hold the requested area or a related one
  — e.g. raw ``VKM_Solikamsk``: candidate SKRU1_OR_SKRU2_UNATTRIBUTED (``SKRU1_SKRU2_SKRU3`` of VKM-SRC-002, 012, 045
  was AMBIGUOUS until map v2);
* sources mapped to a **related** area (:data:`RELATED_SCOPES`) but not to the requested one — e.g. VKM-SRC-110,
  ``SKRU1_SKRU2_PILLAR`` (EXACT): the intermine pillar zone on the SKRU-1/SKRU-2 boundary.

Related areas (MODEL_CHOICE, from the ``Scope`` definitions of ``vkm_world``): an area whose data *may* include the
requested mine without being attributed to it — SKRU1 and SKRU2: SKRU1_SKRU2_PILLAR (the boundary pillar zone),
SKRU1_OR_SKRU2_UNATTRIBUTED (the source does not say which), SOLIKAMSK_GROUP (pooled over SKRU-1/2/3) and the legacy
SKRU1_SKRU2; SKRU3: SOLIKAMSK_GROUP. Every other area has no related area: only its AMBIGUOUS candidates count.

The search answers with the warning ``SCOPE_FILTER_EXCLUDED_AMBIGUOUS`` (count and source ids). With
``include_ambiguous = true`` the filter becomes «the area OR one of these sources» (``source_scope_or_source_id``, the
same clause for BM25, dense k-NN, the page-image channel and the bibliographic channel — every document carries
``source_id``; no reindexing) and the answer says which sources came in that way (``SCOPE_FILTER_INCLUDED_AMBIGUOUS``).
A source taken in this way stays what it is: its envelope keeps ``source_scope`` (raw value, mapping AMBIGUOUS or
the related area) — the filter widens the search, it never attributes the source to the area.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

_SKRU12 = frozenset({"SKRU1_SKRU2_PILLAR", "SKRU1_OR_SKRU2_UNATTRIBUTED", "SOLIKAMSK_GROUP", "SKRU1_SKRU2"})
RELATED_SCOPES: dict[str, frozenset[str]] = {
    "SKRU1": _SKRU12,
    "SKRU2": _SKRU12,
    "SKRU3": frozenset({"SOLIKAMSK_GROUP"}),
}
AMBIGUOUS = "AMBIGUOUS"
MAX_LISTED = 50                    # source ids listed in a warning (the count is always complete)


def related(requested: Iterable[str]) -> frozenset[str]:
    """The requested areas with their related ones."""
    req = {str(s) for s in requested}
    return frozenset(req.union(*(RELATED_SCOPES.get(s, frozenset()) for s in req)))


def excluded_sources(requested: Iterable[str], sources: Iterable[Mapping[str, Any]],
                     within: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Sources a ``source_scope`` filter on ``requested`` leaves out although they may hold the area (see the module
    note), sorted by id; ``within`` (the request's own ``source_id`` filter) narrows them. A row: ``source_id``,
    ``site_scope``, ``site_scope_candidates``, ``site_scope_mapping``, ``site_scope_raw`` (the canon's ``sources``)."""
    req = {str(s) for s in requested}
    rel = related(req)
    allowed = None if within is None else {str(s) for s in within}
    out = []
    for s in sources:
        sid = str(s.get("source_id") or "")
        if not sid or (allowed is not None and sid not in allowed):
            continue
        scope = {str(x) for x in s.get("site_scope") or []}
        if scope & req:
            continue                                   # the filter takes it already
        cands = {str(x) for x in s.get("site_scope_candidates") or []}
        mapping = s.get("site_scope_mapping")
        if mapping == AMBIGUOUS and cands & rel:
            reason, matched = "AMBIGUOUS_CANDIDATE", sorted(cands & rel)
        elif scope & (rel - req):
            reason, matched = "RELATED_SCOPE", sorted(scope & (rel - req))
        else:
            continue
        out.append({"source_id": sid, "reason": reason, "matched": matched, "site_scope_raw": s.get("site_scope_raw"),
                    "site_scope_mapping": mapping})
    return sorted(out, key=lambda r: r["source_id"])


def remapped_sources(requested: Iterable[str], sources: Iterable[Mapping[str, Any]],
                     within: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Sources whose scope under the current map (``site_scope``, see ``contracts.site_scope.with_current_scope``)
    holds a requested area while the snapshot's stored scope (``site_scope_stored``, what the search index carries)
    does not — e.g. ``SKRU1_SKRU2_SKRU3`` (map v2, owner 13.10.2026) on a v1 snapshot. The filter takes them by
    ``source_id`` until the projections are rebuilt; sorted by id."""
    req = {str(s) for s in requested}
    allowed = None if within is None else {str(s) for s in within}
    out = []
    for s in sources:
        sid = str(s.get("source_id") or "")
        if not sid or "site_scope_stored" not in s or (allowed is not None and sid not in allowed):
            continue
        if {str(x) for x in s.get("site_scope") or []} & req and not {str(x) for x in s["site_scope_stored"]} & req:
            out.append({"source_id": sid, "reason": "SCOPE_MAP_CURRENT", "site_scope_raw": s.get("site_scope_raw")})
    return sorted(out, key=lambda r: r["source_id"])


def widen(filters: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """``filters`` with ``source_scope`` replaced by «the area OR one of ``sources``» (no sources → unchanged)."""
    if not sources or not filters.get("source_scope"):
        return dict(filters)
    out = {k: v for k, v in filters.items() if k != "source_scope"}
    scopes = filters["source_scope"]
    out["source_scope_or_source_id"] = {"source_scope": list(scopes) if isinstance(scopes, (list, tuple)) else
                                        [scopes], "source_id": [s["source_id"] for s in sources]}
    return out


def message(sources: list[dict[str, Any]], *, included: bool) -> str:
    ids = ", ".join(s["source_id"] for s in sources[:MAX_LISTED]) + (" …" if len(sources) > MAX_LISTED else "")
    if included:
        return (f"include_ambiguous: also searched sources not attributed to the area (AMBIGUOUS mapping or a related "
                f"area; their envelopes keep their own source_scope): {ids}")
    return (f"the source_scope filter left out sources that may hold the area (AMBIGUOUS mapping or a related area): "
            f"{ids}; include_ambiguous=true searches them too")
