"""vkm-cad: Autodesk bridge of the VKM project (stdio MCP server ``vkm-cad`` on WORKSTATION).

v0 (unchanged contract):

* :mod:`vkm_cad.detect` — capability detector: registry, files, ``dotnet``, process list. It never starts AutoCAD and
  never touches COM, and it does not read licence or serial values.
* :mod:`vkm_cad.com_read` — read-only access to documents the user opened, only by attaching to a running AutoCAD
  (``GetActiveObject``) through an allow-list proxy.
* :mod:`vkm_cad.scratch` + :mod:`vkm_cad.dxf` — scratch documents built with ``ezdxf``: native PDF vector paths → DXF.

v1 (the user's request of 28.09.2026: draw, run commands, build Civil 3D objects — in new documents only):

* :mod:`vkm_cad.jobs` — job directories, receipts, the engine lock (one licensed AutoCAD process at a time);
* :mod:`vkm_cad.engine` + :mod:`vkm_cad.lisp` — headless ``accoreconsole`` runs (isolated profile, crash/dialog/timeout
  aware); :mod:`vkm_cad.dotnet` + ``plugin/VkmCadHost.cs`` — the .NET host (AutoCAD and Civil 3D API) compiled with an
  installed Roslyn against the installed .NET 8 runtime;
* :mod:`vkm_cad.hidden` + :mod:`vkm_cad.hidden_runner` — the gated hidden full instance for ``python_com``;
* :mod:`vkm_cad.fallback` — pure-Python TIN / contours / differences / profiles; :mod:`vkm_cad.draw` — ezdxf drawings;
* :mod:`vkm_cad.cadjobs` — the typed operations; :mod:`vkm_cad.capabilities` — what can be called where;
* :mod:`vkm_cad.audit` — read-only audit of registry/profile side effects of every AutoCAD run.

Outputs are DERIVED objects (INTERPOLATION / DERIVATION with MODEL_CHOICE), ``crs_status = UNKNOWN_CRS`` unless an
explicit transform is passed, never an input of document extraction.
"""
__version__ = "1.0.0"

__all__ = ["__version__"]
