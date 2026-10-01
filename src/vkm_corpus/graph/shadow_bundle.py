"""Bounded, immutable DOCUMENT/NAV/EVIDENCE graph transfer artifacts.

Only fixed canonical/projector queries are used. The SQLite spool bounds Python
memory, and is a disposable projection, never authoritative evidence. Historical
supports keep their exact original payload in a separate label; they cannot
silently point at the currently served object sharing the same logical ID.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from vkm_corpus.parquet.atomic import create_exclusive, sha256_of
from vkm_evidence.contracts import ObjectRef, canonical_bytes, record_hash

FORMAT = "vkm-combined-graph-bundle/1"
KEYS = {"schema", "bundle_id", "inputs", "policy_sha256", "context_sha256", "builder",
        "nodes", "relationships", "content_sha256", "graph_sha256", "accounting", "remote_load"}


def ordinary(path):
    path = Path(path).absolute()
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (path, *path.parents)):
        raise ValueError("indirect graph artifact path")
    if not path.is_file():
        raise ValueError("graph artifact is not an ordinary file")
    import stat
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("graph artifact is not an ordinary file")
    return path


def checked_file(path, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("graph input requires exact SHA-256")
    path = ordinary(path)
    if sha256_of(path) != digest:
        raise ValueError("graph artifact byte identity mismatch")
    return path


def builder_identity():
    from importlib.metadata import version
    from vkm_corpus.graph import canon, rows, schema, nav_rows, nav_schema, preflight, common
    from vkm_evidence import objects, projections
    from vkm_corpus.duckdb import build
    from vkm_corpus.contracts import access, arrow, datasets, models
    from vkm_evidence import contracts
    modules = (canon, rows, schema, nav_rows, nav_schema, preflight, common, objects, projections,
               build, access, arrow, datasets, models, contracts)
    return {"code": {**{m.__name__: sha256_of(Path(m.__file__)) for m in modules},
                      __name__: sha256_of(Path(__file__))},
            "sql": {n: hashlib.sha256(b).hexdigest() for n, b in canon.derived_sql_files()},
            "dependencies": {n: version(n) for n in ("duckdb", "pyarrow", "pydantic", "pymorphy3")}}


@dataclass(frozen=True)
class SnapshotPin:
    root: Path
    snapshot_id: str
    manifest_sha256: str


class _Writer:
    def __init__(self, path, max_records, max_bytes):
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(fd)
        self.con = sqlite3.connect(path)
        self.con.execute("PRAGMA cache_size=-8192")
        self.con.execute("CREATE TABLE nodes (key TEXT PRIMARY KEY, labels TEXT NOT NULL, props TEXT NOT NULL)")
        self.con.execute("CREATE TABLE edges (key TEXT PRIMARY KEY, subject TEXT NOT NULL, object TEXT NOT NULL, type TEXT NOT NULL, props TEXT NOT NULL)")
        self.max_records, self.max_bytes = max_records, max_bytes
        self.count = self.size = 0

    def _budget(self, *values):
        self.count += 1
        self.size += sum(len(v.encode("utf-8")) for v in values)
        if self.count > self.max_records or self.size > self.max_bytes:
            raise ValueError("combined graph exceeds explicit spool budget")

    def node(self, key, labels, props):
        value = {**props, "_vkm_key": key}
        ls, ps = json.dumps(sorted({"VkmCombinedNode", *labels})), canonical_bytes(value).decode()
        self._budget(key, ls, ps)
        try:
            self.con.execute("INSERT INTO nodes VALUES (?,?,?)", (key, ls, ps))
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate graph node identity") from exc

    def edge(self, kind, subject, target, props):
        key = record_hash({"type": kind, "subject": subject, "object": target, "props": props})
        ps = canonical_bytes({**props, "_vkm_key": key}).decode()
        self._budget(key, subject, target, kind, ps)
        try:
            self.con.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (key, subject, target, kind, ps))
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate graph relationship identity") from exc

    def require_endpoints(self):
        bad = self.con.execute("SELECT count(*) FROM edges e LEFT JOIN nodes a ON a.key=e.subject "
            "LEFT JOIN nodes b ON b.key=e.object WHERE a.key IS NULL OR b.key IS NULL").fetchone()[0]
        if bad:
            raise ValueError("dangling DOCUMENT/NAV/EVIDENCE graph endpoint")


def node_key(label, identity):
    return label + ":" + identity


def _iter(con, kind, after="", limit=500):
    if kind == "nodes":
        rows = con.execute("SELECT key,labels,props FROM nodes WHERE key>? ORDER BY key LIMIT ?", (after, limit))
        return [{"key": r[0], "labels": json.loads(r[1]), "props": json.loads(r[2])} for r in rows]
    if kind == "edges":
        rows = con.execute("SELECT key,subject,object,type,props FROM edges WHERE key>? ORDER BY key LIMIT ?", (after, limit))
        return [{"key": r[0], "subject": r[1], "object": r[2], "type": r[3], "props": json.loads(r[4])} for r in rows]
    raise ValueError("unknown graph row kind")


def _content(con):
    h = hashlib.sha256()
    for kind in ("nodes", "edges"):
        after = ""
        while rows := _iter(con, kind, after):
            for row in rows:
                h.update(kind.encode() + b"\0" + canonical_bytes(row) + b"\n")
            after = rows[-1]["key"]
    return h.hexdigest()


class CombinedGraphBundle:
    """Read a hash-pinned prepared bundle or build it from actual immutable input files."""
    def __init__(self, root, manifest, digest):
        self.root, self._manifest_bytes, self.manifest_sha256 = Path(root), canonical_bytes(manifest), digest

    @property
    def manifest(self):
        return json.loads(self._manifest_bytes)

    @classmethod
    def read(cls, root, *, manifest_sha256):
        root = Path(root).absolute()
        path = checked_file(root / "manifest.json", manifest_sha256)
        manifest = json.loads(path.read_bytes())
        if (set(manifest) != KEYS or manifest["schema"] != FORMAT or manifest["remote_load"] != "NOT_RUN"
                or set(manifest["inputs"]) != {"DOCUMENT", "NAV", "EVIDENCE", "historical"}):
            raise ValueError("unsupported combined graph bundle manifest")
        db = checked_file(root / "graph.sqlite", manifest["graph_sha256"])
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as con:
            con.execute("PRAGMA query_only=ON")
            if con.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise ValueError("invalid combined graph spool")
            for name, key in (("nodes", "nodes"), ("edges", "relationships")):
                if con.execute("SELECT count(*) FROM " + name).fetchone()[0] != manifest[key]:
                    raise ValueError("forged graph bundle counts")
            if _content(con) != manifest["content_sha256"]:
                raise ValueError("graph bundle payload digest mismatch")
            if con.execute("SELECT count(*) FROM edges e LEFT JOIN nodes a ON a.key=e.subject "
                    "LEFT JOIN nodes b ON b.key=e.object WHERE a.key IS NULL OR b.key IS NULL").fetchone()[0]:
                raise ValueError("dangling prepared graph endpoint")
        checked_file(path, manifest_sha256)
        checked_file(db, manifest["graph_sha256"])
        return cls(root, manifest, manifest_sha256)

    def fence(self):
        checked_file(self.root / "manifest.json", self.manifest_sha256)
        checked_file(self.root / "graph.sqlite", self.manifest["graph_sha256"])

    def rows(self, kind, after="", limit=500):
        if not 1 <= limit <= 5000:
            raise ValueError("invalid graph page budget")
        with sqlite3.connect((self.root / "graph.sqlite").as_uri() + "?mode=ro", uri=True) as con:
            con.execute("PRAGMA query_only=ON")
            return _iter(con, kind, after, limit)

    @classmethod
    def prepare(cls, output_dir, *, canonical_root, snapshot_id, canonical_manifest_sha256,
                nav_dir, nav_manifest_sha256, evidence_dir, evidence_manifest_sha256,
                policy_store, context, historical_snapshots=(), options=None,
                max_records=5_000_000, max_bytes=8 * 1024**3):
        """No remote calls. Failed builds retain an uncommitted spool without manifest.

        Caller runs this CPU producer with OS memory/disk/time guards and a
        qualified read-only code profile. File/code fences detect operational
        changes; they cannot make a mutable host atomic against an administrator.
        """
        import pyarrow.parquet as pq
        from vkm_corpus.graph import common, canon, rows, schema as S, nav_rows as NR, nav_schema as N
        from vkm_corpus.graph.preflight import require_preflight
        from vkm_corpus.api.canon import KIND_TABLE, kind_of, jsonable
        from vkm_evidence.objects import canonical_resolver
        from vkm_evidence.projections import FILES, MANIFEST_KEYS, PROJECTION_SCHEMA, _prepared_summary
        from vkm_corpus.contracts.access import ResourcePolicy

        if type(max_records) is not int or type(max_bytes) is not int or not 1 <= max_records <= 100_000_000 or not 1 <= max_bytes <= 128 * 1024**3:
            raise ValueError("invalid combined graph resource budget")
        output = Path(output_dir).absolute()
        if output.exists() or any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in output.parents):
            raise ValueError("combined graph output must be a new direct directory")
        options = options or NR.ProjectionOptions(symbol_morphology="surface")
        if not options.verify_files or options.symbol_morphology != "surface":
            raise ValueError("combined graph uses pinned deterministic surface morphology and verified files")
        artifacts = {}
        def pin(path, digest):
            path = checked_file(path, digest)
            if path in artifacts and artifacts[path] != digest:
                raise ValueError("conflicting graph input pins")
            artifacts[path] = digest
            return path
        def fence():
            for path, digest in artifacts.items():
                checked_file(path, digest)
            if builder_identity() != builder:
                raise ValueError("graph builder changed during preparation")
        builder = builder_identity()
        pin(policy_store.path, sha256_of(ordinary(policy_store.path)))
        policies = policy_store.read()
        def authorize(sid):
            policy = policies.get(sid)
            if policy is None:
                raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
            policy.require(context)
            return policy

        current = SnapshotPin(Path(canonical_root), snapshot_id, canonical_manifest_sha256)
        pins = (current, *historical_snapshots)
        if len({p.snapshot_id for p in pins}) != len(pins):
            raise ValueError("duplicate pinned graph snapshot")
        snapshots = {}
        for p in pins:
            marker = ordinary(Path(p.root) / ".vkm_root.json")
            if json.loads(marker.read_bytes()).get("root_kind") != "CANONICAL":
                raise ValueError("combined graph requires a canonical source root")
            pin(marker, sha256_of(marker))
            snap = common.load_snapshot(Path(p.root), p.snapshot_id)
            pin(Path(p.root) / "canonical" / "_snapshots" / (p.snapshot_id + ".json"), p.manifest_sha256)
            if snap.manifest_sha256 != p.manifest_sha256 or not snap.datasets:
                raise ValueError("unavailable exact canonical snapshot")
            for ds in snap.datasets.values():
                if len(ds.paths) != len(ds.expected):
                    raise ValueError("canonical files lack exact identity metadata")
                for path, entry in zip(ds.paths, ds.expected):
                    pin(path, entry.get("sha256"))
            common.verify_snapshot_files(snap)
            for path in snap.datasets["sources"].paths:
                for batch in pq.ParquetFile(path).iter_batches(columns=["source_id"], batch_size=500):
                    for row in batch.to_pylist():
                        authorize(row["source_id"])
            snapshots[p.snapshot_id] = snap

        navdir, evdir = Path(nav_dir).absolute(), Path(evidence_dir).absolute()
        navm = json.loads(pin(navdir / "manifest.json", nav_manifest_sha256).read_bytes())
        if (navm.get("snapshot", {}).get("snapshot_id") != snapshot_id
                or navm.get("snapshot", {}).get("manifest_sha256") != canonical_manifest_sha256):
            raise ValueError("NAV does not pin the actual DOCUMENT snapshot")
        for name, entry in navm.get("datasets", {}).items():
            namepath = entry.get("path") or name + ".parquet"
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.parquet", namepath):
                raise ValueError("unsafe NAV artifact name")
            pin(navdir / namepath, entry.get("sha256"))
        evm = json.loads(pin(evdir / "manifest.json", evidence_manifest_sha256).read_bytes())
        if (set(evm) != MANIFEST_KEYS or evm["schema"] != PROJECTION_SCHEMA or set(evm["files"]) != set(FILES)
                or evm["context_sha256"] != record_hash(context)):
            raise ValueError("unsupported or differently authorized evidence projection")
        for name in FILES:
            pin(evdir / name, evm["files"][name])
        summary = _prepared_summary(evdir)
        if any(evm.get(k) != v for k, v in summary.items()) or summary["load_readiness"] != "PINNED_INPUTS_PREPARED":
            raise ValueError("stale or forged EVIDENCE projection")
        inputs = {"DOCUMENT": {"revision": snapshot_id, "manifest_sha256": canonical_manifest_sha256},
                  "NAV": {"revision": snapshot_id, "manifest_sha256": nav_manifest_sha256},
                  "EVIDENCE": {"revision": evm["evidence_revision"], "manifest_sha256": evidence_manifest_sha256},
                  "historical": [{"revision": p.snapshot_id, "manifest_sha256": p.manifest_sha256} for p in pins[1:]]}
        policy_sha = artifacts[Path(policy_store.path).absolute()]
        bid = record_hash({"inputs": inputs, "policy": policy_sha, "context": record_hash(context), "builder": builder,
                           "rules": options.rules()})
        output.mkdir(parents=True, mode=0o700)
        writer = _Writer(output / "graph.sqlite", max_records, max_bytes)
        try:
            with ExitStack() as stack:
                doc = canon.ProjectionInput.from_snapshot(snapshots[snapshot_id], threads=1)
                stack.callback(doc.close)
                require_preflight(doc)
                document_counts = rows.expected_counts(doc)
                for node in S.NODE_TYPES:
                    for row in rows.iter_nodes(doc, node, bid):
                        writer.node(node_key(node.label, row.id), ["DocumentLayer", node.label], row.props)
                for rel in S.REL_TYPES:
                    for row in rows.iter_rels(doc, rel, bid):
                        writer.edge(rel.type, node_key(rel.start, row.from_id), node_key(rel.end, row.to_id), row.props)
                nav = NR.NavInput.open(navdir, options)
                stack.callback(nav.close)
                nav_counts = NR.expected_counts(nav)
                acct = NR.accounting(nav, nav_counts)
                if common.failures(NR.preflight(nav, nav_counts, acct)):
                    raise ValueError("NAV graph preflight failed")
                for node in N.NODE_TYPES:
                    if node.label not in nav_counts["skipped"]:
                        for row in NR.iter_nodes(nav, node, bid):
                            writer.node(node_key(node.label, row.id), ["NavigationLayer", node.label], row.props)
                for rel in N.REL_TYPES:
                    if rel.name not in nav_counts["skipped"]:
                        for row in NR.iter_rels(nav, rel, bid):
                            props = {**row.props, **({rel.key: row.key} if rel.key else {})}
                            writer.edge(rel.type, node_key(rel.start, row.from_id), node_key(rel.end, row.to_id), props)

                resolved = {snapshot_id: doc}
                class CanonView:
                    def __init__(self, inp): self.inp = inp
                    def snapshot_id(self): return self.inp.info.snapshot_id
                    def row(self, kind, oid):
                        table, key = KIND_TABLE[kind]  # closed registry, never caller SQL
                        result = self.inp.fetch(f'SELECT * FROM {table} WHERE "{key}"=?', [oid])
                        if len(result) != 1:
                            raise ValueError("exact original record absent or duplicated")
                        return result[0]
                def prows(name):
                    for batch in pq.ParquetFile(evdir / name).iter_batches(batch_size=500):
                        yield from batch.to_pylist()
                def evnodes():
                    for row in prows("evidence.parquet"): yield {"layer": "EVIDENCE", **row}
                    for row in prows("occurrences.parquet"): yield {"layer": "ORIGINAL_OCCURRENCE", "node_key": row["occurrence_id"], **row}
                    for row in prows("origins.parquet"): yield {"layer": "PRIMARY_ORIGIN", "node_key": row["origin_version_id"], **row}
                def jsonlines(name):
                    with (evdir / name).open(encoding="utf-8") as stream:
                        for line in stream:
                            if line.strip(): yield json.loads(line)
                for row, prepared in itertools.zip_longest(evnodes(), jsonlines("neo4j_nodes.jsonl")):
                    if row != prepared:
                        raise ValueError("evidence JSONL differs from canonical Parquet")
                    if row["layer"] == "EVIDENCE":
                        payload = json.loads(row["payload_json"])
                        if (record_hash(payload) != row["record_sha256"] or payload.get("record_id") != row["record_id"]
                                or payload.get("kind") != row["kind"]):
                            raise ValueError("evidence record payload identity mismatch")
                        ResourcePolicy.model_validate(payload["policy"]).require(context)
                    writer.node(row["node_key"], ["Evidence", "EvidenceLayer"],
                        {"node_key": row["node_key"], "layer": row["layer"], "payload_json": canonical_bytes(row).decode()})
                    if row["layer"] != "ORIGINAL_OCCURRENCE":
                        continue
                    ref = ObjectRef.model_validate_json(row["payload_json"])
                    if row["occurrence_id"] != "occurrence:" + record_hash(ref):
                        raise ValueError("forged original occurrence key")
                    if any(row.get(k) != getattr(ref, k) for k in ("source_id", "source_sha256", "snapshot_id", "object_id",
                                                                 "object_version", "content_sha256", "extraction_generation", "locator")):
                        raise ValueError("original occurrence columns differ from pinned reference")
                    if ref.snapshot_id not in snapshots:
                        raise ValueError("historical original snapshot requires exact retained artifact pin")
                    if ref.snapshot_id not in resolved:
                        resolved[ref.snapshot_id] = canon.ProjectionInput.from_snapshot(snapshots[ref.snapshot_id], threads=1)
                        stack.callback(resolved[ref.snapshot_id].close)
                    view = CanonView(resolved[ref.snapshot_id])
                    canonical_resolver(view, authorize)(ref)
                    payload = jsonable(view.row(kind_of(ref.object_id), ref.object_id))
                    # One original payload per occurrence (including exact span),
                    # never a current-label duplicate with an ambiguous stable ID.
                    pkey = "PinnedOriginalRecord:" + record_hash(ref)
                    writer.node(pkey, ["PinnedOriginalRecord", "EvidenceLayer"], {
                        "snapshot_id": ref.snapshot_id, "object_id": ref.object_id,
                        "canonical_manifest_sha256": snapshots[ref.snapshot_id].manifest_sha256,
                        "payload_json": canonical_bytes(payload).decode(), "reference_json": canonical_bytes(ref).decode()})
                    writer.edge("PINNED_DOCUMENT_RECORD", row["node_key"], pkey, {"pin_state": "EXACT_ORIGINAL"})
                for row, prepared in itertools.zip_longest(prows("relations.parquet"), jsonlines("neo4j_relations.jsonl")):
                    if row != prepared:
                        raise ValueError("evidence relationships differ from canonical Parquet")
                    writer.edge("VKM_EVIDENCE_REL", row["subject_node_key"], row["object_node_key"],
                        {"predicate": row["predicate"], "payload_json": canonical_bytes(row).decode()})
                accounting = {"DOCUMENT": rows.not_projected_report(doc), "NAV": acct,
                              "NAV_skipped": nav_counts["skipped"], "rules": options.rules()}
                meta_common = {"status": "COMPLETE", "bundle_id": bid}
                writer.node("meta:document", ["ProjectionRun"], {**meta_common, "id": bid, "layer": "DOCUMENT",
                    "built_from_snapshot_id": snapshot_id, "canonical_manifest_sha256": canonical_manifest_sha256,
                    "graph_schema_version": S.GRAPH_SCHEMA_VERSION, "counts_json": canonical_bytes(document_counts).decode(),
                    "projector_version": FORMAT, "content_digest": bid})
                writer.node("meta:nav", ["NavMeta"], {**meta_common, "id": N.META_ID, "layer": "NAV",
                    "snapshot_id": snapshot_id, "run_id": bid, "doc_build_id": bid, "doc_snapshot": snapshot_id,
                    "graph_schema_version": N.NAV_GRAPH_SCHEMA_VERSION, "nav_manifest_sha256": nav_manifest_sha256,
                    "counts_json": canonical_bytes(nav_counts).decode(), "accounting_json": canonical_bytes(acct).decode()})
                writer.node("meta:combined", ["VkmCombinedLoad"], {**meta_common, "inputs_json": canonical_bytes(inputs).decode(),
                    "policy_sha256": policy_sha, "context_sha256": record_hash(context), "builder_sha256": record_hash(builder)})
                writer.require_endpoints()
                writer.con.commit()
                counts = {"nodes": writer.con.execute("SELECT count(*) FROM nodes").fetchone()[0],
                          "relationships": writer.con.execute("SELECT count(*) FROM edges").fetchone()[0]}
                digest = _content(writer.con)
            writer.con.close()
            fence()
            manifest = {"schema": FORMAT, "bundle_id": bid, "inputs": inputs, "policy_sha256": policy_sha,
                        "context_sha256": record_hash(context), "builder": builder, **counts, "content_sha256": digest,
                        "graph_sha256": sha256_of(output / "graph.sqlite"), "accounting": accounting, "remote_load": "NOT_RUN"}
            fence()
            if not create_exclusive(output / "manifest.json", canonical_bytes(manifest).decode("utf-8")):
                raise ValueError("combined graph manifest publication conflict")
            return cls.read(output, manifest_sha256=sha256_of(output / "manifest.json"))
        finally:
            writer.con.close()
