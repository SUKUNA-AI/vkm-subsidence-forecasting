"""Toy data for the CAD bridge tests (synthetic only): a 10 × 10 point grid with a synthetic trough, and a minimal
vector PDF written by hand (no PDF library, no corpus content)."""
from __future__ import annotations

import math


def grid(trough_depth: float, *, n: int = 10, step: float = 10.0, center: tuple[float, float] = (45.0, 45.0),
         sigma: float = 20.0, slope: float = 0.01, base: float = 100.0, prefix: str = "T") -> list[dict]:
    """``z = base + slope·x − depth·exp(−r²/2σ²)`` on an n × n grid (a Gaussian trough; synthetic, not a model)."""
    rows = []
    for i in range(n):
        for j in range(n):
            x, y = i * step, j * step
            r2 = (x - center[0]) ** 2 + (y - center[1]) ** 2
            z = base + slope * x - trough_depth * math.exp(-r2 / (2 * sigma ** 2))
            rows.append({"name": f"{prefix}{i}{j}", "x": x, "y": y, "z": round(z, 4), "desc": "toy"})
    return rows


def synthetic_pdf() -> bytes:
    """One A4 page (595 × 842 pt): a frame, a diagonal, a Bézier curve and a short Helvetica text."""
    content = (b"0.8 w 50 50 m 545 50 l 545 792 l 50 792 l h S "
               b"100 100 m 400 600 l S 300 300 m 350 380 420 220 480 300 c S "
               b"BT /F1 24 Tf 100 700 Td (VKM TEST) Tj ET")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out
