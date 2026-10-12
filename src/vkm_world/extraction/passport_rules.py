"""Rules shared by the world passport registry (``scripts/build_world_passport.py``) and the prior proposal
(``scripts/build_world_priors.py``), 12.10.2026.

Three questions are kept apart and reported in separate columns: does the row belong to the site
(``site_check``), was its value checked on the page (``value_check``), is the source the origin of the number
(``origin_check``). A scientific use is never established here — that is the separate ScientificUseAdmission
contract — so ``scientific_use`` is always ``NOT_ESTABLISHED``.

- Origin: ORIGINAL only when the input records it (``original_or_cited`` / ``origin``). ``UNKNOWN``,
  ``ORIGINAL_OR_UNSTATED``, ``UNSTATED…``, ``EXTERNAL…`` and an empty field are UNKNOWN; free text that starts with
  ``CITED`` is CITED. An input without an origin field never yields ORIGINAL.
- FACT for the world: every row of the group is ORIGINAL, natural scale (FIELD / MASSIF), status FACT, its value
  reviewed on the page, and its mine named on the page or by review (not inferred from the document context). The
  name of a mine or the presence of a number alone never raises a status.
- Copies: equal numbers are not identity. A row leaves the counting only when its identity is confirmed (see
  :data:`CONFIRMED_DUPLICATE_ROLES`); other equal numbers stay counted as match candidates.
- Stable ids: :class:`IdLedger` keeps every issued id with its natural key; a new row never takes an old number,
  a key no longer produced is RETIRED and names its successors.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict

ORIGINAL, CITED, UNKNOWN = "ORIGINAL", "CITED", "UNKNOWN"
# verification of the value on the page beyond the programmatic presence check
VALUE_REVIEWED = frozenset({"PHASE1_REVIEWED", "VERIFIED_ON_PAGE_BY_AGENT", "HAND_SHEET_08_10", "ADJUDICATED_OK",
                            "ADJUDICATED_CORRECTED", "ADJUDICATED_JUDGE", "ADJUDICATED_TWO_LENS",
                            "VERIFIED_IMAGE_CHECK"})
NATURAL_SCALES = frozenset({"FIELD", "MASSIF"})
INFERRED_SITE_BASIS = "DOCUMENT_CONTEXT"
SCIENTIFIC_USE = "NOT_ESTABLISHED"
# roles that remove a row from counting: identity confirmed, not only an equal number
CONFIRMED_DUPLICATE_ROLES = frozenset({"CONFIRMED_REEXTRACTION", "COPY_CITED_CONFIRMED"})
CANDIDATE_ROLES = frozenset({"SAME_PAGE_VALUE_CANDIDATE", "COPY_CITED_CANDIDATE", "SAME_VALUE_OTHER_SOURCE",
                             "REEXTRACTION_CANDIDATE"})


def origin_class(origin: str | None) -> str:
    o = (origin or "").strip().upper()
    if o == ORIGINAL or o.startswith("ORIGINAL ("):
        return ORIGINAL
    if o.startswith(CITED):
        return CITED
    return UNKNOWN


def value_reviewed(row: dict) -> bool:
    return row.get("verification") in VALUE_REVIEWED and row.get("status") != "AUTO_EXTRACTED_UNREVIEWED"


def evidence_kind(scale: str | None) -> str:
    """Measurements, parameters of published models and norms are kept apart (never one range)."""
    s = scale or "UNKNOWN"
    if s in ("LAB", "FIELD", "MASSIF"):
        return "MEASUREMENT"
    if s in ("MODEL", "CALIBRATED", "DERIVATION"):
        return "PUBLISHED_MODEL"
    if s in ("NORMATIVE", "DESIGN"):
        return "NORMATIVE"
    return "UNKNOWN_KIND"


def _norm_text(s: str | None) -> str:
    t = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"\s+", "", t)


def definition_key(equation: str | None) -> str:
    """Identity of a law definition: the normalised equation (spaces and case ignored) or UNSTATED."""
    t = _norm_text(equation)
    return "def:" + hashlib.sha256(t.encode("utf-8")).hexdigest()[:10] if t else "def:UNSTATED"


def checks(rows: list[dict]) -> dict:
    origins = {origin_class(x.get("origin")) for x in rows}
    reviewed = [value_reviewed(x) for x in rows]
    bases = {(x.get("site_basis") or "").split(" ")[0] for x in rows}
    return {
        "origin_check": ("ALL_ORIGINAL" if origins == {ORIGINAL} else
                         ";".join(f"HAS_{o}" for o in sorted(origins - {ORIGINAL})) if rows else ""),
        "value_check": ("" if not rows else "ALL_REVIEWED" if all(reviewed) else
                        "PARTLY_REVIEWED" if any(reviewed) else "UNREVIEWED"),
        "site_check": ("" if not rows else "SITE_FROM_DOCUMENT_CONTEXT" if INFERRED_SITE_BASIS in bases
                       else "SITE_ON_PAGE_OR_REVIEWED"),
        "scientific_use": SCIENTIFIC_USE,
    }


def fact_failures(rows: list[dict]) -> list[str]:
    """Why the rows of one site are not FACT for that site's world (empty list: FACT)."""
    if not rows:
        return ["нет evidence"]
    out = []
    origins = {origin_class(x.get("origin")) for x in rows}
    if CITED in origins:
        out.append("есть пересказ (CITED)")
    if UNKNOWN in origins:
        out.append("происхождение не установлено (UNKNOWN ≠ ORIGINAL)")
    if not all((x.get("scale") or "") in NATURAL_SCALES for x in rows):
        out.append("не только натурный масштаб")
    if not all(x.get("status") == "FACT" for x in rows):
        out.append("статус строки не FACT")
    if not all(value_reviewed(x) for x in rows):
        out.append("значение не проверено по странице")
    if any((x.get("site_basis") or "").startswith(INFERRED_SITE_BASIS) for x in rows):
        out.append("рудник — по контексту документа")
    return out


# ------------------------------------------------------------------------------------------------ stable ids
LEDGER_COLS = ["id", "kind", "natural_key", "state", "since_rule", "retired_rule", "superseded_by", "previous_keys"]


class IdLedger:
    """Append-only register of issued ids. ``assign`` gives every produced natural key an id:

    1. the key is in the ledger (ACTIVE or RETIRED) → its id (a retired key that returns is reactivated);
    2. the key is new, its ``base`` (the key format of the previous rule) is an ACTIVE ledger key that is not
       produced any more and is the base of exactly this one new key → the id is inherited, the old key is kept in
       ``previous_keys``;
    3. otherwise a new number (the largest issued number of the kind + 1, new keys in sorted order).

    ACTIVE keys that are not produced become RETIRED; ``superseded_by`` names the ids of the produced keys whose base
    is the retired key (a split) — references to an old id stay resolvable, nothing is renumbered.
    """

    def __init__(self, rows: list[dict] | None = None):
        self.rows = [dict(r) for r in rows or []]

    def _num(self, ident: str) -> int:
        return int(ident.rsplit("-", 1)[1])

    def assign(self, kind: str, keys: list[tuple[str, str]], rule: str, width: int) -> dict[str, str]:
        """``keys``: (natural key, base key) of every produced row of ``kind`` (unique keys). Returns key → id."""
        mine = [r for r in self.rows if r["kind"] == kind]
        by_key = {r["natural_key"]: r for r in mine}
        produced = {k for k, _ in keys}
        if len(produced) != len(keys):
            raise ValueError(f"{kind}: natural keys are not unique")
        base_users = defaultdict(list)
        for k, b in keys:
            if b and b != k:
                base_users[b].append(k)
        out, new = {}, []
        for k, b in sorted(keys):
            r = by_key.get(k)
            if r is not None:
                if r["state"] != "ACTIVE":
                    r.update(state="ACTIVE", retired_rule="", superseded_by="")
                out[k] = r["id"]
                continue
            old = by_key.get(b) if b and b != k else None
            if (old is not None and old["state"] == "ACTIVE" and b not in produced
                    and base_users.get(b) == [k]):
                old["previous_keys"] = ";".join(filter(None, [old.get("previous_keys", ""), b]))
                old["natural_key"] = k
                by_key[k] = old
                del by_key[b]
                out[k] = old["id"]
                continue
            new.append(k)
        nxt = max((self._num(r["id"]) for r in mine), default=0)
        for k in new:
            nxt += 1
            r = {"id": f"{kind}-{nxt:0{width}d}", "kind": kind, "natural_key": k, "state": "ACTIVE",
                 "since_rule": rule, "retired_rule": "", "superseded_by": "", "previous_keys": ""}
            self.rows.append(r)
            by_key[k] = r
            out[k] = r["id"]
        for k, r in by_key.items():
            if r["state"] == "ACTIVE" and k not in produced:
                succ = sorted(out[s] for s in base_users.get(k, ()))
                r.update(state="RETIRED", retired_rule=rule, superseded_by=";".join(succ))
        return out

    def table(self) -> list[dict]:
        return sorted(self.rows, key=lambda r: (r["kind"], self._num(r["id"])))
