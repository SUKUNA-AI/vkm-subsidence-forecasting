"""Producer/publisher topology (CP-15 with the changes of H §4.1).

* ``transfer.publish`` — WORKSTATION: copy a STAGING root to the CANONICAL root on CORE with rsync, immutable files
  only (``--ignore-existing``), blobs and partitions first, run markers and commit markers last (H-09);
* ``reconcile.reconcile`` — CORE: admit new commits → snapshot + validator (``CURRENT`` moves only on PASS) → DuckDB →
  Neo4j (wipe) → OpenSearch; one entry point, one receipt (H-09);
* ``transfer.backup`` — WORKSTATION: reverse copy of the canonical layer and every artifact blob from CORE into an
  archive directory, with a sha256 manifest (H-10).

Nothing here decides scientific content; it only moves immutable files and runs the builders of D and E.
"""
