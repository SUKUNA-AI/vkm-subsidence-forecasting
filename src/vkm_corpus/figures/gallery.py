"""Local review gallery of the NAV part ``figure_series`` (agent FD2): one static HTML page that opens from disk (no
server, relative paths) and lists every candidate figure — the original region rendered from the source PDF with the
digitized series drawn over it, a re-plot from the digitized numbers, the calibration summary, the status and flags,
and a CSV of the points. Filters: source, status, time series, text of the caption / axis titles.

The output holds captions and axis titles of the corpus (enough to identify a figure, nothing more): it is work data
for the WORKSTATION (``work/figure_gallery/`` of the PUBLIC checkout, git-ignored) — never commit or publish it.

    python -m vkm_corpus.figures.gallery --nav-dir <figure_series build> --duckdb <canon DuckDB> \\
        [--resources <PRIVATE clone>] --out <work/figure_gallery> [--dpi 110] [--workers 4]

Without ``--resources`` (or ``$VKM_RESOURCES_ROOT``) the crops are left out and only the re-plots are drawn.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from vkm_corpus.navigation.figure_series import RASTER_CONFIG, REGION_MARGIN_PT, SUSPECT_FLAGS

MARGIN_PT = REGION_MARGIN_PT      # the digitizer's region: figure box ± 12 pt
PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2", "#7f7f7f",
           "#bcbd22")


def safe(fid: str) -> str:
    return fid.replace(":", "_")


def thin(pts: list, limit: int = 2000) -> list:
    """Every k-th point for drawing (the CSV keeps all): a few measured curves carry tens of thousands of vertices."""
    if len(pts) <= limit:
        return pts
    step = math.ceil(len(pts) / limit)
    return pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])


def _json(v: Any) -> Any:
    if not v:
        return None
    try:
        return json.loads(v)
    except ValueError:
        return None


def _colour(c: str | None, i: int) -> str:
    """The series colour as drawn; black/grey/unknown series get a palette colour so that they can be told apart."""
    if c and c.startswith("#") and len(c) == 7:
        r, g, b = (int(c[k:k + 2], 16) for k in (1, 3, 5))
        if max(r, g, b) - min(r, g, b) > 40:
            return c
    return PALETTE[i % len(PALETTE)]


# ------------------------------------------------------------------------------------------------ crops (PDF)
def _crop_task(task: dict[str, Any]) -> dict[str, Any]:
    """One source: render the region of every figure (displayed page frame) to PNG."""
    import pymupdf

    out = {"source_id": task["source_id"], "written": [], "errors": {}}
    path = task.get("path")
    if not path or not os.path.isfile(path):
        out["errors"] = {f["stem"]: "SOURCE_UNAVAILABLE" for f in task["figures"]}
        return out
    data = Path(path).read_bytes()
    if task.get("sha256") and hashlib.sha256(data).hexdigest() != task["sha256"]:
        out["errors"] = {f["stem"]: "SOURCE_HASH_MISMATCH" for f in task["figures"]}
        return out
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        for f in task["figures"]:
            try:
                page = doc[f["page_index"] - 1]
                clip = pymupdf.Rect(*f["region"]) & page.rect
                pix = page.get_pixmap(dpi=task["dpi"], clip=clip, alpha=False)
                pix.save(str(Path(task["out"]) / "crops" / f"{f['stem']}.png"))
                out["written"].append({"stem": f["stem"], "px": [pix.width, pix.height],
                                       "clip": [clip.x0, clip.y0, clip.x1, clip.y1]})
            except Exception as exc:  # noqa: BLE001 — a figure without a crop is still listed
                out["errors"][f["stem"]] = f"{type(exc).__name__}"
    finally:
        doc.close()
    return out


# ------------------------------------------------------------------------------------------------ SVG
def overlay_svg(clip: list[float], px: list[int], series: list[dict[str, Any]], plot_box: list[float] | None) -> str:
    """The digitized series in page points over the crop (same frame: the crop's clip rectangle)."""
    x0, y0, x1, y1 = clip
    w, h = x1 - x0, y1 - y0
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.2f} {h:.2f}" width="{px[0]}" '
             f'height="{px[1]}" preserveAspectRatio="none">']
    if plot_box:
        bx0, by0, bx1, by1 = plot_box
        parts.append(f'<rect x="{bx0 - x0:.2f}" y="{by0 - y0:.2f}" width="{bx1 - bx0:.2f}" height="{by1 - by0:.2f}" '
                     'fill="none" stroke="#888" stroke-width="0.6" stroke-dasharray="3 2"/>')
    for k, s in enumerate(series):
        pts = thin([(p["x_page_pt"] - x0, p["y_page_pt"] - y0) for p in s["points"] if p["x_page_pt"] is not None])
        if not pts:
            continue
        col = _colour(s.get("series_color"), k)
        dash = "" if s["axes_calibrated"] == "BOTH" else ' stroke-dasharray="2 2"'
        if s.get("sampling") == "MARKER_CENTRES" or len(pts) < 2:
            parts += [f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.8" fill="none" stroke="{col}" stroke-width="0.9"/>'
                      for x, y in pts]
        else:
            d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
            parts.append(f'<polyline points="{d}" fill="none" stroke="{col}" stroke-width="1.4" '
                         f'stroke-opacity="0.85"{dash}/>')
            if len(pts) <= 60:
                parts += [f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.2" fill="{col}"/>' for x, y in pts]
    parts.append("</svg>")
    return "".join(parts)


def _axis_map(cal: dict | None, lo: float, hi: float, length: float, orient: str):
    """value → pixel along an axis of ``length``: log scale for LOG10, the printed direction kept (x grows to the
    right, y grows upward unless the chart was drawn inverted)."""
    kind = (cal or {}).get("kind")
    b = (cal or {}).get("b")
    if b is None and cal and cal.get("labels"):
        labs = sorted(cal["labels"], key=lambda r: r["pos"])
        b = (labs[-1]["value"] - labs[0]["value"]) if len(labs) > 1 else 1.0
    inverted = (b is not None) and ((b < 0) if orient == "x" else (b > 0))
    log = kind == "LOG10" and lo > 0

    def t(v):
        return math.log10(v) if log else v
    a0, a1 = t(lo), t(hi)
    span = (a1 - a0) or 1.0

    def f(v):
        if v is None or (log and v <= 0):
            return None
        r = (t(v) - a0) / span
        if orient == "y":
            r = 1.0 - r
        return (1.0 - r if inverted else r) * length
    return f


def _fmt(v: float) -> str:
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e5 or a < 1e-3:
        return f"{v:.2e}"
    return f"{v:.4g}"


def _range(vals: list[float], labels: list[float], extra: list[float], log: bool) -> tuple[float, float]:
    """Plot range: the tick values and the in-plot values, widened by out-of-plot values up to one tick span."""
    base = [v for v in labels + vals if v is not None and (not log or v > 0)]
    if not base:
        base = [v for v in extra if v is not None and (not log or v > 0)] or [0.0, 1.0]
    lo, hi = min(base), max(base)
    t = (lambda v: math.log10(v)) if log else (lambda v: v)
    span = (t(hi) - t(lo)) or 1.0
    for v in extra:
        if v is None or (log and v <= 0):
            continue
        if t(lo) - span <= t(v) < t(lo):
            lo = v
        elif t(hi) < t(v) <= t(hi) + span:
            hi = v
    return lo, hi


def replot_svg(fig: dict[str, Any], series: list[dict[str, Any]], W: int = 520, H: int = 360) -> str:
    """A plot drawn from the digitized numbers only: the printed tick values, the axis titles as printed, the series
    with both axes calibrated (markers for marker series). Points outside the plot area (``in_plot_area`` false:
    hidden by the chart's clip or taken from a neighbouring chart) are drawn faint and dashed, within one tick span
    of the axis range."""
    both = [s for s in series if s["axes_calibrated"] == "BOTH" and s["points"]]
    esc = html.escape
    if not both:
        msg = {"AXES_OK_NO_SERIES": "оси откалиброваны, рядов не найдено",
               "X_UNCALIBRATED": "ось X не откалибрована — значений X нет",
               "Y_UNCALIBRATED": "ось Y не откалибрована — значений Y нет",
               "NO_AXES": "оси не откалиброваны — значений нет"}.get(fig.get("figure_status"),
                                                                        fig.get("figure_status") or "нет рядов")
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
                f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" fill="none" stroke="#bbb"/>'
                f'<text x="{W / 2}" y="{H / 2}" text-anchor="middle" font-family="sans-serif" font-size="13" '
                f'fill="#777">{esc(msg)}</text></svg>')
    cal = _json(both[0].get("calibration")) or {}
    cx, cy = cal.get("x") or {}, cal.get("y") or {}
    pin = [p for s in both for p in s["points"] if p.get("in_plot_area") is not False]
    pout = [p for s in both for p in s["points"] if p.get("in_plot_area") is False]
    x_lo, x_hi = _range([p["x"] for p in pin], [lab.get("value") for lab in cx.get("labels") or []],
                        [p["x"] for p in pout], cx.get("kind") == "LOG10")
    y_lo, y_hi = _range([p["y"] for p in pin], [lab.get("value") for lab in cy.get("labels") or []],
                        [p["y"] for p in pout], cy.get("kind") == "LOG10")
    L, R, T, B = 64, 14, 12, 44
    pw, ph = W - L - R, H - T - B
    fx = _axis_map(cx, x_lo, x_hi, pw, "x")
    fy = _axis_map(cy, y_lo, y_hi, ph, "y")
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
         'font-family="sans-serif" font-size="10">',
         f'<rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="#fff" stroke="#444" stroke-width="0.8"/>']
    for lab in cx.get("labels") or []:
        px = fx(lab.get("value"))
        if px is None:
            continue
        X = L + px
        o.append(f'<line x1="{X:.1f}" y1="{T}" x2="{X:.1f}" y2="{T + ph}" stroke="#e3e3e3" stroke-width="0.6"/>')
        o.append(f'<text x="{X:.1f}" y="{T + ph + 13}" text-anchor="middle" fill="#333">{esc(str(lab["text"]))}</text>')
    for lab in cy.get("labels") or []:
        py = fy(lab.get("value"))
        if py is None:
            continue
        Y = T + py
        o.append(f'<line x1="{L}" y1="{Y:.1f}" x2="{L + pw}" y2="{Y:.1f}" stroke="#e3e3e3" stroke-width="0.6"/>')
        o.append(f'<text x="{L - 4}" y="{Y + 3:.1f}" text-anchor="end" fill="#333">{esc(str(lab["text"]))}</text>')
    xt, yt = fig.get("x_title_raw"), fig.get("y_title_raw")
    if xt:
        o.append(f'<text x="{L + pw / 2}" y="{H - 8}" text-anchor="middle" fill="#111">{esc(xt[:80])}</text>')
    if yt:
        o.append(f'<text transform="translate(12,{T + ph / 2}) rotate(-90)" text-anchor="middle" fill="#111">'
                 f'{esc(yt[:60])}</text>')
    o.append(f'<clipPath id="c"><rect x="{L}" y="{T}" width="{pw}" height="{ph}"/></clipPath><g clip-path="url(#c)">')
    for k, s in enumerate(both):
        col = _colour(s.get("series_color"), k)
        pts = [(fx(p["x"]), fy(p["y"]), p.get("in_plot_area") is not False) for p in thin(s["points"])]
        pts = [(L + a, T + b, ok) for a, b, ok in pts if a is not None and b is not None]
        markers = s.get("sampling") in ("MARKER_CENTRES", "AT_SERIES_MARKERS", "AT_AXIS_MARKERS") or len(pts) < 2
        if markers:
            o += [f'<circle cx="{a:.1f}" cy="{b:.1f}" r="2.2" fill="none" stroke="{col}" stroke-width="1.1"'
                  + ('' if ok else ' stroke-opacity="0.35" stroke-dasharray="1.5 1.5"') + '/>' for a, b, ok in pts]
            continue
        if not all(ok for _, _, ok in pts):          # the whole line faint, then the in-plot runs over it
            o.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b, _ in pts) +
                     f'" fill="none" stroke="{col}" stroke-width="1.1" stroke-opacity="0.35" '
                     'stroke-dasharray="3 2"/>')
        run: list[tuple[float, float]] = []
        for a, b, ok in pts + [(0.0, 0.0, False)]:
            if ok:
                run.append((a, b))
                continue
            if len(run) >= 2:
                o.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in run) +
                         f'" fill="none" stroke="{col}" stroke-width="1.3"/>')
            run = []
    o.append("</g>")
    labelled = [(k, s) for k, s in enumerate(both) if s.get("series_label_raw")][:8]
    for j, (k, s) in enumerate(labelled):
        yy = T + 10 + 12 * j
        o.append(f'<line x1="{L + pw - 90}" y1="{yy - 3}" x2="{L + pw - 76}" y2="{yy - 3}" '
                 f'stroke="{_colour(s.get("series_color"), k)}" stroke-width="2"/>')
        o.append(f'<text x="{L + pw - 72}" y="{yy}" fill="#111">{esc(str(s["series_label_raw"])[:14])}</text>')
    o.append("</svg>")
    return "".join(o)


# ------------------------------------------------------------------------------------------------ CSV
CSV_FIELDS = ("figure_id", "series_id", "series_index", "series_label_raw", "series_color", "axes_calibrated", "i",
              "x", "x_date", "y", "x_err", "y_err", "x_unit_raw", "y_unit_raw", "x_page_pt", "y_page_pt",
              "in_plot_area", "status", "review_status", "available_from")


_CSV_FMT = {"x": "{:.8g}", "y": "{:.8g}", "x_err": "{:.3g}", "y_err": "{:.3g}", "x_page_pt": "{:.3f}",
            "y_page_pt": "{:.3f}"}


def write_csv(path: Path, fig: dict[str, Any], series: list[dict[str, Any]]) -> None:
    """All points of a figure (every series), one row each, with the status columns (DERIVATION,
    AUTO_EXTRACTED_UNREVIEWED) and the availability date; UTF-8 with BOM for spreadsheet programs. Values are written
    with 8 significant digits (errors 3) — the datasets keep full precision."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        for s in series:
            for p in s["points"]:
                row = {**s, **p, "figure_id": fig["figure_id"], "x_unit_raw": fig.get("x_unit_raw"),
                       "y_unit_raw": fig.get("y_unit_raw"), "available_from": fig.get("available_from") or "UNKNOWN"}
                w.writerow(["" if row.get(k) is None else _CSV_FMT[k].format(row[k]) if k in _CSV_FMT
                            else row.get(k) for k in CSV_FIELDS])


# ------------------------------------------------------------------------------------------------ page
def _summary_axis(f: dict[str, Any], a: str) -> dict[str, Any]:
    return {k: f.get(f"{a}_{k}") for k in ("title_raw", "unit_raw", "axis_kind", "cal_method", "cal_label_source",
                                           "cal_n_labels", "cal_rms_pt")}


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Оцифрованные графики</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mute:#5e6675;--line:#dde1e7;--ok:#1a7f37;--warn:#9a6700;--bad:#b42318;
--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1d2128;--ink:#e6e8eb;--mute:#9aa3b1;--line:#30363f;
--ok:#4ac26b;--warn:#d4a72c;--bad:#f47067;--chip:#262b33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:5}
h1{font-size:18px;margin:0 0 4px}.note{color:var(--mute);font-size:12.5px;margin:0}
.filters{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px;align-items:center}
select,input[type=search]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--ink)}input[type=search]{min-width:240px}
.count{color:var(--mute);margin-left:auto}main{padding:16px 20px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
content-visibility:auto;contain-intrinsic-size:auto 520px}
.head{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}.id{font-family:ui-monospace,monospace;font-size:12.5px}
.badge{font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;background:var(--chip)}
.s-DIGITIZED{color:var(--ok)}.s-X_UNCALIBRATED,.s-Y_UNCALIBRATED,.s-AXES_OK_NO_SERIES{color:var(--warn)}
.s-NO_AXES,.s-ERROR,.s-SOURCE_UNAVAILABLE,.s-SOURCE_HASH_MISMATCH{color:var(--bad)}
.flag{font-size:11px;padding:1px 6px;border-radius:4px;background:var(--chip);color:var(--mute)}
.flag.bad{color:var(--bad);font-weight:600}
.cap{margin:6px 0 8px;color:var(--ink)}.pics{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start}
.pic{position:relative;display:inline-block;max-width:100%}.pic img{display:block;max-width:100%;max-height:460px;
width:auto;height:auto;border:1px solid var(--line);background:#fff}
.pic .ov{position:absolute;inset:0;width:100%;height:100%;max-height:none;border:0;background:none}
.hide-ov .ov{display:none}.cal{font-size:12.5px;color:var(--mute);margin-top:8px;display:grid;gap:2px}
.cal b{color:var(--ink);font-weight:600}a{color:#2f6fdb}.tools{display:flex;gap:12px;font-size:12.5px;margin-top:6px}
.more{text-align:center;margin:10px}button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--line);
background:var(--card);color:var(--ink);cursor:pointer}
</style></head><body>
<header><h1>Оцифрованные графики корпуса ВКМ — NAV figure_series</h1>
<p class="note" id="meta"></p>
<p class="note">Все значения — DERIVATION из публикации, AUTO_EXTRACTED_UNREVIEWED: не наблюдения и не evidence;
модельные кривые и измерения не различаются; участок (рудник) — только как у источника. Слева — область рисунка из PDF
с наложенными оцифрованными рядами (пунктир — ряд без калибровки обеих осей), справа — график, построенный заново по
числам. Рабочие данные: не коммитить и не публиковать.</p>
<div class="filters">
<select id="f-route"><option value="">все маршруты</option></select>
<select id="f-src"><option value="">все источники</option></select>
<select id="f-st"><option value="">все статусы</option></select>
<label><input type="checkbox" id="f-time"> только ряды во времени</label>
<label><input type="checkbox" id="f-ser"> только с оцифрованными рядами</label>
<label><input type="checkbox" id="f-clean"> без подозрительной калибровки</label>
<label><input type="checkbox" id="f-ov" checked> наложение</label>
<input type="search" id="f-q" placeholder="слова подписи или названий осей">
<span class="count" id="count"></span></div></header>
<main id="list"></main><div class="more"><button id="more" hidden>показать ещё</button></div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D=JSON.parse(document.getElementById('data').textContent),F=D.figures;
document.getElementById('meta').textContent=D.meta;
const sel=(id,vals)=>{const s=document.getElementById(id);vals.forEach(v=>{const o=document.createElement('option');
o.value=v[0];o.textContent=v[1];s.appendChild(o)})};
sel('f-src',D.sources.map(s=>[s[0],s[0]+' ('+s[1]+')']));sel('f-st',D.statuses.map(s=>[s[0],s[0]+' ('+s[1]+')']));
const RN={A:'A — векторы PDF',R:'R — растр (проверять)'};sel('f-route',D.routes.map(s=>[s[0],(RN[s[0]]||s[0])+' ('+s[1]+')']));
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const ax=(n,a)=>a.axis_kind?`<b>${n}</b>: ${esc(a.title_raw||'—')} · ${a.axis_kind} · ${esc(a.cal_method)} · меток ${a.cal_n_labels} · rms ${a.cal_rms_pt==null?'—':a.cal_rms_pt.toFixed(3)} pt${a.cal_label_source==='LOCAL_OCR'?' · OCR':''}`:`<b>${n}</b>: не откалибрована`;
const SUS=new Set(D.suspect||[]);
function card(f){const flags=f.flags.map(x=>`<span class="flag${SUS.has(x)?' bad':''}">${x}</span>`).join(' ');
const crop=f.crop?`<div class="pic"><img loading="lazy" src="${f.crop}" alt="crop">${f.overlay?`<img class="ov" loading="lazy" src="${f.overlay}" alt="">`:''}</div>`:'<div class="pic note">нет изображения</div>';
return `<article class="card"><div class="head"><span class="badge s-${f.status}">${f.status}</span>${f.route==='R'?'<span class="badge s-X_UNCALIBRATED">RASTER</span>':''}
<span class="id">${esc(f.id)}</span><span>${esc(f.label||'')}</span><span class="note">${esc(f.source)}, с. ${f.page}${f.rot?' (повёрнута)':''}</span> ${flags}</div>
<div class="cap">${esc(f.caption||'(подписи нет)')}</div><div class="pics">${crop}<div class="pic"><img loading="lazy" src="${f.replot}" alt="re-plot"></div></div>
<div class="cal"><div>${ax('X',f.x)}${f.x_time?' · <b>время</b>':''}</div><div>${ax('Y',f.y)}</div>
<div>рядов ${f.n_series} (обе оси ${f.n_both}, во времени ${f.n_time}) · точек ${f.n_points} (обе оси ${f.n_points_both}${f.n_outside?`, вне области графика ${f.n_outside}`:''}) · доступно с ${esc(f.available_from||'UNKNOWN')} · ${esc(f.scope||'')}</div></div>
<div class="tools">${f.csv?`<a href="${f.csv}">точки (CSV)</a>`:''}${f.crop?`<a href="${f.crop}">исходная область</a>`:''}<a href="${f.replot}">график по числам</a>${f.error?`<span class="note">${esc(f.error)}</span>`:''}</div></article>`}
let shown=0,cur=[];const PAGE=60,list=document.getElementById('list'),more=document.getElementById('more');
function apply(){const s=document.getElementById('f-src').value,st=document.getElementById('f-st').value,
r=document.getElementById('f-route').value,
t=document.getElementById('f-time').checked,se=document.getElementById('f-ser').checked,
cl=document.getElementById('f-clean').checked,q=document.getElementById('f-q').value.trim().toLowerCase();
cur=F.filter(f=>(!r||f.route===r)&&(!s||f.source===s)&&(!st||f.status===st)&&(!t||f.n_time>0)&&(!se||f.n_both>0)&&
(!cl||!f.flags.some(x=>SUS.has(x)))&&(!q||(f.search.includes(q))));list.innerHTML='';shown=0;page();
document.getElementById('count').textContent=cur.length+' из '+F.length}
function page(){const part=cur.slice(shown,shown+PAGE);list.insertAdjacentHTML('beforeend',part.map(card).join(''));
shown+=part.length;more.hidden=shown>=cur.length}
more.onclick=page;['f-route','f-src','f-st','f-time','f-ser','f-clean'].forEach(id=>document.getElementById(id).onchange=apply);
document.getElementById('f-q').oninput=apply;
document.getElementById('f-ov').onchange=e=>document.body.classList.toggle('hide-ov',!e.target.checked);apply();
</script></body></html>
"""


ROUTES = (("A", ("figure_series_figures", "figure_series", "figure_series_points"), MARGIN_PT, ""),
          ("R", ("figure_series_raster_figures", "figure_series_raster", "figure_series_raster_points"),
           RASTER_CONFIG["region_margin_pt"], "_R"))


def _load_route(nav: Path, names: tuple[str, str, str]):
    import pyarrow.parquet as pq

    if not all((nav / f"{n}.parquet").is_file() for n in names):
        return None
    figs = pq.read_table(nav / f"{names[0]}.parquet").to_pylist()
    series = pq.read_table(nav / f"{names[1]}.parquet").to_pylist()
    by_series: dict[str, list[dict[str, Any]]] = {}
    for p in pq.read_table(nav / f"{names[2]}.parquet").to_pylist():
        by_series.setdefault(p["series_id"], []).append(p)
    by_fig: dict[str, list[dict[str, Any]]] = {}
    for s in sorted(series, key=lambda s: (s["figure_id"], s["series_index"])):
        s["points"] = sorted(by_series.get(s["series_id"], []), key=lambda p: p["i"])
        by_fig.setdefault(s["figure_id"], []).append(s)
    return figs, by_fig


def build_gallery(nav_dir: str | Path, duckdb_path: str | Path, out_dir: str | Path, *, resources: str | None = None,
                  dpi: int = 110, workers: int = 4) -> dict[str, Any]:
    """Route A (and the flagged raster dataset of route R when the build has it) → ``<out>/index.html`` with
    ``crops/``, ``overlay/``, ``replot/`` and ``csv/`` beside it (relative links)."""
    import duckdb

    nav, out = Path(nav_dir), Path(out_dir)
    for sub in ("crops", "overlay", "replot", "csv"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    manifest = json.loads((nav / "manifest.json").read_text(encoding="utf-8")) if (nav / "manifest.json").is_file() \
        else {}
    rows = []                                   # (route, stem, margin, figure row, its series)
    for route, names, margin, suffix in ROUTES:
        loaded = _load_route(nav, names)
        if loaded is None:
            continue
        figs, by_fig = loaded
        rows += [(route, safe(f["figure_id"]) + suffix, margin, f, by_fig.get(f["figure_id"], [])) for f in figs]
    con = duckdb.connect(str(duckdb_path), read_only=True)
    try:
        caps = dict(con.execute("SELECT object_id, coalesce(caption_normalized, caption) FROM canonical.figures "
                                "WHERE object_id IN (SELECT unnest(?))", [sorted({r[3]["figure_id"] for r in rows})]
                                ).fetchall())
        srcs = {r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT source_id, canonical_path, source_sha256 FROM canonical.sources").fetchall()}
    finally:
        con.close()
    crops: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    root = resources or os.environ.get("VKM_RESOURCES_ROOT")
    if root and Path(root).is_dir():
        tasks: dict[str, dict[str, Any]] = {}
        for route, stem, margin, f, _ser in rows:
            rel, sha = srcs.get(f["source_id"], (None, None))
            t = tasks.setdefault(f["source_id"], {"source_id": f["source_id"], "path": str(Path(root) / rel) if rel
                                                  else None, "sha256": sha, "dpi": dpi, "out": str(out),
                                                  "figures": []})
            t["figures"].append({"stem": stem, "page_index": f["page_index"],
                                 "region": [f["bbox_x0"] - margin, f["bbox_y0"] - margin,
                                            f["bbox_x1"] + margin, f["bbox_y1"] + margin]})
        todo = sorted(tasks.values(), key=lambda t: (-len(t["figures"]), t["source_id"]))
        if workers > 1 and len(todo) > 1:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as ex:
                results = list(ex.map(_crop_task, todo))
        else:
            results = [_crop_task(t) for t in todo]
        for r in results:
            crops.update({w["stem"]: w for w in r["written"]})
            errors.update(r["errors"])
    items = []
    rank = {s: i for i, s in enumerate(("DIGITIZED", "X_UNCALIBRATED", "Y_UNCALIBRATED", "AXES_OK_NO_SERIES",
                                        "NO_AXES"))}
    for route, stem, _margin, f, ser in sorted(rows, key=lambda r: (r[0], rank.get(r[3]["figure_status"], 9),
                                                                     r[3]["source_id"], r[3]["page_index"],
                                                                     r[3]["figure_id"])):
        fid = f["figure_id"]
        (out / "replot" / f"{stem}.svg").write_text(replot_svg(f, ser), encoding="utf-8")
        item_csv = None
        if ser:
            write_csv(out / "csv" / f"{stem}.csv", f, ser)
            item_csv = f"csv/{stem}.csv"
        crop = crops.get(stem)
        overlay = None
        if crop and ser:
            (out / "overlay" / f"{stem}.svg").write_text(overlay_svg(crop["clip"], crop["px"], ser,
                                                                     f.get("plot_box")), encoding="utf-8")
            overlay = f"overlay/{stem}.svg"
        cap = caps.get(fid)
        items.append({
            "id": fid, "route": route, "source": f["source_id"], "page": f["page_index"],
            "label": f.get("figure_label"), "caption": (cap[:220] + "…") if cap and len(cap) > 220 else cap,
            "status": f["figure_status"],
            "flags": [x for x in f["flags"] if x != "SCOPE_INHERITED_FROM_SOURCE"], "rot": bool(f["page_rotation"]),
            "x": _summary_axis(f, "x"), "y": _summary_axis(f, "y"), "x_time": bool(f.get("x_is_time")),
            "n_series": f["n_series"], "n_both": f["n_series_both_axes"], "n_time": f["n_time_series"],
            "n_points": f["n_points"], "n_points_both": f["n_points_both_axes"],
            "n_outside": f.get("n_points_outside_plot") or 0,
            "available_from": f.get("available_from"), "scope": f.get("source_site_scope_raw"),
            "error": f.get("error") or errors.get(stem), "crop": f"crops/{stem}.png" if crop else None,
            "overlay": overlay, "replot": f"replot/{stem}.svg", "csv": item_csv,
            "search": " ".join(x for x in (cap, f.get("figure_label"), f.get("x_title_raw"), f.get("y_title_raw"),
                                           " ".join(s.get("series_label_raw") or "" for s in ser)) if x).lower(),
        })
    part = (manifest.get("parts") or {}).get("figure_series") or {}
    snap = (manifest.get("snapshot") or {}).get("snapshot_id")
    meta = f"Снимок {snap} · правило {part.get('rule_version')}"
    for route in ("A", "R"):
        sub = [r for r in rows if r[0] == route]
        if not sub:
            continue
        ser_all = [s for r in sub for s in r[4]]
        st = Counter(r[3]["figure_status"] for r in sub)
        meta += (f" · маршрут {route}: рисунков {len(sub)}, рядов {len(ser_all)} (обе оси "
                 f"{sum(1 for s in ser_all if s['axes_calibrated'] == 'BOTH')}), точек "
                 f"{sum(len(s['points']) for s in ser_all)}; " + ", ".join(f"{k} {v}" for k, v in sorted(st.items())))
    data = {"meta": meta, "figures": items,
            "sources": sorted(Counter(r[3]["source_id"] for r in rows).items()),
            "statuses": sorted(Counter(r[3]["figure_status"] for r in rows).items()),
            "routes": sorted(Counter(r[0] for r in rows).items()),
            "suspect": sorted(SUSPECT_FLAGS)}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    (out / "index.html").write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")
    return {"figures": len(rows), "routes": dict(sorted(Counter(r[0] for r in rows).items())), "crops": len(crops),
            "crop_errors": len(errors), "csv": sum(1 for i in items if i["csv"]),
            "overlays": sum(1 for i in items if i["overlay"]), "out": out.name}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m vkm_corpus.figures.gallery",
                                description="local HTML gallery of the NAV part figure_series (work data)")
    p.add_argument("--nav-dir", required=True, help="directory with figure_series*.parquet (+ manifest.json)")
    p.add_argument("--duckdb", required=True, help="canonical DuckDB of the snapshot (captions, source files)")
    p.add_argument("--out", required=True, help="output directory, e.g. work/figure_gallery of the PUBLIC checkout")
    p.add_argument("--resources", default=None, help="PRIVATE clone for the crops (default $VKM_RESOURCES_ROOT)")
    p.add_argument("--dpi", type=int, default=110)
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args(argv)
    print(json.dumps(build_gallery(a.nav_dir, a.duckdb, a.out, resources=a.resources, dpi=a.dpi, workers=a.workers),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
