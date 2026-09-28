"""vkm-cad: Autodesk bridge of the VKM project (stdio MCP server ``vkm-cad`` on WORKSTATION).

* :mod:`vkm_cad.detect` — capability detector: registry, files, ``dotnet``, process list. It never starts AutoCAD and
  never touches COM, and it does not read licence or serial values.
* :mod:`vkm_cad.com_read` — read-only access to documents the user opened, only by attaching to a running AutoCAD
  (``GetActiveObject``) through an allow-list proxy. Starting AutoCAD by automation is not implemented.
* :mod:`vkm_cad.scratch` + :mod:`vkm_cad.dxf` — scratch documents built with ``ezdxf`` (no Autodesk needed): native PDF
  vector paths → DXF with ``$INSUNITS = 0``, XDATA ``VKM_UNITS=PAGE_PT``, fixed header dates, ``crs_status``
  ``UNKNOWN_CRS`` by default. Outputs are DERIVED and never an input of document extraction (H-20).

Source files are never modified: inputs are copied into a scratch directory with SHA-256 checks before and after.
``accoreconsole`` and a .NET plugin are not part of v0.
"""
__version__ = "0.1.0"

__all__ = ["__version__"]
