"""PUBLIC evidence catalogues as one read-only DuckDB snapshot for the API (topic dossier, ``reconstruct_topic``).

* :mod:`vkm_corpus.catalogues.pack` — CSV files under ``catalogues/`` and ``evidence/`` of the PUBLIC repository →
  ``catalogues.duckdb`` (a table per CSV, cells verbatim as VARCHAR) + ``manifest.json`` (git commit, sha256 and rows
  per file); ``publish`` places a pack under ``$VKM_DATA_ROOT/derived/catalogues/<pack_id>/`` and points ``CURRENT`` at it;
* :mod:`vkm_corpus.catalogues.store` — read-only serving of the current pack (``CatalogueStore``, like ``NavStore``).

The catalogues are the PUBLIC-safe evidence records (no quotes): every record keeps its own status (FACT … UNKNOWN),
scope and scale. The pack is a derived serving copy; the repository files stay the source of truth.
"""
