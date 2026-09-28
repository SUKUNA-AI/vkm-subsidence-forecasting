"""Canonical Arrow/Parquet store of the document layer (CP-16 p. 8; H-07, H-08, H-09, H-11).

* ``layout`` — data-root layout and the STAGING/CANONICAL root marker;
* ``writer`` — atomic writer of immutable partitions (required columns, unique keys, sort, key-value metadata);
* ``commits`` — source/registry commits with ``parent_commit_id`` and per-key leases; no-op detection;
* ``runs`` — run records and markers (START/END), run journals;
* ``admit`` — admission of published commits on the CANONICAL root;
* ``snapshot`` — manifest, validation, atomic ``CURRENT``;
* ``validator`` — canon checks (files, keys, references, provenance, science, completeness, hygiene, §50 trace);
* ``reader`` — manifest and dataset reading; ``gc`` — orphans older than 24 h; ``cli`` — ``vkm-corpus canon``.

pyarrow is imported lazily inside functions: importing this package needs only the standard library.
"""
