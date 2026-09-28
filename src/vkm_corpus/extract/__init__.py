"""Document extraction (agent C): container → format by signature → pages by two methods → page classification →
native extraction (PDF via PyMuPDF, DjVu via DjVuLibre CLI, EPUB via zipfile+lxml, DOCX via python-docx + raw XML).

Everything here produces *internal* extraction results (``vkm_corpus.extract.model``); the only module that knows the
canonical row schema is ``vkm_corpus.extract.to_canon``. Heavy libraries are imported inside functions.
"""
