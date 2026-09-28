"""VKM Corpus Platform v0: the document layer (L1) of the VKM / SKRU-1 scientific data system.

The raw scientific corpus stays in the PRIVATE repository (Git + LFS + ``SOURCE_REGISTER.csv``). This package turns it
into a reproducible structured corpus:

    registry + raw sources → native extraction → OCR/layout fallback → document objects
      → canonical Arrow/Parquet ($VKM_DATA_ROOT/canonical) → DuckDB / Neo4j / OpenSearch projections
      → text + visual reranking → API / MCP

Rules that every module keeps (decisions CP-01…CP-22, ``docs/implementation_work/COORDINATOR_DECISIONS.md``):

* automatic output is ``AUTO_EXTRACTED_UNREVIEWED``; nothing here ever promotes it to FACT or reviewed evidence;
* Parquet is canonical; DuckDB, Neo4j and OpenSearch are rebuildable projections; PostgreSQL is operational state only;
* the package depends on ``vkm_world``, never the other way round; heavy dependencies are imported lazily.
"""
from vkm_corpus.versions import PIPELINE_VERSION

__all__ = ["PIPELINE_VERSION"]
