"""Page classification and route (rule of the phase-0 design, agent C §2.1 / §5.3).

Thresholds are parameters of ``CLASSIFIER_VERSION`` (they enter the stage config hash), not scattered constants.

Page classes: NATIVE_TEXT, MIXED, VECTOR, RASTER_SCAN, BROKEN_TEXT_LAYER, EMPTY (+ flags). Routes:

* ``NATIVE`` – native text (OCR only for formula/table regions);
* ``NATIVE_REPAIR`` – deterministic CP1251 remap of a mojibake layer (checked for plausibility);
* ``OCR_OPTIONAL`` – raster scan with a foreign OCR layer: the layer is ``EMBEDDED_OCR`` (H-02), never NATIVE;
  GLM-OCR only for sampled pages (scenario B), re-OCR-ed sources and formula/table regions;
* ``OCR_REQUIRED`` – scan without text, broken layer, outline glyphs, image without text, DjVu without a layer;
* ``EMPTY`` – no text, no images, few paths (an ink check on the render may still send it to OCR).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

CLASSIFIER_VERSION = "c1"


@dataclass(frozen=True)
class Thresholds:
    min_text: int = 20            # non-whitespace glyphs of a usable text layer
    scan_cov: float = 0.85        # union image coverage of a full-page raster
    raster_cov: float = 0.50      # raster-dominated page without text
    mixed_cov: float = 0.15       # native text + raster figures
    image_min_cov: float = 0.05   # an image that may hold text
    vec_paths: int = 500          # paths of a vector-graphics page
    bad_ratio: float = 0.10       # control/U+FFFD (or PUA) share
    latext_max: float = 0.30      # Latin-1/extended share (mojibake signal)
    latext_probe: float = 0.20    # share above which the CP1251 repair is probed
    repair_min_cyr: float = 0.50
    repair_min_func: float = 0.05
    func_min: float = 0.02        # soft LOW_FUNCTION_WORD_RATE flag (>= 40 words)
    invisible_share: float = 0.80 # hidden text layer (render mode 3 / opacity 0)
    empty_ink: float = 0.004      # ink share on the render that turns EMPTY into OCR_REQUIRED

    def as_config(self) -> dict[str, Any]:
        return {"classifier_version": CLASSIFIER_VERSION, **asdict(self)}


DEFAULT_THRESHOLDS = Thresholds()


def classify_pdf_page(f: dict[str, Any], t: Thresholds = DEFAULT_THRESHOLDS) -> tuple[str, list[str]]:
    """Page class and flags from the feature dict of ``pdf_native.page_features``."""
    T = int(f.get("chars") or 0)
    flags: list[str] = []
    cov = float(f.get("img_cov") or 0.0)
    npaths = int(f.get("n_paths") or 0)
    nimg = int(f.get("n_images") or 0)
    if npaths < 0:
        flags.append("PATHS_UNKNOWN")
        npaths = 0
    if int(f.get("n_fonts_nonstd_no_tounicode") or 0) > 0:
        flags.append("FONT_WITHOUT_TOUNICODE")
    if int(f.get("n_type3_fonts") or 0) > 0:
        flags.append("TYPE3_FONT")
    if T < t.min_text:
        if cov >= t.raster_cov:
            return "RASTER_SCAN", flags + ["NO_TEXT_LAYER"]
        if npaths >= t.vec_paths:
            return "VECTOR", flags + ["VECTOR_NO_TEXT"]
        if nimg > 0 and cov >= t.image_min_cov:
            return "MIXED", flags + ["IMAGE_NO_TEXT"]
        return "EMPTY", flags
    words = int(f.get("word_tokens") or 0)
    func = (int(f.get("func_hits") or 0) / words) if words else 0.0
    letters = sum(int(f.get(k) or 0) for k in ("cyr", "lat", "greek"))
    hard_bad = int(f.get("ctrl") or 0) + int(f.get("repl") or 0)
    pua = int(f.get("pua") or 0)
    latext = int(f.get("latext") or 0)
    if hard_bad / T > t.bad_ratio:
        return "BROKEN_TEXT_LAYER", flags + ["GARBAGE_GLYPHS"]
    if latext / T > t.latext_max:
        ok = (float(f.get("remap_cyr_share") or 0) >= t.repair_min_cyr
              and (int(f.get("remap_words") or 0) < 40 or float(f.get("remap_func_rate") or 0) >= t.repair_min_func))
        if not ok:
            return "BROKEN_TEXT_LAYER", flags + ["MOJIBAKE_NOT_REPAIRABLE"]
        flags.append("REPAIRABLE_CP1251_REMAP")
    elif pua / T > t.bad_ratio:
        healthy = letters / T >= 0.5 and (words < 40 or func >= t.repair_min_func)
        if not healthy:
            return "BROKEN_TEXT_LAYER", flags + ["PUA_GLYPHS"]
        flags.append("UNMAPPED_SYMBOL_GLYPHS")
    else:
        good = sum(int(f.get(k) or 0) for k in ("cyr", "lat", "greek", "digits", "punct")) + pua
        if good / T < 0.5:
            return "BROKEN_TEXT_LAYER", flags + ["GARBAGE_GLYPHS"]
        if words >= 40 and func < t.func_min:
            flags.append("LOW_FUNCTION_WORD_RATE")
    inv = int(f.get("chars_invisible") or 0) / T
    if cov >= t.scan_cov:
        flags.append("HIDDEN_TEXT_LAYER" if inv >= t.invisible_share else "VISIBLE_TEXT_OVER_IMAGE")
        return "RASTER_SCAN", flags + ["EMBEDDED_TEXT_LAYER"]
    if cov >= t.mixed_cov:
        return "MIXED", flags
    if npaths >= t.vec_paths:
        return "VECTOR", flags
    return "NATIVE_TEXT", flags


def classify_text_layer(text: str, t: Thresholds = DEFAULT_THRESHOLDS) -> tuple[str, list[str]]:
    """Quality of an embedded layer given as plain text (DjVu): OK / ABSENT / BROKEN reason."""
    from vkm_corpus.extract.text_repair import text_char_stats, word_stats

    st = text_char_stats(text)
    T = st["chars"]
    if T < t.min_text:
        return "ABSENT", ["NO_TEXT_LAYER"]
    if (st["ctrl"] + st["repl"]) / T > t.bad_ratio:
        return "BROKEN", ["GARBAGE_GLYPHS"]
    if st["latext"] / T > t.latext_max:
        return "BROKEN", ["MOJIBAKE_NOT_REPAIRABLE"]
    good = st["cyr"] + st["lat"] + st["greek"] + st["digits"] + st["punct"] + st["pua"]
    if good / T < 0.5:
        return "BROKEN", ["GARBAGE_GLYPHS"]
    words, func = word_stats(text)
    flags = []
    if words >= 40 and func / words < t.func_min:
        flags.append("LOW_FUNCTION_WORD_RATE")
    return "OK", flags


def classify_djvu_page(has_text_chunk: bool, layer_text: str | None,
                       t: Thresholds = DEFAULT_THRESHOLDS) -> tuple[str, list[str]]:
    if not has_text_chunk or layer_text is None:
        return "RASTER_SCAN", ["DJVU", "NO_TEXT_LAYER"]
    quality, flags = classify_text_layer(layer_text, t)
    if quality == "ABSENT":
        return "RASTER_SCAN", ["DJVU", "NO_TEXT_LAYER", "EMPTY_TEXT_CHUNK"]
    if quality == "BROKEN":
        return "BROKEN_TEXT_LAYER", ["DJVU", "EMBEDDED_TEXT_LAYER", *flags]
    return "RASTER_SCAN", ["DJVU", "EMBEDDED_TEXT_LAYER", "HIDDEN_TEXT_LAYER", *flags]


def route_for(page_class: str, flags: list[str]) -> tuple[str, str]:
    """(route, route_reason)."""
    if page_class == "EMPTY":
        return "EMPTY", "NO_TEXT_NO_IMAGES"
    if page_class == "RASTER_SCAN":
        if "EMBEDDED_TEXT_LAYER" in flags:
            return "OCR_OPTIONAL", "EMBEDDED_OCR_LAYER"
        return "OCR_REQUIRED", "SCAN_NO_TEXT_LAYER"
    if page_class == "BROKEN_TEXT_LAYER":
        reason = next((f for f in flags if f in ("GARBAGE_GLYPHS", "MOJIBAKE_NOT_REPAIRABLE", "PUA_GLYPHS")),
                      "BROKEN")
        return "OCR_REQUIRED", "BROKEN_" + reason
    if page_class == "VECTOR" and "VECTOR_NO_TEXT" in flags:
        return "OCR_REQUIRED", "VECTOR_NO_TEXT"
    if page_class == "MIXED" and "IMAGE_NO_TEXT" in flags:
        return "OCR_REQUIRED", "IMAGE_NO_TEXT"
    if "REPAIRABLE_CP1251_REMAP" in flags:
        return "NATIVE_REPAIR", "CP1251_REMAP"
    return "NATIVE", page_class


def document_class(page_classes: list[tuple[str, list[str]]], file_format: str) -> str:
    """DocumentClass of a source from its page classes (phase-0 source-class rule)."""
    if file_format == "EPUB":
        return "REFLOWABLE_EPUB"
    if file_format == "DOCX":
        return "WORD_DOCX"
    core = [(c, f) for c, f in page_classes if c != "EMPTY"]
    if not core:
        return "UNKNOWN"
    m = len(core)
    native = sum(1 for c, _ in core if c in ("NATIVE_TEXT", "VECTOR", "MIXED"))
    scan_no = sum(1 for c, f in core if c == "RASTER_SCAN" and "EMBEDDED_TEXT_LAYER" not in f)
    scan_tx = sum(1 for c, f in core if c == "RASTER_SCAN" and "EMBEDDED_TEXT_LAYER" in f)
    broken = sum(1 for c, _ in core if c == "BROKEN_TEXT_LAYER")
    if broken / m >= 0.5:
        return "BROKEN_TEXT_LAYER"
    if native / m >= 0.9:
        return "NATIVE"
    if scan_no / m >= 0.9:
        return "SCANNED_NO_TEXT"
    if scan_tx / m >= 0.9:
        return "SCANNED_WITH_TEXT_LAYER"
    if (scan_no + scan_tx) / m >= 0.9:
        return "SCANNED_PARTIAL_TEXT_LAYER"
    return "MIXED"
