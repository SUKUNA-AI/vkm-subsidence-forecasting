"""Route-independent primitives of a plot: texts and paths in one linear drawing frame (units and orientation of the
route: DXF inches y-up, PDF points y-down, raster pixels y-down), plus the parsers of printed tick labels."""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

import numpy as np

# advance width / cap height, averaged over Calibri, Arial and Times: estimates the centre of a label whose width
# the route does not give (DXF MTEXT); snapping to tick marks removes the residual bias
ADVANCE = {
    **{d: 0.78 for d in "0123456789"}, "-": 0.47, "−": 0.78, "–": 0.78, "—": 1.4, "+": 0.78, ".": 0.39, ",": 0.39,
    " ": 0.37, ":": 0.39, "/": 0.55, "%": 1.2,
}


def text_width(s: str, cap_height: float) -> float:
    w = 0.0
    for ch in s:
        if ch in ADVANCE:
            w += ADVANCE[ch]
        elif ch.isupper():
            w += 0.95
        else:
            w += 0.75
    return w * cap_height


@dataclass
class Text:
    """A text item: left/right edge, vertical centre, cap height (drawing units), rotation (degrees)."""

    text: str
    x0: float
    x1: float
    yc: float
    h: float
    rot: float = 0.0
    origin: str = ""          # route locator (DXF handle, PDF span, OCR box)
    source: str = "NATIVE"    # NATIVE (text layer / MTEXT) | CORPUS_OCR | LOCAL_OCR

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Path:
    """A drawn path: ``pts`` are its data-bearing vertices (polyline vertices, Bezier joints), ``dense`` the flattened
    shape for drawing; colour 0xRRGGBB or None (black/unknown); line width in drawing units."""

    pts: np.ndarray
    color: int | None
    lw: float
    kind: str                         # LINE / POLY / BEZ / ARC / FILL
    closed: bool = False
    dashed: bool = False
    dense: np.ndarray | None = None
    origin: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        lo = self.pts.min(0)
        hi = self.pts.max(0)
        return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])


# ------------------------------------------------------------------------------------------------ label parsing
_NUM = re.compile(r"^\s*([-−–+]?)\s*(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](\d+))?\s*(%?)\s*$")
# a spreadsheet's scientific notation: '5E-18', '-5E-19' (zero with a rounding residue), '1E+05', '2,5E-3'
_SCI = re.compile(r"^\s*([-−–+]?)\s*(\d+(?:[.,]\d+)?)[Ee]([-−–+]?)(\d{1,3})\s*$")
_DATE = re.compile(r"^\s*(\d{1,2})[./](\d{1,2})[./](\d{2}|\d{4})\s*(?:г\.?)?\s*$")
_MONTH_YEAR = re.compile(r"^\s*(\d{1,2})[./](\d{4})\s*$")


def parse_number(s: str) -> float | None:
    """'−1 150', '–60', '0,5', '1.25', '12%', '5E-18' → float; anything else → None. A plain space separates
    thousands only after a 1–2 digit group ('1 150', '12 500'): '187 188 189' is a row of labels merged by an import,
    not a number (a thin or no-break space is always a thousands separator). Scientific notation is a number
    (fd-0.1.5): a tick label «-5E-19» is the zero of an axis, never an axis title."""
    m = _SCI.match(s)
    if m:
        sign, mant, esign, exp = m.groups()
        v = float(mant.replace(",", ".")) * 10.0 ** (-int(exp) if esign in ("-", "−", "–") else int(exp))
        return -v if sign in ("-", "−", "–") else v
    m = _NUM.match(s)
    if not m:
        return None
    sign, ip, fp, _ = m.groups()
    groups = re.split(r"[   ]", ip)
    if len(groups) > 1 and " " in ip and len(groups[0]) == 3:
        return None
    v = float(re.sub(r"[   ]", "", ip) + ("." + fp if fp else ""))
    return -v if sign in ("-", "−", "–") else v


def parse_date(s: str) -> dt.date | None:
    """'04.01.2010', '4/1/10', '12.2013' (month.year → the 1st) → date."""
    m = _DATE.match(s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
    else:
        m = _MONTH_YEAR.match(s)
        if not m:
            return None
        d, (mo, y) = 1, (int(g) for g in m.groups())
    if y < 100:
        y += 2000 if y < 50 else 1900
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def decimal_year(d: dt.date) -> float:
    start = dt.date(d.year, 1, 1)
    return d.year + (d - start).days / (dt.date(d.year + 1, 1, 1) - start).days


def from_decimal_year(y: float) -> dt.date:
    yr = int(math.floor(y))
    start = dt.date(yr, 1, 1)
    days = (dt.date(yr + 1, 1, 1) - start).days
    return start + dt.timedelta(days=int(round((y - yr) * days)))


def label_value(text: str) -> tuple[float | None, str | None]:
    """Value and kind of a tick label: NUM (a number, incl. years) or DATE (decimal year)."""
    v = parse_number(text)
    if v is not None:
        return v, "NUM"
    d = parse_date(text)
    if d is not None:
        return decimal_year(d), "DATE"
    return None, None


# ------------------------------------------------------------------------------------------------ colours
def rgb_int(r: float, g: float, b: float) -> int:
    """Components in 0…1 → 0xRRGGBB."""
    return (int(round(r * 255)) << 16) | (int(round(g * 255)) << 8) | int(round(b * 255))


def is_black(c: int | None) -> bool:
    if c is None or c < 0:
        return True
    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
    return max(r, g, b) < 40


def is_greyish(c: int | None) -> bool:
    """Neutral grey/white (grid lines, frames, backgrounds) — black is not greyish."""
    if c is None or c < 0:
        return False
    r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
    return max(r, g, b) - min(r, g, b) < 12 and r >= 40


def color_name(c: int | None) -> str:
    return "black" if c is None or c < 0 else f"#{c:06x}"
