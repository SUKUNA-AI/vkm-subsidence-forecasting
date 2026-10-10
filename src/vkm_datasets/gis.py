"""Direct native vector -> GeoPackage conversion with an auditable, loss-detecting round trip.

Call inside an existing GDAL Python runtime. No subprocess/shell, reprojection, CRS assignment,
geometry repair, intermediate GeoJSON/Shapefile, network fetch or production publication occurs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from . import manifest as manifest_module
from .manifest import (DatasetVersion, canonical_bytes, confined, discover, sha256, verify_members,
                       durable_mkdir, durable_write_new, fsync_directory, fsync_file)

RULE_VERSION = "vkm-native-to-gpkg/2"


class ConversionBlocked(RuntimeError):
    """A retained partial attempt needs verification or a new explicit directory."""
    status = "BLOCKED"

    def __init__(self, reason="INCOMPLETE_BUNDLE_REQUIRES_NEW_ATTEMPT"):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class VectorLimits:
    max_features: int = 200_000
    max_fields: int = 4096
    max_geometry_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()):
            raise ValueError("vector limits must be positive integers")


def _request_identity(version, entrypoint, destination, keys, limits, gdal):
    """Exact invocation/implementation/engine identity; no machine paths in receipts."""
    options = {"global": getattr(gdal, "GetConfigOptions", lambda: {})(),
               "thread": getattr(gdal, "GetThreadLocalConfigOptions", lambda: {})(),
               "environment": {k: v for k, v in os.environ.items()
                               if k.startswith(("GDAL_", "OGR_", "CPL_", "PROJ_"))}}
    implementations = {name: manifest_module.sha256(Path(__file__).with_name(name))
                       for name in ("gis.py", "manifest.py", "policy.py")}
    request = {"schema": "vkm-native-conversion-request/1", "dataset_version": version.digest,
        "entrypoint": entrypoint, "destination": destination, "policy": version.policy.as_dict(),
        "keys": {name: list(columns) for name, columns in sorted((keys or {}).items())},
        "limits": asdict(limits), "rule_version": RULE_VERSION, "implementation_sha256": implementations,
        "gdal": {"release": gdal.VersionInfo("RELEASE_NAME"),
                 "build_sha256": hashlib.sha256(gdal.VersionInfo("BUILD_INFO").encode()).hexdigest(),
                 "configuration_sha256": hashlib.sha256(canonical_bytes(options)).hexdigest()}}
    return request, hashlib.sha256(canonical_bytes(request)).hexdigest()


def _durability():
    return {"files": "FSYNC", "directories": "FSYNC" if os.name == "posix" else "NOT_QUALIFIED",
            "power_loss_qualification": "NOT_RUN"}


def _commit_bundle(out, staging, receipt):
    """Dependencies reach disk and their directory before the final marker."""
    try:
        for path in staging.iterdir():
            if path.is_file():
                fsync_file(path)
        fsync_directory(staging)
        durable_write_new(staging / "receipt.json", canonical_bytes(receipt))
        fsync_directory(staging)
        for path in sorted(staging.iterdir()):
            if path.name != "receipt.json":
                os.rename(path, out / path.name)
        fsync_directory(out)
        fsync_directory(staging)
        os.rename(staging / "receipt.json", out / "receipt.json")
        fsync_directory(out)
        fsync_directory(staging)
        # Retain the owned pending directory, including on interruption. No cleanup.
    except Exception as exc:
        reason = ("COMMIT_ACK_UNCERTAIN_RETRY_IDENTICAL_INVOCATION" if (out / "receipt.json").exists()
                  else "INCOMPLETE_BUNDLE_REQUIRES_NEW_ATTEMPT")
        raise ConversionBlocked(reason) from exc


def _engine():
    try:
        from osgeo import gdal, ogr
    except ImportError as e:
        raise RuntimeError("use an existing configured GDAL runtime; no installation is performed") from e
    return gdal, ogr


def _crs(srs) -> dict:
    if srs is None:
        return {"status": "UNKNOWN", "wkt": None, "authority": None, "unit": None}
    name = srs.GetName() or ""
    # GeoPackage -1/0 undefined placeholders are NOT discovered WGS84/geodetic reference systems.
    undefined = name.lower().startswith("undefined") or name.lower().startswith("unknown")
    authority = srs.GetAuthorityName(None)
    code = srs.GetAuthorityCode(None)
    return {"status": "UNKNOWN" if undefined else ("LOCAL" if srs.IsLocal() else "REFERENCED"),
            "wkt": srs.ExportToWkt(), "authority": f"{authority}:{code}" if authority and code else None,
            "unit": None if undefined else (srs.GetAngularUnitsName() if srs.IsGeographic() else srs.GetLinearUnitsName())}


def _value(feature, index, field_type, ogr):
    if not feature.IsFieldSetAndNotNull(index):
        return None
    if field_type in {ogr.OFTDate, ogr.OFTTime, ogr.OFTDateTime}:
        return {"datetime_components": list(feature.GetFieldAsDateTime(index))}
    value = feature.GetField(index)
    if isinstance(value, float):
        return {"float_hex": value.hex()}  # explicit nan/inf, signed zero; no JSON coercion or rounding
    if isinstance(value, (bytes, bytearray)):
        return {"binary_hex": bytes(value).hex()}
    return value


def _scan(ds, limits: VectorLimits, keys: dict[str, tuple[str, ...]] | None = None):
    _, ogr = _engine()
    layers, styles, seen_layers = [], [], set()
    for layer in ds:
        name = layer.GetName()
        if name.casefold() in seen_layers or name.casefold().startswith("_vkm_"):
            raise ValueError("duplicate/reserved layer identity")
        seen_layers.add(name.casefold())
        definition = layer.GetLayerDefn()
        fast_count = layer.GetFeatureCount(0)
        if fast_count > limits.max_features:
            raise ValueError("feature memory/time guard exceeded")
        nfields = definition.GetFieldCount()
        if nfields > limits.max_fields:
            raise ValueError("field memory guard exceeded")
        fields = []
        for i in range(nfields):
            f = definition.GetFieldDefn(i)
            fields.append({"name": f.GetName(), "type": f.GetType(), "type_name": f.GetTypeName(),
                           "subtype": f.GetSubType(), "width": f.GetWidth(), "precision": f.GetPrecision(),
                           "nullable": bool(f.IsNullable()), "default": f.GetDefault()})
        if len({f["name"].casefold() for f in fields}) != len(fields):
            raise ValueError("case-ambiguous field names cannot be represented safely in SQLite")
        key = tuple((keys or {}).get(name, ()))
        indexes = [definition.GetFieldIndex(k) for k in key]
        if any(i < 0 for i in indexes) or len(set(key)) != len(key):
            raise ValueError("invalid composite key fields")
        digest, style_digest, fid_digest = hashlib.sha256(), hashlib.sha256(), hashlib.sha256()
        count, invalid, duplicates, null_keys = 0, 0, 0, 0
        key_seen = set()
        layer.ResetReading()
        for feature in layer:
            if count >= limits.max_features:
                raise ValueError("feature memory/time guard exceeded")
            values = [_value(feature, i, f["type"], ogr) for i, f in enumerate(fields)]
            geometry = feature.GetGeometryRef()
            raw_geometry = bytes(geometry.ExportToIsoWkb()) if geometry is not None else None
            if raw_geometry is not None and len(raw_geometry) > limits.max_geometry_bytes:
                raise ValueError("geometry memory guard exceeded")
            if geometry is not None and not geometry.IsValid():
                invalid += 1
            if indexes:
                key_values = [values[i] for i in indexes]
                null_keys += int(any(v is None for v in key_values))
                key_bytes = canonical_bytes({"key": key_values})
                duplicates += int(key_bytes in key_seen)
                key_seen.add(key_bytes)
            digest.update(canonical_bytes({"attributes": values, "geometry_hex": raw_geometry.hex()
                                           if raw_geometry is not None else None}))
            fid_digest.update(canonical_bytes({"ordinal": count, "source_fid": feature.GetFID()}))
            style = feature.GetStyleString()
            style_digest.update(canonical_bytes({"style": style}))
            if style:
                styles.append({"layer": name, "ordinal": count, "source_fid": feature.GetFID(), "style": style})
            count += 1
        layers.append({"name": name, "fields": fields, "feature_count": count,
                       "geometry_type": definition.GetGeomType(), "crs": _crs(layer.GetSpatialRef()),
                       "content_sha256": digest.hexdigest(), "styles_sha256": style_digest.hexdigest(),
                       "fid_sha256": fid_digest.hexdigest(), "fid_column": layer.GetFIDColumn(),
                       "invalid_geometry_count": invalid, "key_fields": list(key), "duplicate_keys": duplicates,
                       "null_keys": null_keys, "key_validation": "CHECKED" if key else "NOT_RUN",
                       "metadata": layer.GetMetadata_Dict()})
    if keys and set(keys) - {layer["name"] for layer in layers}:
        raise ValueError("key declaration names a missing layer")
    return layers, styles


def _open(path):
    gdal, _ = _engine()
    ds = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY,
                     allowed_drivers=["MapInfo File", "GPKG"],
                     open_options=["LIST_ALL_TABLES=NO"] if Path(path).suffix.lower() == ".gpkg" else [])
    if ds is None:
        raise ValueError("GDAL could not open the declared native vector dataset")
    return ds


def _native_members(root, version, entrypoint):
    declared = {f.path: f for f in version.files}
    for actual in discover(root, (entrypoint,)):
        registered = declared.get(actual.path)
        if registered is None or (registered.sha256, registered.size_bytes) != (actual.sha256, actual.size_bytes):
            raise ValueError("native companion is missing from the immutable manifest")


def _native_header(path):
    if path.suffix.lower() not in {".tab", ".mif"}:
        return {}
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024)
    text = raw.decode("latin1")  # structural keywords only; never guess the encoding of field values
    charsets = re.findall(r'(?im)^\s*!?charset\s+"?([^"\r\n]+)', text)
    coordinate = re.search(r"(?im)^\s*CoordSys\s+([^\r\n]+)", text)
    return {"charset_declarations": charsets, "coordinate_declaration": coordinate.group(1) if coordinate else None,
            "text_decoding": "GDAL_INTERPRETATION; original bytes retained, scientific review NOT_CHECKED"}


def inspect_vector(root: Path, version: DatasetVersion, entrypoint: str, *, destination="local",
                   keys: dict[str, tuple[str, ...]] | None = None, limits=VectorLimits()) -> dict:
    version.policy.require(destination)
    if entrypoint not in version.entrypoints or Path(entrypoint).suffix.lower() not in {".tab", ".mif", ".gpkg"}:
        raise ValueError("not a registered native vector entrypoint")
    verify_members(root, version.files)
    _native_members(root, version, entrypoint)
    ds = _open(confined(root, entrypoint))
    try:
        layers, _ = _scan(ds, limits, keys)
        driver = ds.GetDriver().ShortName
    finally:
        ds = None
    verify_members(root, version.files)
    gdal, _ = _engine()
    return {"schema": "vkm-vector-inspection-v1", "dataset_version": version.digest,
            "policy": version.policy.as_dict(), "driver": driver, "gdal_version": gdal.VersionInfo("RELEASE_NAME"),
            "layers": layers, "native_header": _native_header(confined(root, entrypoint)),
            "scientific_admission": "NOT_CHECKED"}


def _compare(source, target):
    errors, warnings = [], []
    if [x["name"] for x in source] != [x["name"] for x in target]:
        return ["LAYER_IDENTITY_MISMATCH"], warnings
    for a, b in zip(source, target):
        name = a["name"]
        for field in ("feature_count", "content_sha256", "geometry_type", "fid_sha256"):
            if a[field] != b[field]:
                errors.append(f"{name}:{field}:MISMATCH")
        core = lambda layer: [(f["name"], f["type"], f["subtype"]) for f in layer["fields"]]
        if core(a) != core(b):
            errors.append(name + ":FIELD_NAME_TYPE_MISMATCH")
        if a["fields"] != b["fields"]:
            warnings.append(name + ":NATIVE_FIELD_METADATA_PRESERVED_IN_SIDECAR")
        if a["styles_sha256"] != b["styles_sha256"]:
            warnings.append(name + ":NATIVE_STYLES_PRESERVED_IN_SIDECAR")
        if a["crs"]["status"] != b["crs"]["status"]:
            errors.append(name + ":CRS_STATUS_CHANGED")
        elif a["crs"]["status"] != "UNKNOWN":
            from osgeo import osr
            x, y = osr.SpatialReference(), osr.SpatialReference()
            x.ImportFromWkt(a["crs"]["wkt"])
            y.ImportFromWkt(b["crs"]["wkt"])
            if not x.IsSame(y):
                errors.append(name + ":CRS_CHANGED")
        elif b["crs"]["authority"]:
            errors.append(name + ":UNKNOWN_CRS_ACQUIRED_AUTHORITY")
    return errors, warnings


def convert_to_gpkg(root: Path, version: DatasetVersion, entrypoint: str, output_directory: Path, *,
                    destination="local", keys: dict[str, tuple[str, ...]] | None = None,
                    limits=VectorLimits()) -> dict:
    """Publish one immutable conversion bundle, or verify an identical completed retry.

    On a validation failure the bundle contains only candidate.gpkg and a REJECTED receipt;
    data.gpkg exists ONLY after full attribute/geometry/CRS comparison succeeds. Styles and
    original schemas/CRS are retained in native_metadata.json and a hash-bound SQLite table.
    Unknown CRS remains unknown and never acquires a metric/scientific admission claim.
    """
    version.policy.require(destination)
    if entrypoint not in version.entrypoints or Path(entrypoint).suffix.lower() not in {".tab", ".mif", ".gpkg"}:
        raise ValueError("not a registered native vector entrypoint")
    verify_members(root, version.files)
    _native_members(root, version, entrypoint)
    out = Path(output_directory).absolute()
    if out.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("conversion output must be outside original-byte quarantine")
    confined(out.parent, out.name, must_exist=False)
    if out.exists() and not (out / "receipt.json").is_file():
        raise ConversionBlocked()
    gdal, _ = _engine()
    request, request_sha = _request_identity(version, entrypoint, destination, keys, limits, gdal)
    if out.exists():
        proof = verify_conversion_bundle(out, version.digest, expected_request=request_sha)
        try:
            for name in ("data.gpkg", "native_metadata.json", "receipt.json"):
                fsync_file(confined(out, name))
            fsync_directory(out)
        except Exception as exc:
            raise ConversionBlocked("COMMIT_ACK_UNCERTAIN_RETRY_IDENTICAL_INVOCATION") from exc
        verify_members(root, version.files)
        # Return the committed receipt verbatim. Retry is not another conversion.
        raw = confined(out, "receipt.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != proof["receipt_sha256"]:
            raise ValueError("receipt changed after verification")
        return json.loads(raw)
    durable_mkdir(out.parent)
    # Exclusive reservation prevents concurrent writers from replacing an empty output directory.
    # Consumers must require receipt.json; it is the final commit marker, written after all artifacts.
    out.mkdir()
    try:
        fsync_directory(out.parent)
        staging = Path(tempfile.mkdtemp(prefix=".dataset-pending-", dir=out))
        fsync_directory(out)
    except Exception as exc:
        raise ConversionBlocked() from exc
    receipt = {"schema": "vkm-gpkg-conversion-v2", "dataset_id": version.dataset_id,
               "dataset_version": version.digest, "entrypoint": entrypoint, "policy": version.policy.as_dict(),
               "operation": "native_to_gpkg_no_reprojection_no_repair", "scientific_admission": "NOT_CHECKED",
               "request": request, "request_sha256": request_sha, "durability": _durability(),
               "status": "REJECTED", "errors": [], "warnings": []}
    source = target = None
    try:
        receipt["gdal_version"] = gdal.VersionInfo("RELEASE_NAME")
        receipt["native_header"] = _native_header(confined(root, entrypoint))
        source = _open(confined(root, entrypoint))
        before, styles = _scan(source, limits, keys)
        receipt["source_layers"] = before
        if any(x["invalid_geometry_count"] for x in before):
            raise ValueError("INVALID_SOURCE_GEOMETRY; no automatic repair")
        if any(x["duplicate_keys"] or x["null_keys"] for x in before):
            raise ValueError("NON_UNIQUE_OR_NULL_COMPOSITE_KEY")
        field_names = {f["name"].casefold() for layer in before for f in layer["fields"]}
        fid = "_vkm_fid"
        while fid.casefold() in field_names:
            fid += "_"
        candidate = staging / "candidate.gpkg"
        result = gdal.VectorTranslate(str(candidate), source, format="GPKG",
                                      preserveFID=True,
                                      layerCreationOptions=["SPATIAL_INDEX=NO", "FID=" + fid])
        if result is None:
            raise ValueError("GDAL conversion failed")
        result = None
        target = _open(candidate)
        after, _ = _scan(target, limits, keys)
        receipt["target_layers"] = after
        receipt["errors"], receipt["warnings"] = _compare(before, after)
        target = source = None
        verify_members(root, version.files)
        if receipt["errors"]:
            raise ValueError("ROUND_TRIP_VALIDATION_FAILED")
        metadata = {"schema": "vkm-native-vector-metadata-v1", "dataset_version": version.digest,
                    "request_sha256": request_sha,
                    "layers": before, "styles": styles, "native_header": receipt["native_header"],
                    "source_manifest": version.as_dict()}
        metadata_bytes = canonical_bytes(metadata)
        durable_write_new(staging / "native_metadata.json", metadata_bytes)
        # Metadata is a native nonspatial table, not a reinterpretation of the original attributes.
        with closing(sqlite3.connect(candidate)) as db, db:
            db.execute("CREATE TABLE _vkm_native_metadata (id INTEGER PRIMARY KEY, json TEXT NOT NULL, sha256 TEXT NOT NULL)")
            db.execute("INSERT INTO _vkm_native_metadata VALUES (1, ?, ?)",
                       (metadata_bytes.decode(), hashlib.sha256(metadata_bytes).hexdigest()))
        candidate.rename(staging / "data.gpkg")
        fsync_file(staging / "data.gpkg")
        fsync_directory(staging)
        receipt["outputs"] = {name: sha256(staging / name) for name in ("data.gpkg", "native_metadata.json")}
        receipt["status"] = "PASS_CONVERSION"
    except Exception as e:
        # Keep rejected output inspectable; do not publish it as canonical data or hide the error.
        receipt["status"] = "REJECTED"
        receipt.pop("outputs", None)
        receipt["errors"].append(type(e).__name__ + ": " + str(e))
        if (staging / "data.gpkg").exists():
            (staging / "data.gpkg").rename(staging / "candidate.gpkg")
    finally:
        source = target = None
    _commit_bundle(out, staging, receipt)
    return receipt


def verify_conversion_bundle(directory: Path, expected_version: str, *, expected_request: str | None = None) -> dict:
    """Verify the completed commit marker, immutable version binding and every published output hash.

    A directory left by a killed process has no receipt and is not a successful conversion.
    This verifies bytes/provenance, not reviewer authority or scientific admission.
    """
    receipt_path = confined(directory, "receipt.json")
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if receipt.get("schema") != "vkm-gpkg-conversion-v2" or receipt.get("status") != "PASS_CONVERSION" or \
            receipt.get("errors") != [] or \
            receipt.get("dataset_version") != expected_version:
        raise ValueError("conversion is incomplete, rejected or bound to another version")
    request = receipt.get("request")
    if not isinstance(request, dict) or \
            hashlib.sha256(canonical_bytes(request)).hexdigest() != receipt.get("request_sha256") or \
            request.get("dataset_version") != expected_version:
        raise ValueError("conversion request identity mismatch")
    if expected_request is not None and receipt["request_sha256"] != expected_request:
        raise ValueError("existing conversion uses different input, context, rules or config")
    if set(receipt.get("outputs", {})) != {"data.gpkg", "native_metadata.json"}:
        raise ValueError("incomplete output manifest")
    for name, digest in receipt["outputs"].items():
        if sha256(confined(directory, name)) != digest:
            raise ValueError("conversion output hash mismatch")
    metadata_bytes = confined(directory, "native_metadata.json").read_bytes()
    metadata = json.loads(metadata_bytes)
    if metadata.get("dataset_version") != expected_version or \
            DatasetVersion.from_dict(metadata["source_manifest"]).digest != expected_version or \
            metadata.get("request_sha256") != receipt["request_sha256"] or \
            metadata.get("layers") != receipt.get("source_layers"):
        raise ValueError("native metadata identity mismatch")
    with closing(sqlite3.connect(confined(directory, "data.gpkg").absolute().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT id,json,sha256 FROM _vkm_native_metadata").fetchall()
    expected_metadata = (1, metadata_bytes.decode(), hashlib.sha256(metadata_bytes).hexdigest())
    if rows != [expected_metadata]:
        raise ValueError("embedded native metadata identity mismatch")
    return {"status": "PASS", "dataset_version": expected_version, "request_sha256": receipt["request_sha256"],
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "durability": receipt.get("durability"), "scientific_admission": "NOT_CHECKED"}
