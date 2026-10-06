"""Operator CLI: schema checks, durable publication and complete permitted export."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import field_validator

from vkm_corpus.contracts.access import AccessContext
from vkm_evidence.contracts import EvidenceBatch, Identifier, Sha256, StrictModel, canonical_bytes, record_hash
from vkm_evidence.coverage import CoverageLedger
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.query import EvidenceReader


QUALIFICATION_MAX_BYTES = 16 * 1024 * 1024


class TrustedQualificationRegistration(StrictModel):
    """Contents of an externally approved receipt, not a CLI-issued registration.

    Authentication and append-only chronology belong to the operator's trusted
    registry. This reader only verifies its externally supplied byte pin and the
    exact fields used by FrozenRegistration.
    """
    schema_version: Literal["vkm-qualification-preregistration-receipt/1"]
    plan_sha256: Sha256
    gold_sha256: Sha256
    registered_at: datetime
    registrar: Identifier

    @field_validator("registered_at")
    @classmethod
    def _utc(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone-aware registration required")
        return value.astimezone(timezone.utc)


def _qualification_path(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("indirect qualification path")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("indirect qualification path")
    return path


def _qualification_bytes(path: Path, expected_sha256: str | None = None) -> bytes:
    """Read bounded bytes once; validate the same bytes subsequently parsed."""
    path = _qualification_path(path)
    if expected_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("invalid qualification byte pin")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > QUALIFICATION_MAX_BYTES:
        raise ValueError("qualification input is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > QUALIFICATION_MAX_BYTES:
            raise ValueError("qualification input is not a bounded regular file")
        raw = stream.read(QUALIFICATION_MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    current = _qualification_path(path).stat()
    # Python's Windows path.stat/fstat expose different ctime semantics on some
    # NTFS versions. Byte pins plus inode/size/mtime work on both host families.
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if len(raw) > QUALIFICATION_MAX_BYTES or identity(before) != identity(after) or identity(after) != identity(current):
        raise ValueError("qualification input changed while reading")
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("qualification byte pin mismatch")
    return raw


def _qualification_model(path, pin, model):
    if bool(path) != bool(pin):
        raise ValueError("qualification input requires a separate byte pin")
    if not path:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate qualification JSON key")
            result[key] = value
        return result
    def reject_constant(_value):
        raise ValueError("non-finite qualification JSON constant")
    raw = _qualification_bytes(Path(path), pin)
    return model.model_validate(json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant))


def _qualification_sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _qualification_write_report(path: Path, raw: bytes) -> None:
    """Durable staging plus no-replace hard-link publication; identical retries only.

    The existing generic os.replace writer cannot enforce immutability against
    two simultaneous publishers. Linking a complete staging file is atomic and
    fails if another writer wins. Failed directory fsync never returns success.
    """
    if len(raw) > QUALIFICATION_MAX_BYTES:
        raise ValueError("qualification report exceeds bound")
    path = _qualification_path(path)
    if not path.parent.is_dir():
        raise ValueError("qualification output directory must already exist")
    fd, name = tempfile.mkstemp(prefix=".qualification-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _qualification_path(path)
        try:
            os.link(temp, path)
        except FileExistsError:
            if _qualification_bytes(path) != raw:
                raise ValueError("different qualification report already published")
            # Re-establish durability for a previous lost acknowledgement.
            with path.open("r+b") as stream:
                os.fsync(stream.fileno())
        _qualification_sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)
        _qualification_sync_directory(path.parent)


def qualification_command(args):
    """Evaluate operator-pinned files; no source lookup or implicit registration."""
    from vkm_evidence.qualification import (FrozenPlan, FrozenRegistration, GoldSet,
        MatchAdjudication, PredictionSet, evaluate_qualification)
    try:
        if not args.plan:
            print(json.dumps({"status": "NOT_RUN", "qualification": "NOT_QUALIFIED",
                              "reason": "APPROVED_PLAN_NOT_SUPPLIED", "production_ready": False}))
            return 2
        if not args.output:
            raise ValueError("immutable report output required")
        models = {"plan": FrozenPlan, "gold": GoldSet, "predictions": PredictionSet,
                  "adjudication": MatchAdjudication, "registration": FrozenRegistration}
        values = {key: _qualification_model(getattr(args, key), getattr(args, key + "_sha256"), model)
                  for key, model in models.items()}
        trusted = _qualification_model(args.trusted_registration_receipt, args.trusted_registration_sha256,
                                       TrustedQualificationRegistration)
        if bool(trusted) != bool(args.trusted_registrar):
            raise ValueError("trusted receipt requires operator-approved registrar")
        verified = False
        def verify_registration(registration):
            nonlocal verified
            verified = bool(trusted is not None and
                trusted.registrar == args.trusted_registrar == registration.registrar and
                trusted.registered_at == registration.registered_at and
                trusted.plan_sha256 == registration.plan_sha256 == values["plan"].sha256 and
                trusted.gold_sha256 == registration.gold_sha256 == record_hash(values["gold"]) and
                registration.durable_receipt_sha256 == args.trusted_registration_sha256)
            return verified
        report = evaluate_qualification(values["plan"], values["gold"], values["predictions"],
            values["adjudication"], values["registration"], verify_registration=verify_registration)
        report["operator_verification"] = {
            "registration_trust": "OPERATOR_PINNED_RECEIPT" if verified else "NOT_VERIFIED",
            "trust_basis": "EXTERNALLY_APPROVED_RECEIPT_AND_REGISTRAR",
            "append_only_ancestry": "OPERATOR_ATTESTED_NOT_AUTOMATICALLY_PROVEN" if verified else "NOT_VERIFIED",
            "trusted_receipt_file_sha256": args.trusted_registration_sha256,
            "input_file_sha256": {key: getattr(args, key + "_sha256") for key in models},
            "file_durability": "FSYNC",
            "directory_durability": "NOT_QUALIFIED" if os.name == "nt" else "FSYNC",
            "power_loss_qualification": "NOT_RUN",
        }
        # The operator binding is covered by the same report hash as all metrics.
        report.pop("report_sha256")
        report["report_sha256"] = record_hash(report)
        raw = canonical_bytes(report) + b"\n"
        _qualification_write_report(Path(args.output), raw)
        # Operational logs omit source IDs, locators, actors, values and paths.
        print(json.dumps({"status": report["status"], "qualification": report["qualification"],
            "population": report["population"], "report_sha256": report["report_sha256"],
            "report_file_sha256": hashlib.sha256(raw).hexdigest(),
            "registration_trust": report["operator_verification"]["registration_trust"],
            "production_ready": False, "scientific_admission": "NOT_ESTABLISHED",
            "field_validation": "NOT_ESTABLISHED"}, sort_keys=True))
        return 0 if report["status"] == "PASS" else 2
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as exc:
        print(json.dumps({"status": "FAILED", "qualification": "NOT_QUALIFIED",
                          "error_type": type(exc).__name__, "production_ready": False}))
        return 1


def _context(args):
    return AccessContext.model_validate_json(Path(args.context).read_bytes())


def _root(args):
    from vkm_corpus.config import load_settings
    return load_settings().require_data_root() / "canonical" / "evidence"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rebase_command(args):
    """Plan (read-only) or publish (owner-approved) re-anchoring onto a new canonical snapshot.

    Canons are explicit read-only DuckDB files; the journal root defaults to the
    configured runtime. Printed output carries counts, hashes and reason codes only.
    """
    from vkm_corpus.api.canon import CanonStore
    from vkm_corpus.contracts.policy_store import SourcePolicyStore
    from vkm_evidence import rebase as rb
    from vkm_evidence.objects import canonical_resolver

    try:
        context = _context(args)
        store = SourcePolicyStore(Path(args.policy), lambda: ())
        journal_root = Path(args.journal_root) if args.journal_root else _root(args)
        opened: dict[Path, CanonStore] = {}

        def canon(path):
            key = Path(path).resolve()
            if not key.is_file():
                raise rb.RebaseBlocked("CANON_FILE_MISSING")
            return opened.setdefault(key, CanonStore(key))

        target = canon(args.new_canon)
        sources = [canon(p) for p in args.old_canon]
        inputs = {"source_policy_file": _file_sha256(Path(args.policy)),
                  "target_canon_duckdb": _file_sha256(Path(args.new_canon).resolve())}
        for path, store_ in zip(args.old_canon, sources):
            if store_ is not target:
                inputs["source_canon_duckdb:" + str(store_.snapshot_id())] = _file_sha256(Path(path).resolve())

        def plan(policy, recorded_at, revision=None):
            return rb.plan_rebase(EvidenceJournal(journal_root), canons=sources, target=target, policy=policy,
                                  source_policy=store.for_source, context=context, recorded_at=recorded_at,
                                  target_snapshot_id=args.new_snapshot_id, revision=revision)

        if args.command == "rebase-plan":
            recorded_at = datetime.fromisoformat(args.recorded_at) if args.recorded_at else datetime.now(timezone.utc)
            if recorded_at.tzinfo is None:
                raise rb.RebaseBlocked("REBASE_TIMESTAMP_REQUIRES_TIMEZONE")
            policy = rb.RebasePolicy(fuzzy_threshold=args.fuzzy_threshold, fuzzy_min_chars=args.fuzzy_min_chars,
                                     not_found=args.not_found)
            planned = plan(policy, recorded_at)
            receipt = rb.write_outputs(planned, Path(args.output_dir), inputs_sha256=inputs,
                                       public_dir=Path(args.public_output_dir) if args.public_output_dir else None)
            result = {key: receipt[key] for key in ("schema", "status", "journal_base_revision", "to_snapshot",
                                                    "from_snapshots", "counts", "per_source", "outputs",
                                                    "artifacts_sha256")}
            result["held_to_acknowledge"] = rb.held_count(planned)
            result["not_found_repointed_to_acknowledge"] = rb.not_found_repointed(planned)
        else:
            planned = rb.RebasePlan.model_validate_json(Path(args.plan).read_bytes())
            approval = rb.RebaseApproval.model_validate_json(_qualification_bytes(Path(args.approval),
                                                                                  args.approval_sha256))
            owners = frozenset(json.loads(Path(args.owners).read_bytes()))
            journal = EvidenceJournal(journal_root, object_validator=canonical_resolver(target, store.for_source))
            result = rb.publish_rebase(journal, planned, approval, owners=owners, context=context,
                                       replan=lambda: plan(planned.policy, planned.recorded_at, planned.base_revision))
            from vkm_evidence.migration import _path, _write_once
            _write_once(_path(Path(args.plan).absolute().parent, "publish-receipt.json"), canonical_bytes(result))
    except rb.RebaseBlocked as exc:
        # Stable reason codes only; never record values, quotes or paths.
        print(json.dumps({"status": "BLOCKED", "code": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    return 0


def command(args):
    if args.command == "qualification":
        return qualification_command(args)
    if args.command in {"rebase-plan", "rebase-publish"}:
        return rebase_command(args)
    if args.command == "validate-batch":
        batch = EvidenceBatch.model_validate_json(Path(args.input).read_bytes())
        result = {"status": "SCHEMA_VALID", "records": len(batch.records), "scientific_admission": "NOT_RUN"}
    elif args.command == "coverage":
        result = CoverageLedger.model_validate_json(Path(args.input).read_bytes()).report()
    elif args.command.startswith("inventory-"):
        from vkm_corpus.api.canon import CanonStore
        from vkm_corpus.config import load_settings
        from vkm_corpus.contracts.policy_store import SourcePolicyStore
        from vkm_evidence.inventory import inventory_summary, inventory_source
        canon = CanonStore.from_data_root(load_settings().require_data_root())
        policy = SourcePolicyStore(Path(args.policy), lambda: [r["source_id"] for r in canon.query("SELECT source_id FROM sources")])
        context = _context(args)
        if args.command == "inventory-summary":
            policy.require_served_corpus(context)
            result = inventory_summary(canon)
        else:
            policy.for_source(args.source_id).require(context)
            result = inventory_source(canon, args.campaign_sha256, args.source_id)
    else:
        root = _root(args)
        if args.command == "publish":
            from vkm_corpus.api.canon import CanonStore
            from vkm_corpus.config import load_settings
            from vkm_corpus.contracts.policy_store import SourcePolicyStore
            from vkm_evidence.objects import canonical_resolver
            data_root = load_settings().require_data_root()
            canon = CanonStore.from_data_root(data_root)
            policy = SourcePolicyStore(Path(args.policy), lambda: ())
            reviewers = frozenset(json.loads(Path(args.reviewers).read_bytes())) if args.reviewers else frozenset()
            journal = EvidenceJournal(root, object_validator=canonical_resolver(canon, policy.for_source), reviewers=reviewers)
            result = journal.publish(args.request_id, args.base_revision,
                EvidenceBatch.model_validate_json(Path(args.input).read_bytes()), _context(args))
        elif args.command == "status":
            journal = EvidenceJournal(root)
            records = journal.records()
            result = {"revision": journal.revision, "logical_records": len(records), "commits": len(journal.commits()),
                      "status": "JOURNAL_VALID", "scientific_admission": "PER_RECORD_ONLY"}
        else:
            from vkm_corpus.contracts.policy_store import SourcePolicyStore
            policy = SourcePolicyStore(Path(args.policy), lambda: ()) if args.policy else None
            reader = EvidenceReader(EvidenceJournal(root), source_policy=policy.for_source if policy else None)
            context = _context(args)
            if args.command == "get":
                result = reader.get(args.record_id, context)
            elif args.command == "dependencies":
                result = reader.dependencies(args.record_id, context, cursor=args.cursor, limit=args.limit)
            elif args.command == "project":
                from vkm_evidence.projections import build_projection
                result = build_projection(reader, Path(args.output), context)
            elif args.command == "export":
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                cursor, count, generation = None, 0, None
                # Exclusive output preserves any prior user export. The receipt
                # is written only after the complete, generation-bound traversal.
                with output.open("xb") as stream:
                    while True:
                        page = reader.page(context, cursor=cursor, limit=500)
                        generation = page["generation"]
                        for record in page["items"]:
                            stream.write(canonical_bytes(record) + b"\n")
                            count += 1
                        if not page["has_more"]:
                            break
                        cursor = page["next_cursor"]
                    stream.flush()
                    import os
                    os.fsync(stream.fileno())
                from vkm_corpus.parquet.atomic import sha256_of, write_bytes
                result = {"schema": "vkm-evidence-export/1", "status": "COMPLETE", "generation": generation,
                          "records": count, "output_sha256": sha256_of(output)}
                write_bytes(output.parent / "tmp", output.with_suffix(output.suffix + ".manifest.json"), canonical_bytes(result))
            else:
                raise ValueError("unknown evidence command")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=1))
    if args.command in {"inventory-source", "inventory-summary"} and result.get("status") in {"INCOMPLETE", "INTEGRITY_FAILURE"}:
        return 1
    return 0


def safe_command(args):
    try:
        return command(args)
    except (ValueError, OSError, PermissionError, KeyError) as exc:
        # Never echo private source text or user-supplied paths into operational logs.
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


def configure(parser):
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("qualification", help="evaluate pinned artifacts; missing inputs are NOT_RUN",
                       description="Evaluate operator-pinned artifacts. Missing inputs are NOT_RUN; help performs no evaluation.")
    for name in ("plan", "gold", "predictions", "adjudication", "registration"):
        p.add_argument("--" + name, help="bounded JSON artifact; source values stay out of logs")
        p.add_argument("--" + name + "-sha256", help="operator-approved SHA-256 of exact file bytes")
    p.add_argument("--trusted-registration-receipt", help="separate externally approved durable preregistration receipt")
    p.add_argument("--trusted-registration-sha256", help="operator-supplied SHA-256 of trusted receipt bytes")
    p.add_argument("--trusted-registrar", help="registrar explicitly approved by the operator")
    p.add_argument("--output", help="immutable report file in an existing controlled directory")
    p.set_defaults(func=safe_command)
    for name in ("validate-batch", "coverage"):
        p = sub.add_parser(name)
        p.add_argument("--input", required=True)
        p.set_defaults(func=safe_command)
    sub.add_parser("status").set_defaults(func=safe_command)
    for name in ("inventory-summary", "inventory-source"):
        p = sub.add_parser(name)
        p.add_argument("--context", required=True)
        p.add_argument("--policy", required=True)
        if name == "inventory-source":
            p.add_argument("--source-id", required=True)
            p.add_argument("--campaign-sha256", required=True)
        p.set_defaults(func=safe_command)
    for name in ("get", "dependencies"):
        p = sub.add_parser(name)
        p.add_argument("record_id")
        p.add_argument("--context", required=True)
        p.add_argument("--policy", help="live source policy; required for qualified production exports")
        if name == "dependencies":
            p.add_argument("--cursor")
            p.add_argument("--limit", type=int, default=500)
        p.set_defaults(func=safe_command)
    for name in ("export", "project"):
        p = sub.add_parser(name)
        p.add_argument("--context", required=True)
        p.add_argument("--output", required=True)
        p.add_argument("--policy", help="live source policy; required for qualified production exports")
        p.set_defaults(func=safe_command)
    p = sub.add_parser("publish")
    for arg in ("context", "input", "request-id", "base-revision", "policy"):
        p.add_argument("--" + arg, required=True)
    p.add_argument("--reviewers")
    p.set_defaults(func=safe_command)
    for name in ("rebase-plan", "rebase-publish"):
        p = sub.add_parser(name, help="re-anchor evidence supports onto a new canonical snapshot (revision+1)")
        p.add_argument("--journal-root", help="evidence journal root; default: configured runtime data root")
        p.add_argument("--old-canon", action="append", required=True,
                       help="read-only DuckDB of a snapshot the current supports cite (repeatable)")
        p.add_argument("--new-canon", required=True, help="read-only DuckDB of the target snapshot")
        p.add_argument("--new-snapshot-id", required=True)
        p.add_argument("--policy", required=True, help="live source policy inventory")
        p.add_argument("--context", required=True, help="publisher AccessContext JSON")
        if name == "rebase-plan":
            p.add_argument("--output-dir", required=True, help="PRIVATE package directory outside the PUBLIC checkout")
            p.add_argument("--public-output-dir", help="optional copy of the public-safe receipt and review list")
            p.add_argument("--recorded-at", help="ISO timestamp with timezone (default: now)")
            p.add_argument("--not-found", choices=("HOLD", "REPOINT_IF_BASELINE_ABSENT", "REPOINT"), default="HOLD")
            p.add_argument("--fuzzy-threshold", type=float, default=90.0)
            p.add_argument("--fuzzy-min-chars", type=int, default=24)
        else:
            p.add_argument("--plan", required=True, help="plan.json of the PRIVATE package")
            p.add_argument("--approval", required=True, help="owner RebaseApproval JSON")
            p.add_argument("--approval-sha256", required=True, help="operator-pinned SHA-256 of the approval bytes")
            p.add_argument("--owners", required=True, help="operator-configured JSON list of owner authorities")
        p.set_defaults(func=safe_command)


def register(subparsers):
    configure(subparsers.add_parser("evidence", help="durable evidence/review journal and complete exports"))


def main(argv=None):
    p = argparse.ArgumentParser(prog="vkm-evidence")
    configure(p)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, OSError, PermissionError) as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
