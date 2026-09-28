"""The eleven ``vkm-cad`` operations as plain Python (the MCP layer only wraps them).

Read tools (``cad_status`` and five COM tools) never change anything. Scratch tools write only below the scratch root;
their outputs are DERIVED artifacts described by ``manifest.json`` (``coordinate_space = DRAWING_UNITS``, ``crs_status``
``UNKNOWN_CRS`` unless ``SCHEMATIC`` is chosen with a rationale, ``epsg = null``, ``review_status =
AUTO_EXTRACTED_UNREVIEWED``). Importing them into the corpus is a separate control-plane job (DN-G9), never a direct
write.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from vkm_cad import __version__
from vkm_cad import com_read, dxf
from vkm_cad.detect import DetectEnv, detect, progids
from vkm_cad.errors import ToolFailure
from vkm_cad.scratch import ScratchStore, env_value, sha256_file, utc_now
from vkm_corpus.ids.grammar import matches

ALLOWED_CRS = ("UNKNOWN_CRS", "SCHEMATIC")
DXF_MEDIA_TYPE = "image/vnd.dxf"
PREVIEW_ROWS = 50
MAX_GEOMETRY_ROWS = 200_000


class ArtifactFetcher:
    """Reads an artifact from the VKM API (``GET /v1/artifact/{id}/content``) with the read token."""

    def __init__(self, base_url: str, token: str | None, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def fetch(self, artifact_id: str) -> bytes | None:
        request = urllib.request.Request(f"{self.base_url}/v1/artifact/{artifact_id}/content")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - configured URL
                return response.read(dxf.MAX_VECTOR_BYTES + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None


def _crs(crs_status: str, rationale: str | None) -> tuple[str, dict[str, Any] | None]:
    if crs_status not in ALLOWED_CRS:
        raise ToolFailure("CRS_STATUS_NOT_ALLOWED", "a derived drawing is UNKNOWN_CRS, or SCHEMATIC with a rationale; "
                                                    "exact, local or map-digitised coordinates are never assigned "
                                                    "automatically")
    if crs_status == "SCHEMATIC":
        if not rationale or len(rationale.strip()) < 10:
            raise ToolFailure("CRS_STATUS_NOT_ALLOWED", "SCHEMATIC needs a rationale (≥ 10 characters)")
        return crs_status, {"kind": "MODEL_CHOICE", "rationale": rationale.strip()}
    return crs_status, None


class CadService:
    def __init__(self, scratch: ScratchStore, *, detect_env: DetectEnv | None = None,
                 com: com_read.ComSession | None = None, artifact_roots: list[Path] | None = None,
                 fetcher: ArtifactFetcher | None = None, detector: Callable[[], dict[str, Any]] | None = None) -> None:
        self.scratch = scratch
        self._detect = detector or (lambda: detect(detect_env))
        self._report: dict[str, Any] | None = None
        self.com = com or com_read.ComSession()
        self.artifact_roots = artifact_roots or []
        self.fetcher = fetcher

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CadService":
        env = os.environ if env is None else env
        roots = []
        data = env_value(env, "VKM_DATA_ROOT")
        if data:
            roots.append(Path(data) / "artifacts")
        fetcher = None
        from vkm_corpus.config import ConfigError, load_settings

        try:
            settings = load_settings(dict(env))
            if settings.api_url and "${" not in settings.api_url:
                fetcher = ArtifactFetcher(settings.api_url, settings.api_token)
        except ConfigError:
            fetcher = None
        return cls(ScratchStore.from_env(env), artifact_roots=roots, fetcher=fetcher)

    # -------------------------------------------------------------------------------------------- read tools
    def report(self, refresh: bool = False) -> dict[str, Any]:
        if self._report is None or refresh:
            self._report = self._detect()
        return self._report

    def status(self) -> dict[str, Any]:
        report = self.report(refresh=True)
        scratch = {"available": self.scratch.root is not None, "reason": self.scratch.reason,
                   "logical": "$VKM_CAD_SCRATCH (or $VKM_WORK/cad_scratch)", "warnings": self.scratch.warnings()}
        return {**report, "scratch": scratch,
                "vector_sources": {"data_root_artifacts": bool(self.artifact_roots), "scratch_inbox": True,
                                   "vkm_api": self.fetcher is not None}}

    def _com(self, fn: Callable[[Any, Any], Any]) -> Any:
        report = self.report()
        cap = report["capabilities"]["read_open_documents"]
        if cap.startswith("UNAVAILABLE"):
            raise ToolFailure("CAD_UNAVAILABLE", f"reading open documents is unavailable: {cap.split(':', 1)[-1]}")
        return self.com.run(fn, progids(report))

    def list_open_documents(self) -> dict[str, Any]:
        docs = self._com(lambda app, _v: com_read.list_open_documents(app))
        return {"documents": docs, "count": len(docs)}

    def get_layers(self, doc_ref: str) -> dict[str, Any]:
        layers = self._com(lambda app, _v: com_read.get_layers(com_read.find_document(app, doc_ref)))
        return {"doc_ref": doc_ref, "layers": layers, "count": len(layers)}

    def get_extents(self, doc_ref: str, space: str = "model") -> dict[str, Any]:
        if space not in ("model", "paper"):
            raise ToolFailure("INVALID_ARGUMENT", "space is 'model' or 'paper'")
        out = self._com(lambda app, _v: com_read.get_extents(com_read.find_document(app, doc_ref), space))
        return {"doc_ref": doc_ref, **out}

    def list_entities(self, doc_ref: str, layers: list[str] | None = None, types: list[str] | None = None,
                      limit: int = 200, cursor: int = 0, include_bbox: bool = False) -> dict[str, Any]:
        out = self._com(lambda app, v: com_read.list_entities(com_read.find_document(app, doc_ref), v,
                                                               layers=layers, types=types, limit=limit,
                                                               cursor=cursor, include_bbox=include_bbox))
        return {"doc_ref": doc_ref, **out}

    def get_coordinate_system(self, doc_ref: str) -> dict[str, Any]:
        out = self._com(lambda app, _v: com_read.get_coordinate_system(com_read.find_document(app, doc_ref)))
        return {"doc_ref": doc_ref, **out}

    # -------------------------------------------------------------------------------------------- scratch tools
    def _files_entry(self, rel: str, kind: str, data: bytes, media_type: str) -> dict[str, Any]:
        return {"path": rel, "kind": kind, "media_type": media_type, "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data)}

    def _base_manifest(self, operation: str, crs_status: str, crs_basis: dict[str, Any] | None) -> dict[str, Any]:
        return {"operation": operation, "created_at": utc_now(), "tool": "EZDXF", "tool_version": dxf.ezdxf_version(),
                "coordinate_space": "DRAWING_UNITS", "crs_status": crs_status, "crs_status_basis": crs_basis,
                "epsg": None, "review_status": "AUTO_EXTRACTED_UNREVIEWED", "never_input_of_extraction": True}

    def create_scratch_document(self, template: str = "empty", label: str | None = None,
                                dxf_version: str = "R2013") -> dict[str, Any]:
        if template != "empty":
            raise ToolFailure("INVALID_ARGUMENT", "template must be 'empty' in v0")
        dxf.ezdxf_module()
        doc = dxf.new_document(dxf_version, {"VKM_LABEL": label or ""} if label else None)
        data = dxf.to_bytes(doc)
        doc_id, _path = self.scratch.new_doc()
        self.scratch.write_bytes(doc_id, "doc.dxf", data)
        manifest = {**self._base_manifest("CREATE_EMPTY", "UNKNOWN_CRS", None), "label": label, "units": "PAGE_PT",
                    "insunits": 0, "dxf_version": dxf_version, "derived_from": [],
                    "files": [self._files_entry("doc.dxf", "CAD_DXF", data, DXF_MEDIA_TYPE)]}
        self.scratch.write_manifest(doc_id, manifest)
        return {"scratch_doc_id": doc_id, "doc": self.scratch.logical(doc_id, "doc.dxf"),
                "sha256": manifest["files"][0]["sha256"], "bytes": len(data), "dxf_version": dxf_version,
                "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS", "insunits": 0}

    def _find_vector(self, artifact_id: str) -> tuple[bytes, str]:
        if not matches("artifact", artifact_id):
            raise ToolFailure("INVALID_ARGUMENT", "vector_artifact_id has the form sha256:<64 hex>")
        digest = artifact_id[7:]
        candidates: list[tuple[Path, str]] = []
        for root in self.artifact_roots:
            base = root / "vector" / digest[0:2] / digest[2:4]
            if base.is_dir():
                candidates += [(p, "DATA_ROOT") for p in sorted(base.glob(digest + ".*"))]
        if self.scratch.root is not None and (self.scratch.root / "inbox").is_dir():
            candidates += [(p, "SCRATCH_INBOX") for p in sorted((self.scratch.root / "inbox").glob(digest + "*"))]
        for path, where in candidates:
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() == digest:
                return data, where
            raise ToolFailure("ARTIFACT_HASH_MISMATCH", f"stored bytes of {artifact_id} do not match its id ({where})")
        if self.fetcher is not None:
            data = self.fetcher.fetch(artifact_id)
            if data is not None:
                if len(data) > dxf.MAX_VECTOR_BYTES:
                    raise ToolFailure("PAYLOAD_TOO_LARGE", "vector artifact is larger than the bridge limit")
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ToolFailure("ARTIFACT_HASH_MISMATCH", f"bytes served for {artifact_id} do not match its id")
                return data, "VKM_API"
        raise ToolFailure("NO_VECTOR_ARTIFACT", f"{artifact_id} not found in the data root, the scratch inbox or the "
                                                "VKM API")

    def import_pdf_vector(self, vector_artifact_id: str, page_height_pt: float | None = None,
                          source_id: str | None = None, page_id: str | None = None,
                          crs_status: str = "UNKNOWN_CRS", crs_rationale: str | None = None,
                          dxf_version: str = "R2013", label: str | None = None) -> dict[str, Any]:
        crs_status, crs_basis = _crs(crs_status, crs_rationale)
        if source_id is not None and not matches("source", source_id):
            raise ToolFailure("INVALID_ARGUMENT", "source_id has the form VKM-SRC-NNN")
        if page_id is not None and not matches("page", page_id):
            raise ToolFailure("INVALID_ARGUMENT", "page_id has the form VKM-SRC-NNN:pNNNN")
        if page_id is not None and source_id is not None and not page_id.startswith(source_id + ":"):
            raise ToolFailure("INVALID_ARGUMENT", "page_id does not belong to source_id")
        if page_height_pt is not None and not 1 <= page_height_pt <= 100_000:
            raise ToolFailure("INVALID_ARGUMENT", "page_height_pt must be 1…100000")
        dxf.ezdxf_module()
        data, where = self._find_vector(vector_artifact_id)
        vectors = dxf.load_vector_json(data)
        height = page_height_pt if page_height_pt is not None else vectors.get("page_height_pt")
        y_transform = (f"y_cad = {float(height):g} - y_page (page height, pt)" if height is not None
                       else "y_cad = -y_page (page height unknown)")
        meta = {"VKM_VECTOR_ARTIFACT": vector_artifact_id, "VKM_Y_TRANSFORM": y_transform, "VKM_CRS_STATUS": crs_status,
                **({"VKM_SOURCE_ID": source_id} if source_id else {}), **({"VKM_PAGE_ID": page_id} if page_id else {})}
        doc = dxf.new_document(dxf_version, meta)
        counts = dxf.add_vector_paths(doc, vectors, page_height_pt=float(height) if height is not None else None)
        out = dxf.to_bytes(doc)
        doc_id, _path = self.scratch.new_doc()
        suffix = ".json.gz" if data[:2] == b"\x1f\x8b" else ".json"
        self.scratch.write_bytes(doc_id, f"in/vector_paths{suffix}", data)
        self.scratch.write_bytes(doc_id, "doc.dxf", out)
        manifest = {**self._base_manifest("IMPORT_PDF_VECTOR", crs_status, crs_basis), "label": label,
                    "source_bbox_space": "PAGE_PT_TL", "units": "PAGE_PT", "insunits": 0, "y_transform": y_transform,
                    "dxf_version": dxf_version, "source_id": source_id, "page_id": page_id, "counts": counts,
                    "vector_truncated": bool(vectors.get("truncated")),
                    "derived_from": [{"artifact_id": vector_artifact_id, "kind": "VECTOR_PATHS_JSON",
                                      "location": where, "copy": f"in/vector_paths{suffix}"}],
                    "files": [self._files_entry("doc.dxf", "CAD_DXF", out, DXF_MEDIA_TYPE)]}
        self.scratch.write_manifest(doc_id, manifest)
        return {"scratch_doc_id": doc_id, "doc": self.scratch.logical(doc_id, "doc.dxf"),
                "sha256": manifest["files"][0]["sha256"], "bytes": len(out), "counts": counts,
                "coordinate_space": "DRAWING_UNITS", "source_bbox_space": "PAGE_PT_TL", "units": "PAGE_PT",
                "insunits": 0, "y_transform": y_transform, "crs_status": crs_status, "epsg": None,
                "derived_from": manifest["derived_from"]}

    def _working_dxf(self, doc_id: str) -> tuple[Path, Any]:
        path = self.scratch.doc_dir(doc_id) / "doc.dxf"
        if not path.is_file():
            raise ToolFailure("FORMAT_NOT_SUPPORTED", "this scratch document has no DXF working copy (a DWG copy "
                                                      "needs AutoCAD conversion, not part of v0)")
        return path, dxf.read_bytes(path.read_bytes())

    def extract_geometry(self, scratch_doc_id: str, layers: list[str] | None = None, types: list[str] | None = None,
                         limit: int = 100_000, out_name: str = "geometry.jsonl",
                         overwrite: bool = False) -> dict[str, Any]:
        if not 1 <= limit <= MAX_GEOMETRY_ROWS:
            raise ToolFailure("INVALID_ARGUMENT", f"limit must be 1…{MAX_GEOMETRY_ROWS}")
        if not out_name.endswith(".jsonl"):
            raise ToolFailure("INVALID_ARGUMENT", "out_name must end with .jsonl")
        target = self.scratch.out_path(scratch_doc_id, out_name, overwrite)
        _path, doc = self._working_dxf(scratch_doc_id)
        rows, summary = dxf.extract_geometry(doc, layers=layers, types=types, limit=limit)
        data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")
        info = self.scratch.write_bytes(scratch_doc_id, f"out/{target.name}", data)
        manifest = self.scratch.manifest(scratch_doc_id)
        files = [f for f in manifest.get("files", []) if f["path"] != info["path"]]
        files.append(self._files_entry(info["path"], "CAD_GEOMETRY_JSONL", data, "application/x-ndjson"))
        self.scratch.write_manifest(scratch_doc_id, {**{k: v for k, v in manifest.items()
                                                       if k not in ("schema", "scratch_doc_id", "bridge_version")},
                                                    "files": files})
        return {"scratch_doc_id": scratch_doc_id, "geometry": self.scratch.logical(scratch_doc_id, info["path"]),
                "sha256": info["sha256"], "bytes": info["bytes"], "summary": summary,
                "preview": rows[:PREVIEW_ROWS], "coordinate_space": "DRAWING_UNITS",
                "crs_status": manifest.get("crs_status", "UNKNOWN_CRS")}

    def export_dxf(self, scratch_doc_id: str, dxf_version: str = "R2013", out_name: str | None = None,
                   overwrite: bool = False) -> dict[str, Any]:
        if dxf_version not in dxf.DXF_VERSIONS:
            raise ToolFailure("INVALID_ARGUMENT", f"dxf_version must be one of {sorted(dxf.DXF_VERSIONS)}")
        name = out_name or f"{scratch_doc_id}.{dxf_version}.dxf"
        if not name.endswith(".dxf"):
            raise ToolFailure("INVALID_ARGUMENT", "out_name must end with .dxf")
        target = self.scratch.out_path(scratch_doc_id, name, overwrite)
        _path, doc = self._working_dxf(scratch_doc_id)
        wanted = dxf.DXF_VERSIONS[dxf_version]
        if doc.dxfversion != wanted:
            if doc.dxfversion > wanted:
                raise ToolFailure("INVALID_ARGUMENT", "down-conversion of DXF versions is not supported")
            doc.dxfversion = wanted
        data = dxf.to_bytes(doc)
        info = self.scratch.write_bytes(scratch_doc_id, f"out/{target.name}", data)
        manifest = self.scratch.manifest(scratch_doc_id)
        files = [f for f in manifest.get("files", []) if f["path"] != info["path"]]
        files.append(self._files_entry(info["path"], "CAD_DXF", data, DXF_MEDIA_TYPE))
        self.scratch.write_manifest(scratch_doc_id, {**{k: v for k, v in manifest.items()
                                                       if k not in ("schema", "scratch_doc_id", "bridge_version")},
                                                    "files": files})
        return {"scratch_doc_id": scratch_doc_id, "dxf": self.scratch.logical(scratch_doc_id, info["path"]),
                "sha256": info["sha256"], "bytes": info["bytes"], "dxf_version": dxf_version,
                "insunits": int(doc.header.get("$INSUNITS", 0)), "coordinate_space": "DRAWING_UNITS",
                "crs_status": manifest.get("crs_status", "UNKNOWN_CRS"), "epsg": None}

    def save_copy(self, doc_ref: str) -> dict[str, Any]:
        info = self._com(lambda app, _v: com_read.saved_file_info(com_read.find_document(app, doc_ref)))
        full = info["full_name"]
        if not full or not os.path.isabs(full):
            raise ToolFailure("INVALID_ARGUMENT", "the document has never been saved to a file")
        source = Path(full)
        ext = source.suffix.lower()
        if ext not in (".dwg", ".dxf"):
            raise ToolFailure("FORMAT_NOT_SUPPORTED", f"only .dwg and .dxf documents are copied, not {ext}")
        if not source.is_file():
            raise ToolFailure("INVALID_ARGUMENT", "the saved file of the document is not reachable")
        doc_id, _path = self.scratch.new_doc()
        copied = self.scratch.copy_in(doc_id, source, f"source{ext}")
        files = [{"path": copied["path"], "kind": "CAD_DWG" if ext == ".dwg" else "CAD_DXF",
                  "media_type": "image/vnd.dwg" if ext == ".dwg" else DXF_MEDIA_TYPE, "sha256": copied["sha256"],
                  "bytes": copied["bytes"]}]
        if ext == ".dxf":
            data = (self.scratch.doc_dir(doc_id) / copied["path"]).read_bytes()
            self.scratch.write_bytes(doc_id, "doc.dxf", data)
            files.append(self._files_entry("doc.dxf", "CAD_DXF", data, DXF_MEDIA_TYPE))
        manifest = {**self._base_manifest("SAVE_COPY", "UNKNOWN_CRS", None), "tool": "FILE_COPY",
                    "tool_version": __version__, "captured_state": "LAST_SAVED_ON_DISK",
                    "unsaved_changes": not info["saved"], "user_document_name": info["name"],
                    "derived_from": [{"user_document": info["name"], "sha256": copied["source_sha256_before"]}],
                    "files": files}
        self.scratch.write_manifest(doc_id, manifest)
        return {"scratch_doc_id": doc_id, "captured_state": "LAST_SAVED_ON_DISK",
                "unsaved_changes": not info["saved"], "name": info["name"], "format": ext.lstrip("."),
                "copy": self.scratch.logical(doc_id, copied["path"]), "sha256": copied["sha256"],
                "bytes": copied["bytes"], "original_unchanged": copied["source_sha256_before"] ==
                copied["source_sha256_after"], "coordinate_space": "DRAWING_UNITS", "crs_status": "UNKNOWN_CRS"}

    def scratch_file_sha(self, doc_id: str, rel: str) -> str:
        return sha256_file(self.scratch.doc_dir(doc_id) / rel)
