"""Navigation layer (NAV) of the corpus: sections (document tree), formula context and concepts — no LLM.

Everything here is DERIVED from the canonical snapshot (DuckDB) and, for native outlines, from the source files on
the producer; every row carries a rule version and points to real pages/blocks. Design and field lists:
``docs/corpus_platform/NAVIGATION_LAYER.md``. Builders are pure functions over canonical rows returning Arrow
tables; ``vkm_corpus.navigation.ids`` holds the shared identifiers.
"""
