"""Validation designs that respect temporal and spatial dependence (data-free, stdlib only).

Repeated epochs of one benchmark / survey line / borehole are dependent: a random row split or a plain
K-fold puts the same trajectory on both sides and scores memorisation. Allowed designs:

* forward-only rolling origin: at cut ``c`` training uses only labels known by ``c``
  (``target <= c``), validation uses samples issued at origin ``c``;
* grouped hold-outs: leave-one-borehole-out, leave-one-line-out, leave-one-profile-out, ...
  (spatial extrapolation; combine with rolling origin for temporal extrapolation).

The test set stays sealed: no design here may consume it; access goes only through
``vkm_world.validation.access`` after a candidate is frozen.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Collection, Iterable, Mapping, NoReturn

from .leakage import LeakageViolation, as_date


class UnsafeSplitError(LeakageViolation):
    """Row-random, plain K-fold, unrecognised or time-violating validation design."""


class SealedTestError(PermissionError):
    """Test samples requested before a frozen candidate is authorised (or inside a development design)."""


# ---------------------------------------------------------------- forbidden splitters
def reject_random_train_test_split(*_: Any, **__: Any) -> NoReturn:
    raise UnsafeSplitError("random row train/test split is prohibited; use frozen manifests and forward-only "
                           "or grouped designs")


def reject_plain_kfold(*_: Any, **__: Any) -> NoReturn:
    raise UnsafeSplitError("plain K-fold is prohibited for repeated trajectories; use rolling origin or "
                           "leave-one-group-out")


FORWARD_ONLY_SPLITTERS = frozenset({"rollingorigin", "expandingwindow", "forwardchaining"})
# spatial / object groups; 'campaign' is deliberately absent (a temporal group is not forward-only)
GROUP_KINDS = frozenset({"group", "borehole", "line", "profile", "benchmark", "point", "zone", "panel", "site"})
GROUPED_SPLITTERS = frozenset({f"leaveone{g}out" for g in GROUP_KINDS} | {"groupkfold"})
_FORBIDDEN_TOKENS = ("random", "shuffle", "traintestsplit")


def normalize_splitter_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def validate_splitter_name(name: str) -> str:
    """Normalised name of an allowed design; anything else raises (fail closed)."""
    n = normalize_splitter_name(name)
    if not n or any(t in n for t in _FORBIDDEN_TOKENS) or ("kfold" in n and "group" not in n):
        raise UnsafeSplitError(f"forbidden split strategy: {name!r}")
    if n in FORWARD_ONLY_SPLITTERS or n in GROUPED_SPLITTERS:
        return n
    raise UnsafeSplitError(f"unrecognised split strategy {name!r}; allowed: rolling origin or "
                           f"leave-one-<{'/'.join(sorted(GROUP_KINDS))}>-out")


# ---------------------------------------------------------------- assignments
@dataclass(frozen=True)
class FoldAssignment:
    fold_id: str
    role: str        # "train" | "validation"
    sample_id: str
    held_out: str    # cut date (ISO) or held-out group


def sample_id_list_sha256(sample_ids: Iterable[str]) -> str:
    """Hash of the ordered id list (independent of CSV dialect / line endings)."""
    return hashlib.sha256("\n".join(map(str, sample_ids)).encode("utf-8")).hexdigest()


def _refuse_sealed(ids: Iterable[str], sealed: Collection[str]) -> None:
    hit = sorted(set(ids) & set(map(str, sealed)))
    if hit:
        raise SealedTestError(f"validation design must not consume sealed test samples: {hit[:5]} ({len(hit)})")


def _dates(mapping: Mapping[str, Any], what: str) -> dict[str, date]:
    out = {str(k): as_date(v) for k, v in mapping.items()}
    missing = sorted(k for k, v in out.items() if v is None)
    if missing:
        raise ValueError(f"{what} unknown for samples {missing[:5]} ({len(missing)})")
    return out  # type: ignore[return-value]


def rolling_origin_assignments(origin_dates: Mapping[str, Any], target_dates: Mapping[str, Any], *,
                               minimum_train_dates: int = 4, maximum_folds: int = 5,
                               sealed: Collection[str] = ()) -> list[FoldAssignment]:
    """Deterministic expanding-window folds.

    ``target_dates`` may be label availability dates (>= target date) for a stricter design. A cut ``c``
    is an origin date with at least ``minimum_train_dates`` distinct label dates ``<= c``; the last
    ``maximum_folds`` cuts are used.
    """
    validate_splitter_name("rolling_origin")
    if minimum_train_dates < 1 or maximum_folds < 1:
        raise ValueError("minimum_train_dates and maximum_folds must be >= 1")
    origins, targets = _dates(origin_dates, "origin date"), _dates(target_dates, "target date")
    if set(origins) != set(targets):
        raise ValueError("origin_dates and target_dates must cover the same samples")
    _refuse_sealed(origins, sealed)
    cuts = [c for c in sorted(set(origins.values()))
            if len({t for t in targets.values() if t <= c}) >= minimum_train_dates][-maximum_folds:]
    if not cuts:
        raise ValueError("not enough history for a rolling-origin design")
    rows: list[FoldAssignment] = []
    for k, c in enumerate(cuts, start=1):
        fold = f"rolling_{k:02d}"
        rows += [FoldAssignment(fold, "train", s, c.isoformat()) for s in sorted(targets) if targets[s] <= c]
        rows += [FoldAssignment(fold, "validation", s, c.isoformat()) for s in sorted(origins) if origins[s] == c]
    assert_forward_only(rows, origins, targets)
    return rows


def assert_forward_only(assignments: Iterable[FoldAssignment], origin_dates: Mapping[str, Any],
                        target_dates: Mapping[str, Any]) -> None:
    """In every fold, each training label is known by the earliest validation origin; roles are disjoint."""
    origins, targets = _dates(origin_dates, "origin date"), _dates(target_dates, "target date")
    folds: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"train": set(), "validation": set()})
    for a in assignments:
        if a.role not in ("train", "validation"):
            raise UnsafeSplitError(f"{a.fold_id}: unknown role {a.role!r}")
        folds[a.fold_id][a.role].add(a.sample_id)
    for fold, roles in folds.items():
        train, val = roles["train"], roles["validation"]
        if not train or not val or train & val:
            raise UnsafeSplitError(f"{fold}: empty or overlapping train/validation roles")
        if max(targets[s] for s in train) > min(origins[s] for s in val):
            raise UnsafeSplitError(f"{fold}: a training label is not yet known at the validation origin")


def leave_one_group_out_assignments(sample_groups: Mapping[str, Any], *, group_kind: str = "group",
                                    sealed: Collection[str] = ()) -> list[FoldAssignment]:
    """One fold per group: that group validates, all other groups train."""
    validate_splitter_name(f"leave_one_{group_kind}_out")
    groups = {str(s): g for s, g in sample_groups.items()}
    nulls = sorted(s for s, g in groups.items() if g is None or str(g).strip() == "")
    if nulls:
        raise ValueError(f"{group_kind} unknown for samples {nulls[:5]} ({len(nulls)})")
    _refuse_sealed(groups, sealed)
    distinct = sorted({str(g) for g in groups.values()})
    if len(distinct) < 2:
        raise ValueError(f"need at least two {group_kind} groups for leave-one-{group_kind}-out")
    return [FoldAssignment(f"leave_one_{group_kind}_out:{g}", "validation" if str(groups[s]) == g else "train",
                           s, g)
            for g in distinct for s in sorted(groups)]


def leave_one_borehole_out(sample_boreholes: Mapping[str, Any], *, sealed: Collection[str] = ()) -> list[FoldAssignment]:
    return leave_one_group_out_assignments(sample_boreholes, group_kind="borehole", sealed=sealed)


def leave_one_line_out(sample_lines: Mapping[str, Any], *, sealed: Collection[str] = ()) -> list[FoldAssignment]:
    return leave_one_group_out_assignments(sample_lines, group_kind="line", sealed=sealed)
