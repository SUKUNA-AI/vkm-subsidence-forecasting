"""Registry layer: ``SOURCE_REGISTER.csv`` → ``sources`` (251 rows, CP-05…CP-07) and the curated Work register
(``WORK_REGISTER.csv`` + ``WORK_LINKS.csv``, CP-09) → works, links, authors, venues; one REGISTRY commit.

* ``rules`` — lifecycle, review status, format by signature, row hashes;
* ``sources`` — register loading, parallel sha256 verification with cache, ``SourceRow`` building;
* ``works`` — curated Work files → rows (type signatures checked);
* ``importer`` — ``import_registry(layout, resources_root, ...)`` (STAGING);
* ``work_seed`` — bootstrap proposal of the Work register (never written to PRIVATE by code);
* ``cli`` — ``vkm-corpus registry import | verify | propose-works``.
"""
