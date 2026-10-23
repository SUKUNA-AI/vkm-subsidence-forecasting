"""Mining history of the SKRU-1 mine field as time intervals (pure functions, no I/O).

Used by ``scripts/build_skru1_mine_history.py``. Rules kept here so they are tested once:

* object names of the corpus («бл. 27», «Бл.49-51», «1ЮЗП», «6 западный блок 1 СЗП», «18П блок») map to stable keys
  (``BLK:27``, ``PAN:1ЮЗП``, ``SUB:1СЗП:6W``); a list or range is expanded only when every member is printed
  («блоки 49-51» → 49, 51 is NOT expanded to 49, 50, 51: the plan numbers blocks by twos, the printed range is kept as
  its end members and flagged);
* seams: КрII, АБ, Вс, Вк; «В» without composition stays ``В?``; pooled sylvinite stays ``SYLV``;
* dates are intervals: a printed year y means [y, y]; start and end bounds are kept separately; several sources are
  combined into an envelope and a certain core, and sources whose intervals do not overlap are a CONFLICT (never
  averaged);
* plan ≠ fact: only ACTUAL rows enter the mined-time envelope; PLANNED/DESIGN/FORECAST rows are kept apart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SEAMS = ("КрII", "АБ", "Вс", "Вк")
DECADES = tuple(range(1930, 2030, 10))
ACTUAL = frozenset({"ACTUAL", "STATE_SNAPSHOT"})
NOT_FACT_TIME = frozenset({"PLANNED", "DESIGN", "FORECAST", "EXPECTED"})

_PANEL_RE = re.compile(r"(\d{1,2})\s*[-–]?\s*(ЮЗП|СЗП|ЮВП|СВП|ЗП|ВП|СП)(?![А-Яа-я])")
_PANEL_RANGE_RE = re.compile(r"(\d{1,2})\s*(ЮЗП|СЗП|ЮВП|СВП|ЗП|ВП|СП)?\s*[-–]\s*(\d{1,2})\s*(ЮЗП|СЗП|ЮВП|СВП|ЗП|ВП|СП)(?![А-Яа-я])")
_PANEL_WORDS = (("юго-западн", "ЮЗП"), ("северо-западн", "СЗП"), ("юго-восточн", "ЮВП"), ("северо-восточн", "СВП"),
                ("западн", "ЗП"), ("восточн", "ВП"), ("северн", "СП"))
_PANEL_WORD_RE = re.compile(r"((?:\d{1,2}\s*(?:-?[йяе]{1,2})?\s*(?:,|и|-|–)?\s*)+)\s*((?:юго-|северо-)?(?:западн|восточн|северн))\w*\s+панел", re.I)
_SUB_RE = re.compile(r"(\d)\s*(западн|восточн)\w*\s+блок\w*(?:\s*\(?\s*(\d)\s*(СЗП|ЮВП)\)?)?", re.I)
_SUB_NORM_RE = re.compile(r"(\d)\s*(СЗП|ЮВП)\s*/\s*(\d)\s*(западн|восточн)", re.I)
_BLOCK_RE = re.compile(r"(?:\b|^)(?:бл(?:ок\w*|оки|\.)?|Бл(?:ок\w*|оки|\.)?)\s*№?\s*((?:\d{1,3}\s*(?:[-–,и]|и)\s*)*\d{1,3})", re.I)
_BLOCK_AFTER_RE = re.compile(r"(\d{1,3})\s*П?\s*блок", re.I)


def normalize_seams(text: str | None) -> tuple[str, ...]:
    """Seam keys named in a cell. Unknown composition of В stays ``В?``; pooled sylvinite ``SYLV``."""
    if not text:
        return ()
    t = text.replace("Кр.II", "КрII").replace("Кр. II", "КрII").replace("Кр-2", "КрII").replace("Кр2", "КрII")
    t = t.replace("Кр.2", "КрII").replace("Кр II", "КрII").replace("KrII", "КрII").replace("Кр-II", "КрII")
    out: list[str] = []
    low = t.lower()
    if "без разбивки" in low or "пулом" in low or "без раздел" in low:
        return ("SYLV",)
    if "крii" in low or "кр ii" in low:
        out.append("КрII")
    if re.search(r"(?<![А-Яа-я])АБ(?![А-Яа-я])", t) or "пласт аб" in low:
        out.append("АБ")
    if re.search(r"(?<![А-Яа-я])Вс(?![А-Яа-я])", t) or "в (сильвинит" in low or "в сильв" in low or "в сильвинитов" in low:
        out.append("Вс")
    if (re.search(r"(?<![А-Яа-я])Вк(?![А-Яа-я])", t) or "в (карналлит" in low or "в карн" in low
            or "карналлитов" in low and "пласт" in low):
        out.append("Вк")
    if not any(s in out for s in ("Вс", "Вк")) and re.search(r"(?<![А-Яа-яA-Za-z])В(?![А-Яа-яA-Za-z])", t):
        if "карналлит" in low:
            out.append("Вк")
        elif "сильвинит" in low and "пласт" in low:
            out.append("В?")
        else:
            out.append("В?")
    if not out and "сильвинит" in low:
        out.append("SYLV")
    if not out and "карналлит" in low:
        out.append("Вк")
    seen: list[str] = []
    for s in out:
        if s not in seen:
            seen.append(s)
    return tuple(seen)


def _expand_numbers(spec: str) -> tuple[list[int], bool]:
    """«49-51» → [49, 51] (range kept as its printed ends, flagged); «33, 44 и 46» → [33, 44, 46]."""
    nums = [int(x) for x in re.findall(r"\d{1,3}", spec)]
    is_range = bool(re.search(r"\d\s*[-–]\s*\d", spec))
    return nums, is_range


def normalize_objects(text: str | None) -> tuple[list[str], list[str]]:
    """Object keys named in a cell and flags. Keys: ``BLK:<n>``, ``PAN:<n><kind>``, ``SUB:<n><kind>:<k><W|E>``.

    Flags: ``RANGE_ENDS_ONLY`` (a printed range was reduced to its ends), ``NO_OBJECT`` (nothing recognised).
    """
    if not text:
        return [], ["NO_OBJECT"]
    t = text.replace(" ", " ")
    keys: list[str] = []
    flags: list[str] = []
    m = _SUB_NORM_RE.search(t)
    if m:
        side = "W" if m.group(4).lower().startswith("запад") else "E"
        keys.append(f"SUB:{m.group(1)}{m.group(2).upper()}:{m.group(3)}{side}")
        t = t[:m.start()] + t[m.end():]
    for m in _SUB_RE.finditer(t):
        side = "W" if m.group(2).lower().startswith("запад") else "E"
        parent = f"{m.group(3)}{m.group(4).upper()}" if m.group(3) else ("1СЗП" if "СЗП" in t else "")
        if parent:
            keys.append(f"SUB:{parent}:{m.group(1)}{side}")
        else:
            flags.append("SUBBLOCK_PARENT_UNKNOWN")
    if keys:
        t = _SUB_RE.sub(" ", t)
    for m in _PANEL_WORD_RE.finditer(t):
        word = m.group(2).lower()
        kind = next(k for w, k in _PANEL_WORDS if word.startswith(w))
        spec = m.group(1)
        rng = re.fullmatch(r"\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s*", spec)
        if rng and int(rng.group(2)) > int(rng.group(1)):
            keys.extend(f"PAN:{n}{kind}" for n in range(int(rng.group(1)), int(rng.group(2)) + 1))
            flags.append("PANEL_RANGE_EXPANDED")
        else:
            for n in re.findall(r"\d{1,2}", spec):
                keys.append(f"PAN:{int(n)}{kind}")
    t = _PANEL_WORD_RE.sub(" ", t)
    m6 = re.search(r"1\s*СЗП\W+блок\w*\s*(\d)(?!\d)", t)
    if m6 and not any(k.startswith("SUB:1СЗП:") for k in keys):
        keys.append(f"SUB:1СЗП:{m6.group(1)}?")
        flags.append("SUBBLOCK_SIDE_UNKNOWN")
        t = t[:m6.start()] + " " + t[m6.end():]
    ranges = []
    for m in _PANEL_RANGE_RE.finditer(t):
        a, kind_a, b, kind = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        if (kind_a is None or kind_a == kind) and b >= a and b - a <= 12:
            keys.extend(f"PAN:{n}{kind}" for n in range(a, b + 1))
            flags.append("PANEL_RANGE_EXPANDED")
            ranges.append((m.start(), m.end()))
    t2 = t
    for a0, a1 in reversed(ranges):
        t2 = t2[:a0] + " " + t2[a1:]
    for m in _PANEL_RE.finditer(t2):
        k = f"PAN:{int(m.group(1))}{m.group(2)}"
        if k not in keys and not (m.group(2) in ("СЗП", "ЮВП") and any(x.startswith(f"SUB:{int(m.group(1))}{m.group(2)}:")
                                                                         for x in keys)) and k != "PAN:1СЗП" or (
                k == "PAN:1СЗП" and not any(x.startswith("SUB:1СЗП:") for x in keys) and k not in keys):
            keys.append(k)
    t3 = _PANEL_RE.sub(" ", t2)
    for m in _BLOCK_RE.finditer(t3):
        nums, is_range = _expand_numbers(m.group(1))
        keys.extend(f"BLK:{n}" for n in nums)
        if is_range:
            flags.append("RANGE_ENDS_ONLY")
    if not any(k.startswith("BLK:") for k in keys):
        for m in _BLOCK_AFTER_RE.finditer(t3):
            keys.append(f"BLK:{int(m.group(1))}")
    out: list[str] = []
    for k in keys:
        if k not in out:
            out.append(k)
    if not out:
        flags.append("NO_OBJECT")
    return out, flags


def plan_key(kind: str, label: str, parent: str = "") -> str | None:
    """Key of a labelled plan region (C4 map layer PANEL_BLOCK_LAYOUT_014)."""
    label = (label or "").strip()
    if kind == "BLOCK" and label.isdigit():
        return f"BLK:{int(label)}"
    if kind == "PANEL":
        m = _PANEL_RE.search(label)
        return f"PAN:{int(m.group(1))}{m.group(2)}" if m else None
    if kind == "SUBBLOCK":
        m = re.match(r"(\d)\s*(западн|восточн)", label, re.I)
        pm = _PANEL_RE.search(parent or "")
        if m and pm:
            side = "W" if m.group(2).lower().startswith("запад") else "E"
            return f"SUB:{int(pm.group(1))}{pm.group(2)}:{m.group(1)}{side}"
    return None


@dataclass(frozen=True)
class YearInterval:
    """Bounds of a start and an end year (inclusive). ``None`` = not bounded by this record."""

    start_min: int | None
    start_max: int | None
    end_min: int | None
    end_max: int | None

    def __post_init__(self):
        for a, b in ((self.start_min, self.start_max), (self.end_min, self.end_max), (self.start_min, self.end_max)):
            if a is not None and b is not None and a > b:
                raise ValueError(f"interval bounds out of order: {self}")

    @property
    def earliest(self) -> int | None:
        return self.start_min

    @property
    def latest(self) -> int | None:
        return self.end_max

    def filled(self, open_lo: int, open_hi: int) -> "YearInterval":
        """Open bounds replaced by the global bounds of the field (commissioning, last source year)."""
        smin = self.start_min if self.start_min is not None else open_lo
        emax = self.end_max if self.end_max is not None else open_hi
        smax = self.start_max if self.start_max is not None else (self.end_max if self.end_max is not None else open_hi)
        emin = self.end_min if self.end_min is not None else (self.start_min if self.start_min is not None else open_lo)
        return YearInterval(smin, smax, emin, emax)

    def core(self) -> tuple[int, int] | None:
        """Years certainly inside the activity: [start_max, end_min] when both are known and ordered."""
        if self.start_max is None or self.end_min is None or self.start_max > self.end_min:
            return None
        return self.start_max, self.end_min

    def envelope(self) -> tuple[int, int] | None:
        lo, hi = self.earliest, self.latest
        if lo is None or hi is None:
            return None
        return lo, hi


def interval_from_row(start_min, start_max, end_min, end_max) -> YearInterval | None:
    """Interval from the four bound cells of a finding. A missing bound stays open: «не позднее 2019» is end_max = 2019
    with end_min open. Only logical cross-bounds are added: the start is not later than the latest end, and the end is
    not earlier than the earliest start."""
    def num(v):
        if v is None:
            return None
        s = str(v).strip()
        return int(float(s)) if re.fullmatch(r"-?\d+(\.0+)?", s) else None
    smin, smax, emin, emax = num(start_min), num(start_max), num(end_min), num(end_max)
    if all(v is None for v in (smin, smax, emin, emax)):
        return None
    if smax is None and emax is not None:
        smax = emax
    if emin is None and smin is not None:
        emin = smin
    return YearInterval(smin, smax, emin, emax)


@dataclass
class Combined:
    envelope: tuple[int, int] | None
    core: tuple[int, int] | None
    n_records: int
    conflict: bool
    members: list[str] = field(default_factory=list)


def combine_intervals(items: list[tuple[str, YearInterval]], open_lo: int | None = None,
                      open_hi: int | None = None) -> Combined:
    """Envelope and certain core of several records of one activity. Open bounds are filled with ``open_lo`` /
    ``open_hi`` when given. Records whose envelopes do not overlap are a CONFLICT (two dates for one event or two
    separate campaigns — the caller keeps both rows)."""
    if open_lo is not None and open_hi is not None:
        items = [(rid, iv.filled(open_lo, open_hi)) for rid, iv in items]
    envs = [(rid, iv.envelope()) for rid, iv in items if iv.envelope()]
    if not envs:
        return Combined(None, None, len(items), False, [rid for rid, _ in items])
    lo = min(e[0] for _, e in envs)
    hi = max(e[1] for _, e in envs)
    conflict = False
    for i in range(len(envs)):
        for j in range(i + 1, len(envs)):
            a, b = envs[i][1], envs[j][1]
            if a[1] < b[0] or b[1] < a[0]:
                conflict = True
    cores = [iv.core() for _, iv in items if iv.core()]
    core = (min(c[0] for c in cores), max(c[1] for c in cores)) if cores and not conflict else None
    return Combined((lo, hi), core, len(items), conflict, [rid for rid, _ in items])


def decade_state(envelope: tuple[int, int] | None, core: tuple[int, int] | None, decade: int) -> str:
    """State of an activity in a decade: ACTIVE (certain core overlaps), POSSIBLE (only the envelope overlaps),
    NO (envelope outside), UNKNOWN (no dates)."""
    d0, d1 = decade, decade + 9
    if envelope is None:
        return "UNKNOWN"
    if core and core[0] <= d1 and core[1] >= d0:
        return "ACTIVE"
    if envelope[0] <= d1 and envelope[1] >= d0:
        return "POSSIBLE"
    return "NO"


def areal_extraction_ratio(chamber_m: float, pillar_m: float) -> float:
    """Areal extraction of a ribbon-pillar layout k = a / (a + b) (DERIVATION; ignores height and losses)."""
    if chamber_m <= 0 or pillar_m < 0:
        raise ValueError("chamber width must be positive and pillar width non-negative")
    return chamber_m / (chamber_m + pillar_m)


def overlapping_same_seam(units: list[dict], area_tol_m2: float = 2000.0) -> list[dict]:
    """Pairs of units of one seam whose plan polygons overlap by more than ``area_tol_m2`` while their certain mining
    cores overlap in time. ``units`` items: id, seam, geom (shapely), core (tuple|None). Returns issue records."""
    issues = []
    by_seam: dict[str, list[dict]] = {}
    for u in units:
        if u.get("geom") is not None and u.get("core"):
            by_seam.setdefault(u["seam"], []).append(u)
    for seam, lst in sorted(by_seam.items()):
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i], lst[j]
                if a["core"][1] < b["core"][0] or b["core"][1] < a["core"][0]:
                    continue
                inter = a["geom"].intersection(b["geom"]).area
                if inter > area_tol_m2:
                    issues.append({"seam": seam, "unit_a": a["id"], "unit_b": b["id"], "overlap_m2": round(inter, 1),
                                   "core_a": a["core"], "core_b": b["core"]})
    return issues


_ASCII = (("ЮЗП", "YuZP"), ("СЗП", "SZP"), ("ЮВП", "YuVP"), ("СВП", "SVP"), ("ЗП", "ZP"), ("ВП", "VP"), ("СП", "SP"),
          ("КрII", "KR2"), ("АБ", "AB"), ("Вс", "VS"), ("Вк", "VK"), ("В?", "VQ"), ("?", "Q"))


def ascii_id(text: str) -> str:
    """ASCII id fragment for keys and seams (WorldSpec ids allow letters, digits, _ . : - / +)."""
    for a, b in _ASCII:
        text = text.replace(a, b)
    text = re.sub(r"[^A-Za-z0-9_.:/+-]", "_", text)
    return text

