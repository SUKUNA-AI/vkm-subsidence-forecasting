"""vkm-drawio: deterministic draw.io diagrams for the VKM project (stdio MCP server ``vkm-drawio`` on WORKSTATION).

* A structured :class:`~vkm_drawio.model.DiagramSpec` is written as uncompressed, byte-deterministic ``.drawio`` XML
  (:mod:`vkm_drawio.xmlio`); files edited in the draw.io GUI or laid out by the draw.io CLI are canonicalised by the
  same writer, so diffs stay small.
* Layout is either explicit coordinates, a trivial deterministic grid for unplaced nodes, or the draw.io Desktop CLI
  ``--layout`` followed by canonicalisation. There is no in-house graph layout engine.
* Writes happen only inside two roots: ``public`` = ``<PUBLIC>/docs/diagrams`` (committed; leakage policy applies) and
  ``work`` = ``$VKM_WORK/diagrams`` (drafts, PDF exports). Nothing is overwritten unless ``overwrite=true``.

Pure-Python operations (create/read/update/list) work without draw.io Desktop; export, preview, CLI layout and
``drawio_open`` need the executable (:mod:`vkm_drawio.locate`).
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
