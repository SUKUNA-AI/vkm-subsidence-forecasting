"""Figure digitization (prototype, agent FD): plots of the corpus → numeric series with provenance.

Routes (one core: axis calibration, curve extraction, per-point errors — :mod:`.core`):

* **A — native vectors** (recommended default for vector figures): PyMuPDF paths with their clip state and the PDF
  text layer (:mod:`.pdf_route`) — the same paths as the pipeline's ``VECTOR_PATHS_JSON``;
* **B — AutoCAD** (independent cross-check and the way to a DWG): the figure page is copied with CropBox = figure
  box (:mod:`.crop`), imported headless with ``-PDFIMPORT`` through ``vkm-cad``, and the DXF (MTEXT, polylines,
  splines, true colours) is read by :mod:`.dxf_route`; MTEXT is checked against the visible PDF text layer;
* **R — raster** (scans and embedded images, :mod:`.raster`, :mod:`.raster_digitize`): axes by line detection, tick
  labels from corpus text or a local OCR helper, curves by column tracking; gaps are not filled.

Tick labels that are not text (rotated labels exported as glyph outlines, raster labels) are read by
:mod:`.labels_ocr`: corpus text of the region first, then a local OCR helper used *only* for the calibration of this
derived object; the engine and its settings are recorded in the provenance, nothing goes into the canon.

Rules (CLAUDE.md, SCIENTIFIC_RULES_RU.md): every digitized value is ``DERIVATION`` with a per-point error estimate,
``review_status = AUTO_EXTRACTED_UNREVIEWED``, never ``FACT``; a plotted model result is not an observation (the
series keeps the caption/legend text as printed, classification is a later review step); availability in time is the
publication date of the source work; nothing here is promoted to evidence.

As a pipeline stage, route A runs as the NAV part ``figure_series`` (:mod:`vkm_corpus.navigation.figure_series`).

Versions: fd-0.1.2 — FD sweep (29.09); fd-0.1.3 — route A reads rotated pages in the displayed frame (agent FD2);
fd-0.1.4 — a log10 axis only when its labels span ≥ 3× (years and narrow ranges stay linear; agent FD2);
fd-0.1.5 — fixes after the visual review of 89 figures (08.10): labels snap to the tick beside them, a corner «0» off
its tick is snapped where its neighbours put it, the axes are one chart's corner (multi-panel boxes), stacked panels
read each series on its own y scale, a second y scale beside the first is flagged, legend markers are not data and
name their series, identical markers close together are kept, scientific-notation tick labels are numbers; route R:
strips anchored at the axis lines, minus signs kept (also read as «=», «—»), implausible OCR axes withdrawn, misread
labels dropped, rotated date labels read, the plot box no longer cut at a zone line and reaching the unread first
tick, filled label boxes not traced, OCR garbage and tick numbers not series labels.
"""
DIGITIZER_VERSION = "fd-0.1.5"
STATUS = "DERIVATION"
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
FORBIDDEN_STATUSES = frozenset({"FACT", "REVIEWED_MEASUREMENT", "ACCEPTED_PARAMETER", "ACCEPTED_FORMULA"})

__all__ = ["DIGITIZER_VERSION", "FORBIDDEN_STATUSES", "REVIEW_STATUS", "STATUS"]
