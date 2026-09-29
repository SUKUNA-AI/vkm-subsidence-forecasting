"""Figure digitization (prototype, agent FD): plots of the corpus → numeric series with provenance.

Routes:

* **B — AutoCAD** (default for vector figures): the figure is cut out of its PDF page (:mod:`.crop`: one page,
  everything outside the figure box redacted, CropBox = figure box), imported headless with ``-PDFIMPORT`` through
  ``vkm-cad`` (DXF with MTEXT, polylines, splines, true colours), then calibrated and traced (:mod:`.dxf_route`);
* **A — native vectors**: the same core on PyMuPDF paths and the PDF text layer (:mod:`.pdf_route`);
* **R — raster**: scanned or embedded raster plots (:mod:`.raster`): axes by line detection, tick labels from corpus
  text or a local OCR helper, curves by colour clustering.

Tick labels that are not text (rotated labels exported as glyph outlines, raster labels) are read by
:mod:`.labels_ocr`: corpus text of the region first, then a local OCR helper used *only* for the calibration of this
derived object; the engine and its settings are recorded in the provenance, nothing goes into the canon.

Rules (CLAUDE.md, SCIENTIFIC_RULES_RU.md): every digitized value is ``DERIVATION`` with a per-point error estimate,
``review_status = AUTO_EXTRACTED_UNREVIEWED``, never ``FACT``; a plotted model result is not an observation (the
series keeps the caption/legend text as printed, classification is a later review step); availability in time is the
publication date of the source work; nothing here is promoted to evidence.
"""
DIGITIZER_VERSION = "fd-0.1.0"
STATUS = "DERIVATION"
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
FORBIDDEN_STATUSES = frozenset({"FACT", "REVIEWED_MEASUREMENT", "ACCEPTED_PARAMETER", "ACCEPTED_FORMULA"})

__all__ = ["DIGITIZER_VERSION", "FORBIDDEN_STATUSES", "REVIEW_STATUS", "STATUS"]
