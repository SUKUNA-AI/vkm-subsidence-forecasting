"""Source scope: raw register value → ``vkm_world`` ``Scope`` values → mapping type (CP-07, audit A §2.4).

The register is never cleaned in place: ``site_scope_raw`` keeps the value verbatim. An unmapped raw value is a
blocking error (validator E10). AMBIGUOUS values are not resolved: ``site_scope = []`` and the possible readings go to
``site_scope_candidates`` (never use them as a "true" filter). On objects the scope is always named
``source_site_scope*`` — it is the scope of the whole source, not of each value (H-18).

Version 2 (owner's decision 13.10.2026): ``SKRU1_SKRU2_SKRU3`` (VKM-SRC-002, 012, 045 — works on all three Solikamsk
mines) is MULTI → SKRU1, SKRU2, SKRU3; in v1 it was AMBIGUOUS. The scope of the *source* says the work covers the three
mines; which mine a value belongs to is still read from the text (attribution per value, D-19). Canonical snapshots and
search projections built with v1 keep the old stored fields until they are rebuilt: the API maps the raw value with
the current table when it answers (:func:`current_scope`, :func:`with_current_scope`).

Crosswalk of the contract v0.2 §46 names (documentation only): ``SKRU1_EXACT`` ≙ ``SKRU1``;
``ANALOG`` ≙ raw ``NON_VKM_ANALOG`` → ``NON_VKM`` (the label is not the epistemic status ANALOGUE).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from vkm_world.core.provenance import Scope

from vkm_corpus.contracts.vocab import SiteScopeMapping

SITE_SCOPE_MAP_VERSION = "2"


class UnknownScopeError(KeyError):
    """A raw ``evidence_scope`` value that the versioned table does not map (blocking, E10)."""


@dataclass(frozen=True)
class ScopeMapping:
    raw: str
    scopes: tuple[str, ...]
    mapping: SiteScopeMapping
    candidates: tuple[str, ...] = ()


def _m(raw: str, scopes: tuple[Scope, ...], mapping: SiteScopeMapping,
       candidates: tuple[Scope, ...] = ()) -> tuple[str, ScopeMapping]:
    return raw, ScopeMapping(raw, tuple(s.value for s in scopes), mapping, tuple(c.value for c in candidates))


M = SiteScopeMapping
SITE_SCOPE_TABLE: dict[str, ScopeMapping] = dict([
    _m("GENERAL_METHOD", (Scope.GENERAL_METHOD,), M.EXACT),
    _m("VKM_REGIONAL", (Scope.VKM_REGIONAL,), M.EXACT),
    _m("OTHER_VKM_SITE", (Scope.OTHER_VKM_SITE,), M.EXACT),
    _m("NON_VKM_ANALOG", (Scope.NON_VKM,), M.SYNONYM),
    _m("SKRU1", (Scope.SKRU1,), M.EXACT),
    _m("METHOD_GENERAL", (Scope.GENERAL_METHOD,), M.SYNONYM),
    _m("VKM_Berezniki", (Scope.OTHER_VKM_SITE,), M.LOSSY),
    _m("VKM_REGIONAL_and_SKRU1", (Scope.VKM_REGIONAL, Scope.SKRU1), M.MULTI),
    _m("OTHER_POTASH_SITE", (Scope.OTHER_POTASH_SITE,), M.EXACT),
    _m("SKRU1_SKRU2_SKRU3", (Scope.SKRU1, Scope.SKRU2, Scope.SKRU3), M.MULTI),           # v2: owner 13.10.2026
    _m("VKM_Uralkali", (Scope.VKM_REGIONAL,), M.LOSSY),
    _m("SKRU1_SKRU2_PILLAR", (Scope.SKRU1_SKRU2_PILLAR,), M.EXACT),
    _m("VKM_regional", (Scope.VKM_REGIONAL,), M.CASE),
    _m("VKM_Solikamsk", (), M.AMBIGUOUS, (Scope.SKRU1_OR_SKRU2_UNATTRIBUTED,)),
    _m("other_potash_deposit", (Scope.OTHER_POTASH_SITE,), M.SYNONYM),
    _m("BKPRU4_VKM", (Scope.BKPRU4,), M.SYNONYM),
    _m("LEGACY_RETIRED", (), M.NOT_A_SCOPE),
    _m("SALT_DEPOSITS_USSR_incl_VKM", (Scope.NON_VKM, Scope.VKM_REGIONAL), M.MULTI),
])


def map_site_scope(raw: str) -> ScopeMapping:
    try:
        return SITE_SCOPE_TABLE[raw]
    except KeyError:
        raise UnknownScopeError(f"evidence_scope {raw!r} is not in the site scope table v{SITE_SCOPE_MAP_VERSION}")


SCOPE_VALUES: frozenset[str] = frozenset(s.value for s in Scope)


def current_scope(raw: Any) -> ScopeMapping | None:
    """The mapping of a raw register value under this table version; ``None`` for no value or one the table does not
    know (the caller keeps what is stored — the validator reports unknown values at build time)."""
    if raw is None or str(raw) not in SITE_SCOPE_TABLE:
        return None
    return SITE_SCOPE_TABLE[str(raw)]


def with_current_scope(row: Mapping[str, Any]) -> dict[str, Any]:
    """``row`` (a canonical ``sources`` row or an object row with ``source_site_scope*``) with its scope fields mapped
    from the raw value by this table version, so a snapshot built with an older map answers with the current one; the
    stored values are kept as ``*_stored`` only when they differ. Rows without a known raw value come back unchanged."""
    out = dict(row)
    for prefix in ("site_scope", "source_site_scope"):
        m = current_scope(row.get(f"{prefix}_raw"))
        if m is None:
            continue
        values, mapping, cands = list(m.scopes), m.mapping.value, list(m.candidates)
        stored = (list(row.get(prefix) or []), row.get(f"{prefix}_mapping"))
        if stored == (values, mapping):
            continue
        out[f"{prefix}_stored"] = stored[0]
        out[prefix], out[f"{prefix}_mapping"] = values, mapping
        if f"{prefix}_candidates" in row:
            out[f"{prefix}_candidates"] = cands
        if f"{prefix}_map_version" in row:
            out[f"{prefix}_map_version"] = SITE_SCOPE_MAP_VERSION
    return out
