"""Explicit native dataset operations; originals are never modified."""
import argparse
import json
from pathlib import Path
import sys

from .manifest import DatasetVersion, Registry, canonical_bytes, discover, verify_members
from .policy import AccessClass, ExperimentalRole, Policy


def main(argv=None):
    parser = argparse.ArgumentParser(prog="vkm-datasets")
    commands = parser.add_subparsers(dest="operation", required=True)
    create = commands.add_parser("manifest", help="inspect companion identities and emit a manifest to stdout")
    create.add_argument("--root", type=Path, required=True)
    create.add_argument("--entrypoint", action="append", required=True)
    create.add_argument("--additional", action="append", default=[])
    for name in ("dataset-id", "owner", "licence", "received-at", "decision-ref"):
        create.add_argument("--" + name, required=True)
    create.add_argument("--access-class", choices=[v.value for v in AccessClass], required=True)
    create.add_argument("--experimental-role", choices=[v.value for v in ExperimentalRole], required=True)
    create.add_argument("--data-valid-at")
    create.add_argument("--available-from")
    create.add_argument("--source-id", action="append", default=[])
    create.add_argument("--parent", action="append", default=[])
    create.add_argument("--allow-nonspatial-tab", action="store_true")
    for name in ("verify", "register", "inspect-workbook", "inspect-vector", "convert"):
        p = commands.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        p.add_argument("--manifest", type=Path, required=True)
        if name == "register":
            p.add_argument("--registry", type=Path, required=True)
        if name.startswith("inspect") or name == "convert":
            p.add_argument("--entrypoint", required=True)
            p.add_argument("--destination", choices=["local", "cloud", "public"], default="local")
        if name == "inspect-workbook":
            p.add_argument("--with-values", action="store_true")
        if name in {"inspect-vector", "convert"}:
            p.add_argument("--keys", type=json.loads, help='JSON object: {"layer":["key1","key2"]}')
        if name == "convert":
            p.add_argument("--output-directory", type=Path, required=True)
    bundle = commands.add_parser("verify-bundle", help="verify a completed conversion, including fresh output hashes")
    bundle.add_argument("--directory", type=Path, required=True)
    bundle.add_argument("--expected-version", required=True)
    a = parser.parse_args(argv)
    try:
        if a.operation == "verify-bundle":
            from .gis import verify_conversion_bundle
            result = verify_conversion_bundle(a.directory, a.expected_version)
        elif a.operation == "manifest":
            version = DatasetVersion(a.dataset_id, discover(a.root, tuple(a.entrypoint), additional=tuple(a.additional),
                                     allow_nonspatial_tab=a.allow_nonspatial_tab), tuple(a.entrypoint),
                                     Policy(a.access_class, a.experimental_role, a.decision_ref),
                                     a.owner, a.licence, a.received_at, a.data_valid_at, a.available_from,
                                     tuple(a.source_id), tuple(a.parent))
            result = version.as_dict()
        else:
            version = DatasetVersion.from_dict(json.loads(a.manifest.read_text(encoding="utf-8")))
            if a.operation in {"verify", "register"}:
                verify_members(a.root, version.files)
                if a.operation == "register":
                    Registry(a.registry).register(version)
                result = {"status": "PASS", "dataset_version": version.digest, "operation": a.operation}
            elif a.operation == "inspect-workbook":
                from .workbooks import inspect_workbook
                result = inspect_workbook(a.root, version, a.entrypoint, destination=a.destination,
                                          include_values=a.with_values)
            else:
                from .gis import convert_to_gpkg, inspect_vector
                if a.operation == "convert":
                    result = convert_to_gpkg(a.root, version, a.entrypoint, a.output_directory,
                                             destination=a.destination, keys=a.keys)
                else:
                    result = inspect_vector(a.root, version, a.entrypoint, destination=a.destination, keys=a.keys)
        raw = canonical_bytes(result)
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(raw)
        else:
            sys.stdout.write(raw.decode("utf-8"))
        return 2 if result.get("status") == "REJECTED" else 0
    except Exception as e:
        from .gis import ConversionBlocked
        if isinstance(e, ConversionBlocked):
            sys.stdout.write(canonical_bytes({"status": "BLOCKED", "reason": e.reason,
                                             "operation": a.operation}).decode("utf-8"))
            return 2
        # Do not print tracebacks or workbook values in failure logs.
        sys.stderr.write(type(e).__name__ + ": " + str(e) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
