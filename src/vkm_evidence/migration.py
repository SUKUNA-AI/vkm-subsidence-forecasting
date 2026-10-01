"""Bounded, source-backed Phase-1 migration. Planning never publishes evidence.

All plan/archive bytes are PRIVATE. Old catalogue flags are not review authority.
The operator supplies current policies, an actual CanonStore and approved plan
hashes; none of these trust roots is inferred from the candidate input rows.
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Callable, Literal, Mapping

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.api.errors import ApiFailure
from vkm_evidence.contracts import (Claim, EvidenceBatch, Identifier, ObjectRef,
    ReviewDecision, Sha256, StrictModel, VersionRef, canonical_bytes, record_hash)
from vkm_evidence.journal import EvidenceJournal, MAX_BATCH_BYTES, MAX_BATCH_RECORDS
from vkm_evidence.objects import canonical_resolver
from vkm_world.core.provenance import (EpistemicStatus, EvidenceType, Provenance,
                                     Scale, Scope, SourceRef)

STREAMS = frozenset({"BOREHOLES", "CITATIONS", "EXTERNAL", "FORMULAS", "GEOLOGY_COORDS",
    "HYDRO_THERMAL_GEOPHYS", "MECH_RHEO", "MINING", "MONITORING_LIFECYCLE",
    "OCR_VISUAL_QA", "PHYSICS_CAUSAL", "SOURCES"})
RULE_VERSION = "phase1-archival-claim/1"
SOURCE_ID = re.compile(r"(?:VKM-SRC-\d{3,}|EXT-SRC-[A-Za-z0-9_.-]+|EXTWEB-[A-Za-z0-9_.-]+)\Z")


class MigrationBlocked(ValueError):
    """Public exceptions carry stable codes, never row/quote/path values."""


class MigrationBounds(StrictModel):
    max_files: int = Field(32, ge=1, le=256)
    max_total_bytes: int = Field(8 * 1024 * 1024, ge=1, le=MAX_BATCH_BYTES)
    max_rows: int = Field(2048, ge=1, le=MAX_BATCH_RECORDS)
    max_columns: int = Field(512, ge=1, le=2048)
    max_cell_chars: int = Field(100_000, ge=1, le=100_000)


class CatalogueSpec(StrictModel):
    """Explicit column mapping, never a guess from a filename or cell contents.

    statement_column=None deliberately archives otherwise unmappable catalogues
    as UNRESOLVED. Source/locator fields may contain multiple old references; the
    exact old locator string remains in the row, while owner bindings resolve it.
    """
    path: str
    id_column: str
    source_column: str = "source_id"
    locator_column: str = "locator"
    statement_column: str | None = "quote"
    quote_column: str | None = "quote"
    status_column: str = "status"
    scope_column: str = "scope"
    scale_column: str = "scale"
    evidence_type_column: str | None = "evidence_type"
    method_column: str | None = None
    inputs_column: str | None = None
    rationale_column: str | None = None

    @field_validator("path")
    @classmethod
    def _path(cls, value):
        parts = value.split("/")
        if (len(parts) < 2 or parts[0] not in STREAMS or not value.endswith(".csv")
                or "\\" in value or ":" in value or any(x in {"", ".", ".."} for x in parts)
                or any(ord(c) < 32 for c in value)
                or any(x.endswith((".", " ")) for x in parts)):
            raise ValueError("CURRENT_CANONICAL_CSV_REQUIRED")
        return value


class FrozenCatalogue(StrictModel):
    spec: CatalogueSpec
    sha256: Sha256
    size_bytes: int = Field(ge=0)
    header: tuple[str, ...]
    row_count: int = Field(ge=0)


class FrozenMigrationInputs(StrictModel):
    schema_version: Literal["vkm-phase1-migration-inputs/1"] = "vkm-phase1-migration-inputs/1"
    files: tuple[FrozenCatalogue, ...]
    catalogue_policy: ResourcePolicy
    bounds: MigrationBounds

    @model_validator(mode="after")
    def _unique(self):
        if not self.files or len({f.spec.path.casefold() for f in self.files}) != len(self.files):
            raise ValueError("MIGRATION_INPUT_PATHS_EMPTY_OR_DUPLICATE")
        return self


class RowBinding(StrictModel):
    row_sha256: Sha256
    supports: tuple[ObjectRef, ...] = Field(min_length=1, max_length=64)
    historical_quote_field: Literal["VERBATIM", "PARAPHRASE", "UNSPECIFIED", "NONE"] = "UNSPECIFIED"
    # Exact existing records only. No creation of an observation family/identity.
    dependencies: tuple[VersionRef, ...] = ()


class ArchivedRow(StrictModel):
    path: str
    file_sha256: Sha256
    row_number: int = Field(ge=1)  # one-based data row, not a physical line in multiline CSV
    old_id: str | None
    values: tuple[str, ...]  # zipped with frozen header; preserves empty strings and order

    @property
    def row_sha256(self):
        return record_hash(self)


class MigrationRow(StrictModel):
    row_sha256: Sha256
    outcome: Literal["MIGRATABLE", "UNRESOLVED"]
    reasons: tuple[str, ...]
    record: Claim | None = None
    semantic_state: Literal["NEEDS_SEMANTIC_REVIEW"] = "NEEDS_SEMANTIC_REVIEW"
    historical_quote_field: Literal["VERBATIM", "PARAPHRASE", "UNSPECIFIED", "NONE"] = "UNSPECIFIED"


class MigrationPlan(StrictModel):
    schema_version: Literal["vkm-phase1-migration-plan/1"] = "vkm-phase1-migration-plan/1"
    rule_version: Literal["phase1-archival-claim/1"] = RULE_VERSION
    inputs: FrozenMigrationInputs
    bindings: tuple[RowBinding, ...]
    base_revision: Sha256
    recorded_at: datetime
    actor: str
    output_policy: ResourcePolicy
    archive: tuple[ArchivedRow, ...]
    rows: tuple[MigrationRow, ...]
    source_policies: dict[str, ResourcePolicy]
    canonical_snapshot_sha256: Sha256
    review_decisions: tuple[ReviewDecision, ...] = ()
    scientific_admission: Literal["NOT_ESTABLISHED"] = "NOT_ESTABLISHED"

    @field_validator("recorded_at")
    @classmethod
    def _aware(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MIGRATION_TIMESTAMP_REQUIRES_TIMEZONE")
        return value.astimezone(timezone.utc)

    @property
    def sha256(self):
        return record_hash(self)


class MigrationApproval(StrictModel):
    """Trusted operator configuration, not a self-approved publish request."""
    plan_sha256: Sha256
    base_revision: Sha256
    publisher: str
    authority: str
    allow_unresolved: bool = False


def _private_policy(policy, context):
    if policy.access_class == "PUBLIC":
        raise MigrationBlocked("PRIVATE_MIGRATION_STORAGE_REQUIRED")
    policy.require(context)


def _path(root: Path, relative: str) -> Path:
    root = Path(root).absolute()
    candidate = root / relative
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise MigrationBlocked("MIGRATION_PATH_ESCAPE")
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise MigrationBlocked("MIGRATION_LINK_REJECTED")
    return candidate


def _canonical_root(root):
    root = Path(root).absolute()
    _path(root, ".")
    if root.name != "canonical" or root.parent.name != "11_evidence_vnext":
        raise MigrationBlocked("CURRENT_PHASE1_CANONICAL_ROOT_REQUIRED")
    return root


def _read_csv(root, spec, bounds):
    path = _path(root, spec.path)
    try:
        if not path.is_file() or path.stat().st_size > bounds.max_total_bytes:
            raise MigrationBlocked("MIGRATION_INPUT_SIZE_OR_FILE_INVALID")
        with path.open("rb") as stream:
            raw = stream.read(bounds.max_total_bytes + 1)
        if len(raw) > bounds.max_total_bytes:
            raise MigrationBlocked("MIGRATION_INPUT_SIZE_LIMIT")
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        header = tuple(next(reader))
        if (not header or len(header) > bounds.max_columns or any(not c for c in header)
                or len(set(header)) != len(header)):
            raise MigrationBlocked("MIGRATION_HEADER_INVALID")
        rows = []
        for values in reader:
            if len(rows) >= bounds.max_rows:
                raise MigrationBlocked("MIGRATION_ROW_LIMIT")
            if len(values) != len(header) or any(len(c) > bounds.max_cell_chars for c in values):
                raise MigrationBlocked("MIGRATION_ROW_WIDTH_OR_CELL_LIMIT")
            rows.append(tuple(values))
        return raw, header, tuple(rows)
    except MigrationBlocked:
        raise
    except (OSError, UnicodeError, csv.Error, StopIteration):
        raise MigrationBlocked("MIGRATION_CSV_UNREADABLE") from None


def freeze_inputs(canonical_root: Path, specs: tuple[CatalogueSpec, ...], *,
                  catalogue_policy: ResourcePolicy, context: AccessContext,
                  bounds: MigrationBounds | None = None) -> FrozenMigrationInputs:
    """Read-only, explicit bounded catalogue selection; no discovery of raw sources."""
    _private_policy(catalogue_policy, context)  # before paths, headers or counts
    bounds = bounds or MigrationBounds()
    root = _canonical_root(canonical_root)
    if not specs or len(specs) > bounds.max_files:
        raise MigrationBlocked("MIGRATION_FILE_LIMIT")
    if len({s.path.casefold() for s in specs}) != len(specs):
        raise MigrationBlocked("MIGRATION_DUPLICATE_INPUT")
    total_bytes = total_rows = 0
    files = []
    for spec in specs:
        raw, header, rows = _read_csv(root, spec, bounds)
        total_bytes += len(raw)
        total_rows += len(rows)
        if total_bytes > bounds.max_total_bytes or total_rows > bounds.max_rows:
            raise MigrationBlocked("MIGRATION_TOTAL_LIMIT")
        files.append(FrozenCatalogue(spec=spec, sha256=hashlib.sha256(raw).hexdigest(),
                                    size_bytes=len(raw), header=header, row_count=len(rows)))
    return FrozenMigrationInputs(files=tuple(files), catalogue_policy=catalogue_policy, bounds=bounds)


def _load_rows(root, frozen):
    archive = []
    if sum(f.size_bytes for f in frozen.files) > frozen.bounds.max_total_bytes:
        raise MigrationBlocked("MIGRATION_TOTAL_LIMIT")
    for file in frozen.files:
        raw, header, rows = _read_csv(root, file.spec, frozen.bounds)
        if (hashlib.sha256(raw).hexdigest() != file.sha256 or len(raw) != file.size_bytes
                or header != file.header or len(rows) != file.row_count):
            raise MigrationBlocked("FROZEN_MIGRATION_INPUT_CHANGED")
        id_index = header.index(file.spec.id_column) if file.spec.id_column in header else None
        for number, values in enumerate(rows, 1):
            if len(archive) >= frozen.bounds.max_rows:
                raise MigrationBlocked("MIGRATION_TOTAL_LIMIT")
            archive.append(ArchivedRow(path=file.spec.path, file_sha256=file.sha256, row_number=number,
                old_id=values[id_index] if id_index is not None else None, values=values))
    if len(archive) > frozen.bounds.max_rows or sum(f.size_bytes for f in frozen.files) > frozen.bounds.max_total_bytes:
        raise MigrationBlocked("MIGRATION_TOTAL_LIMIT")
    return tuple(archive)


def _source_ids(text):
    values = tuple(x.strip() for x in re.split(r"[;,|]", text) if x.strip())
    if not values or any(not SOURCE_ID.fullmatch(x) for x in values):
        raise MigrationBlocked("HISTORICAL_SOURCE_IDS_UNRESOLVED")
    return frozenset(values)


def plan_migration(canonical_root: Path, inputs: FrozenMigrationInputs, bindings: tuple[RowBinding, ...], *,
                   canon, source_policy: Callable[[str], ResourcePolicy], context: AccessContext,
                   catalogue_policy: ResourcePolicy, output_policy: ResourcePolicy,
                   recorded_at: datetime, base_revision: str,
                   review_decisions: tuple[ReviewDecision, ...] = ()) -> MigrationPlan:
    """Plan exact archival Claims; no inferred observation, review, time or quantity.

    Current source policies are checked before canonical lookup. The catalogue
    itself requires a separately configured private policy before any CSV read.
    """
    _private_policy(catalogue_policy, context)
    _private_policy(output_policy, context)
    if catalogue_policy != inputs.catalogue_policy or not output_policy.preserves(catalogue_policy):
        raise MigrationBlocked("MIGRATION_CATALOGUE_POLICY_CHANGED_OR_WIDENED")
    if len(inputs.files) > inputs.bounds.max_files:
        raise MigrationBlocked("MIGRATION_FILE_LIMIT")
    root = _canonical_root(canonical_root)
    archive = _load_rows(root, inputs)
    binding_map = {b.row_sha256: b for b in bindings}
    if len(binding_map) != len(bindings) or set(binding_map) - {r.row_sha256 for r in archive}:
        raise MigrationBlocked("MIGRATION_BINDINGS_DUPLICATE_OR_DANGLING")
    files = {f.spec.path: f for f in inputs.files}
    id_counts = {}
    for row in archive:
        id_counts[row.old_id] = id_counts.get(row.old_id, 0) + 1
    policies = {}
    snapshot_sha = record_hash(asdict(canon.snapshot()))

    def authorize(sid):
        try:
            policy = source_policy(sid)
        except KeyError:
            raise MigrationBlocked("MIGRATION_SOURCE_POLICY_UNCLASSIFIED") from None
        if not isinstance(policy, ResourcePolicy):
            raise MigrationBlocked("MIGRATION_SOURCE_POLICY_UNCLASSIFIED")
        policy.require(context)
        if not output_policy.preserves(policy):
            raise MigrationBlocked("MIGRATION_SOURCE_POLICY_WIDENING")
        if sid in policies and policies[sid] != policy:
            raise MigrationBlocked("MIGRATION_SOURCE_POLICY_CHANGED")
        policies[sid] = policy
        return policy

    # A row without a binding is still copied into the PRIVATE archive. Its
    # declared sources therefore need current authorization as well; otherwise
    # an UNRESOLVED row could bypass target/source policy and leak through rows.json.
    # The separately trusted catalogue policy covers genuinely unclassified old
    # attribution strings; no source ID is guessed from free prose.
    for archived in archive:
        file = files[archived.path]
        data = dict(zip(file.header, archived.values))
        # An unresolved attribution may mix a known source with an unknown
        # token. The known source still requires authorization before archival;
        # rejecting the whole attribution must not hide that source's policy.
        declared = {token.strip() for token in re.split(r"[;,|]", data.get(file.spec.source_column, ""))
                    if SOURCE_ID.fullmatch(token.strip())}
        for sid in sorted(declared):
            authorize(sid)
    resolve = canonical_resolver(canon, authorize)
    rows = []
    for archived in archive:
        file = files[archived.path]
        spec = file.spec
        data = dict(zip(file.header, archived.values))
        binding = binding_map.get(archived.row_sha256)
        reasons = []
        record = None
        quote_mode = binding.historical_quote_field if binding else "UNSPECIFIED"
        try:
            if not archived.old_id:
                raise MigrationBlocked("HISTORICAL_ID_MISSING")
            if id_counts[archived.old_id] != 1:
                raise MigrationBlocked("HISTORICAL_ID_DUPLICATE")
            try:
                TypeAdapter(Identifier).validate_python(archived.old_id)
            except ValidationError:
                raise MigrationBlocked("HISTORICAL_ID_NOT_REPRESENTABLE") from None
            if not binding:
                raise MigrationBlocked("ORIGINAL_BINDING_MISSING")
            required = (spec.source_column, spec.locator_column, spec.status_column, spec.scope_column, spec.scale_column)
            if spec.statement_column is None or any(c not in data for c in (*required, spec.statement_column)):
                raise MigrationBlocked("HISTORICAL_SCHEMA_MAPPING_UNRESOLVED")
            if not data[spec.locator_column].strip():
                raise MigrationBlocked("HISTORICAL_LOCATOR_MISSING")
            if _source_ids(data[spec.source_column]) != {s.source_id for s in binding.supports}:
                raise MigrationBlocked("ORIGINAL_BINDING_SOURCE_MISMATCH")
            # Every original reference is independently verified; same source
            # repeated in several rows never becomes independent validation.
            try:
                for support in binding.supports:
                    resolve(support)
            except PermissionError:
                raise
            except MigrationBlocked:
                raise
            except (ValueError, KeyError, OSError, ApiFailure):
                raise MigrationBlocked("ORIGINAL_BINDING_UNRESOLVED") from None
            quote = data.get(spec.quote_column, "") if spec.quote_column else ""
            if quote and quote_mode == "VERBATIM":
                quote_sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
                if not any(s.char_start is not None and s.fragment_sha256 == quote_sha
                           and s.char_end - s.char_start == len(quote) for s in binding.supports):
                    raise MigrationBlocked("HISTORICAL_QUOTE_FRAGMENT_MISMATCH")
            elif quote and quote_mode != "PARAPHRASE":
                raise MigrationBlocked("HISTORICAL_QUOTE_SEMANTICS_UNSPECIFIED")
            elif not quote and quote_mode != "NONE":
                raise MigrationBlocked("HISTORICAL_QUOTE_MODE_MISMATCH")
            statement = data[spec.statement_column]
            if not statement.strip():
                raise MigrationBlocked("HISTORICAL_STATEMENT_EMPTY")
            try:
                scope, scale = Scope(data[spec.scope_column]), Scale(data[spec.scale_column])
            except ValueError:
                raise MigrationBlocked("HISTORICAL_SCOPE_OR_SCALE_UNRESOLVED") from None
            try:
                status = EpistemicStatus(data[spec.status_column])
            except ValueError:
                status = EpistemicStatus.UNKNOWN
                reasons.append("HISTORICAL_STATUS_UNMAPPED_RETAINED_AS_UNKNOWN")
            evidence_type = data.get(spec.evidence_type_column, "") if spec.evidence_type_column else ""
            try:
                evidence_type = EvidenceType(evidence_type) if evidence_type else EvidenceType.NOT_APPLICABLE
            except ValueError:
                raise MigrationBlocked("HISTORICAL_EVIDENCE_TYPE_UNRESOLVED") from None
            historical_inputs = data.get(spec.inputs_column, "") if spec.inputs_column else ""
            input_ids = tuple(x.strip() for x in re.split(r"[;,|]", historical_inputs) if x.strip())
            if input_ids and set(input_ids) != {d.record_id for d in binding.dependencies}:
                raise MigrationBlocked("HISTORICAL_DEPENDENCIES_UNRESOLVED")
            provenance = Provenance(status=status, scope=scope, scale=scale, evidence_type=evidence_type,
                sources=tuple(SourceRef(source_id=s.source_id, locator=s.locator) for s in binding.supports),
                method=data.get(spec.method_column) if spec.method_column else None, inputs=input_ids,
                rationale=data.get(spec.rationale_column) if spec.rationale_column else None,
                notes="Historical Phase-1 attribution; semantic correctness has not been established.")
            record = Claim(record_id=archived.old_id, recorded_at=recorded_at, actor=context.principal,
                policy=output_policy, supports=binding.supports, depends_on=binding.dependencies,
                proposition=statement, attribution="Phase-1 canonical catalogue transcription",
                polarity="UNCERTAIN", modality="REPORTED", provenance=provenance,
                qualifiers=("UNREVIEWED_HISTORICAL_CATALOGUE", "archive_row_sha256:" + archived.row_sha256,
                            "historical_quote_field:" + quote_mode))
        except PermissionError:
            # Do not return counts or partial payloads if any source becomes denied.
            raise PermissionError("RESOURCE_POLICY_DENIED") from None
        except MigrationBlocked as exc:
            reasons.append(str(exc))
        except ValidationError:
            reasons.append("HISTORICAL_PROVENANCE_NOT_REPRESENTABLE")
        rows.append(MigrationRow(row_sha256=archived.row_sha256, outcome="MIGRATABLE" if record else "UNRESOLVED",
                                 reasons=tuple(reasons), record=record, historical_quote_field=quote_mode))
    for sid, policy in policies.items():
        if source_policy(sid) != policy:
            raise MigrationBlocked("MIGRATION_SOURCE_POLICY_CHANGED")
    records = {r.record.record_id: r.record for r in rows if r.record is not None}
    for review in review_decisions:
        target = records.get(review.target.record_id)
        if target is None or review.target != target.version_ref or review.actor != context.principal:
            raise MigrationBlocked("MIGRATION_REVIEW_BINDING_INVALID")
        if (review.decision not in {"VERIFIED_TRANSCRIPTION", "SEMANTIC_REVIEWED"}
                or not review.source_verified or review.reviewer_authority != context.principal
                or not set(target.supports).issubset(review.supports)
                or not review.policy.preserves(output_policy)):
            raise MigrationBlocked("MIGRATION_REVIEW_BINDING_INVALID")
        for support in review.supports:
            resolve(support)
    if record_hash(asdict(canon.snapshot())) != snapshot_sha:
        raise MigrationBlocked("MIGRATION_CANONICAL_SNAPSHOT_CHANGED")
    for sid, policy in policies.items():
        if source_policy(sid) != policy:
            raise MigrationBlocked("MIGRATION_SOURCE_POLICY_CHANGED")
    plan = MigrationPlan(inputs=inputs, bindings=bindings, base_revision=base_revision, recorded_at=recorded_at,
        actor=context.principal, output_policy=output_policy, archive=archive, rows=tuple(rows),
        source_policies=policies, canonical_snapshot_sha256=snapshot_sha, review_decisions=review_decisions)
    batch = _batch(plan)
    if len(batch.records) > MAX_BATCH_RECORDS or len(canonical_bytes(batch)) > MAX_BATCH_BYTES:
        raise MigrationBlocked("MIGRATION_EVIDENCE_BATCH_LIMIT")
    # Archive+plan expansion is bounded as well as original bytes.
    if len(canonical_bytes(plan)) > 4 * inputs.bounds.max_total_bytes + 1024 * 1024:
        raise MigrationBlocked("MIGRATION_PLAN_SIZE_LIMIT")
    return plan


def _batch(plan):
    try:
        return EvidenceBatch(records=tuple(r.record for r in plan.rows if r.record is not None) + plan.review_decisions)
    except ValidationError:
        raise MigrationBlocked("MIGRATION_BATCH_IDENTITY_INVALID") from None


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _mkdir(path):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(directory)
        _sync_directory(directory.parent)


def _write_once(path, data):
    """Durable atomic no-replace, including simultaneous identical replay.

    Interrupted pending files are retained; never cleanup a directory we do not
    own. File/link flush success is not physical power-loss qualification.
    """
    _path(path.parent, path.name)
    _mkdir(path.parent)
    if path.exists():
        if path.stat().st_size != len(data) or path.read_bytes() != data:
            raise MigrationBlocked("IMMUTABLE_MIGRATION_OUTPUT_CONFLICT")
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
        return
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != data:
            raise MigrationBlocked("IMMUTABLE_MIGRATION_OUTPUT_CONFLICT")
    _sync_directory(path.parent)


class MigrationPublisher:
    """Explicit publisher. Construct only from trusted operator configuration.

    `approved_plans` is a separately managed immutable configuration snapshot:
    accepting arbitrary caller-provided approvals would remove the owner gate.
    The runtime root must be PRIVATE with owner-controlled ACLs. Outputs inside
    the PUBLIC checkout are rejected, including its ignored work directories.
    """
    def __init__(self, private_root: Path, journal: EvidenceJournal, *, canon,
                 source_policy: Callable[[str], ResourcePolicy],
                 catalogue_policy: Callable[[], ResourcePolicy],
                 approved_plans: Mapping[str, MigrationApproval], owners: frozenset[str],
                 public_roots: tuple[Path, ...] = ()):
        self.root = Path(private_root).absolute()
        self.journal = journal
        self.canon = canon
        self.source_policy = source_policy
        self.catalogue_policy = catalogue_policy
        self.approved_plans = {key: MigrationApproval.model_validate_json(value.model_dump_json())
                               for key, value in approved_plans.items()}
        self.owners = frozenset(owners)
        # Package source checkout is public; callers can protect additional roots.
        self.public_roots = (Path(__file__).resolve().parents[2], *public_roots)
        for root in (self.root, journal.root):
            _path(root, ".")
            if any(root.resolve().is_relative_to(p.resolve()) for p in self.public_roots):
                raise MigrationBlocked("PRIVATE_MIGRATION_STORAGE_REQUIRED")

    def _verify(self, canonical_root, plan, context):
        # Reparse to detach mutable nested WorldSpec data, then reconstruct from
        # original CSV bytes and freshly authorized canonical objects on *every*
        # replay, including an already committed journal request.
        plan = MigrationPlan.model_validate_json(plan.model_dump_json())
        rebuilt = plan_migration(canonical_root, plan.inputs, plan.bindings, canon=self.canon,
            source_policy=self.source_policy, context=context, catalogue_policy=self.catalogue_policy(),
            output_policy=plan.output_policy, recorded_at=plan.recorded_at, base_revision=plan.base_revision,
            review_decisions=plan.review_decisions)
        if rebuilt.sha256 != plan.sha256:
            raise MigrationBlocked("MIGRATION_PLAN_NO_LONGER_REPRODUCIBLE")
        return rebuilt

    def stage(self, canonical_root: Path, plan: MigrationPlan, context: AccessContext) -> dict:
        """Persist a PRIVATE dry-run package. Does not advance evidence HEAD."""
        plan = self._verify(canonical_root, plan, context)
        folder = _path(self.root, plan.sha256)
        for file in plan.inputs.files:
            raw, _, _ = _read_csv(_canonical_root(canonical_root), file.spec, plan.inputs.bounds)
            if hashlib.sha256(raw).hexdigest() != file.sha256:
                raise MigrationBlocked("FROZEN_MIGRATION_INPUT_CHANGED")
            _write_once(_path(folder, "original-catalogues/" + file.sha256 + ".csv"), raw)
        artifacts = {
            "plan.json": canonical_bytes(plan),
            "rows.json": canonical_bytes([r.model_dump(mode="json") for r in plan.archive]),
            "map.json": canonical_bytes([{"old_id": a.old_id, "old_path": a.path, "old_row": a.row_number,
                "old_file_sha256": a.file_sha256, "row_sha256": r.row_sha256, "outcome": r.outcome,
                "reasons": r.reasons, "record": r.record.version_ref.model_dump(mode="json") if r.record else None,
                "semantic_state": r.semantic_state} for a, r in zip(plan.archive, plan.rows)]),
        }
        for name, data in artifacts.items():
            _write_once(_path(folder, name), data)
        receipt = {"schema": "vkm-phase1-migration-stage/1", "status": "PLANNED", "plan_sha256": plan.sha256,
            "artifacts": {name: hashlib.sha256(data).hexdigest() for name, data in artifacts.items()},
            "original_catalogue_sha256": [f.sha256 for f in plan.inputs.files],
            "row_count": len(plan.rows), "unresolved_count": sum(r.outcome == "UNRESOLVED" for r in plan.rows),
            "scientific_admission": "NOT_ESTABLISHED", "physical_durability": "NOT_QUALIFIED",
            "directory_fsync": "COMPLETED" if os.name == "posix" else "NOT_QUALIFIED"}
        _write_once(_path(folder, "stage-receipt.json"), canonical_bytes(receipt))
        return receipt

    def publish(self, canonical_root: Path, plan: MigrationPlan, context: AccessContext, *,
                after_commit=None) -> dict:
        """Publish only an operator-approved plan. Resume retries the same hash.

        No automatic review is generated. Explicit historical decisions must pass
        the journal's independently configured reviewer authority and exact target
        checks. A publication receipt never means scientific admission.
        """
        # Detach nested mutable provenance before the approval digest is checked.
        plan = MigrationPlan.model_validate_json(plan.model_dump_json())
        approval = self.approved_plans.get(plan.sha256)
        if (approval is None or approval.plan_sha256 != plan.sha256
                or approval.authority not in self.owners or approval.publisher != context.principal
                or approval.base_revision != plan.base_revision):
            raise MigrationBlocked("MIGRATION_OWNER_APPROVAL_REQUIRED")
        if any(r.outcome == "UNRESOLVED" for r in plan.rows) and not approval.allow_unresolved:
            raise MigrationBlocked("MIGRATION_UNRESOLVED_ROWS_REQUIRE_OWNER_ACK")
        plan = self._verify(canonical_root, plan, context)
        batch = _batch(plan)
        if not batch.records:
            raise MigrationBlocked("MIGRATION_NO_PUBLISHABLE_RECORDS")
        self.stage(canonical_root, plan, context)
        folder = _path(self.root, plan.sha256)
        _write_once(_path(folder, "owner-approval.json"), canonical_bytes(approval))
        self._verify(canonical_root, plan, context)
        # Journal does its own resolver checks before a new commit. This wrapper
        # has just reverified all supports/policies even for the journal replay path.
        committed = self.journal.publish("phase1-migration:" + plan.sha256, plan.base_revision, batch,
                                         context, after_commit=after_commit)
        self._verify(canonical_root, plan, context)
        receipt = {"schema": "vkm-phase1-migration-receipt/1", "status": "COMMITTED",
            "plan_sha256": plan.sha256, "approval_sha256": record_hash(approval), "journal": committed,
            "rows": [{"row_sha256": r.row_sha256, "outcome": "MIGRATED" if r.record else "UNRESOLVED",
                      "reasons": r.reasons, "semantic_state": r.semantic_state} for r in plan.rows],
            "scientific_admission": "NOT_ESTABLISHED", "physical_durability": "NOT_QUALIFIED",
            "directory_fsync": "COMPLETED" if os.name == "posix" else "NOT_QUALIFIED"}
        _write_once(_path(folder, "publish-receipt.json"), canonical_bytes(receipt))
        return receipt
