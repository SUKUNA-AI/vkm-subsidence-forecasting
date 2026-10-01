"""Read-only native PostgreSQL control identity on the actual API connection.

No SQL is accepted from configuration. Worker heartbeats prove current control
liveness, not worker code provenance or successful execution of future jobs.
The narrowly privileged pg_control_system reader is required; absence blocks.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from vkm_evidence.contracts import Sha256, StrictModel, record_hash


class WorkerRequirement(StrictModel):
    host_role: Literal["WORKSTATION", "CORE", "EDGE"]
    kind: Literal["PIPELINE", "PUBLISHER"]


class ControlSpec(StrictModel):
    schema_sha256: Sha256
    workers: tuple[WorkerRequirement, ...]
    max_heartbeat_age_seconds: int = Field(default=180, ge=1, le=900)

    @model_validator(mode="after")
    def _workers(self):
        keys = [(x.host_role, x.kind) for x in self.workers]
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("explicit nonempty unique control worker requirements required")
        return self


BEGIN = "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
TIMEOUT = "SET LOCAL statement_timeout = '5000ms'"
NATIVE = """SELECT system_identifier::text AS system_identifier,
 (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS database_oid,
 current_database() AS database_name, current_setting('server_version_num') AS server_version,
 current_user::text AS current_role, session_user::text AS session_role,
 current_setting('search_path') AS search_path,
 inet_server_addr()::text AS server_address, inet_server_port() AS server_port,
 pg_postmaster_start_time()::text AS postmaster_started
 FROM pg_control_system()"""
COLUMNS = """SELECT c.relname AS relation, a.attnum AS position, a.attname AS name,
 format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null,
 pg_get_expr(d.adbin,d.adrelid) AS default_expression
 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
 JOIN pg_attribute a ON a.attrelid=c.oid
 LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
 WHERE n.nspname='ops' AND c.relkind='r' AND a.attnum>0 AND NOT a.attisdropped
 ORDER BY c.relname,a.attnum"""
CONSTRAINTS = """SELECT c.relname AS relation, k.conname AS name,
 pg_get_constraintdef(k.oid) AS definition
 FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
 JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='ops'
 ORDER BY c.relname,k.conname"""
INDEXES = "SELECT tablename AS relation,indexname AS name,indexdef AS definition FROM pg_indexes WHERE schemaname='ops' ORDER BY tablename,indexname"
VERSIONS = "SELECT version FROM ops.schema_version ORDER BY version"
WORKERS = """SELECT worker_id,host_role,kind,state,
 extract(epoch FROM (clock_timestamp()-last_heartbeat_at))::double precision AS age_seconds
 FROM ops.worker ORDER BY worker_id"""
REQUIRED_TABLES = {"schema_version", "ingest_run", "worker", "job", "attempt", "heartbeat", "error", "review_task", "service_state"}


def read_native(conn, spec: ControlSpec):
    """Fresh transaction on PgControlPlane._run's actual serving connection."""
    try:
        with conn.cursor() as cur:
            cur.execute(BEGIN)
            cur.execute(TIMEOUT)
            cur.execute(NATIVE)
            native = dict(cur.fetchone() or {})
            required = {"system_identifier", "database_oid", "database_name", "server_version", "server_address", "server_port", "postmaster_started",
                        "current_role", "session_role", "search_path"}
            if set(native) != required or not all(native.values()):
                raise ValueError("native PostgreSQL cluster/database/endpoint identity unavailable")
            if not str(native["system_identifier"]).isdigit() or not isinstance(native["database_oid"], int):
                raise ValueError("invalid PostgreSQL system identifier")
            cur.execute(COLUMNS)
            columns = [dict(r) for r in cur.fetchall()]
            cur.execute(CONSTRAINTS)
            constraints = [dict(r) for r in cur.fetchall()]
            cur.execute(INDEXES)
            indexes = [dict(r) for r in cur.fetchall()]
            cur.execute(VERSIONS)
            versions = [dict(r) for r in cur.fetchall()]
            schema = {"columns": columns, "constraints": constraints, "indexes": indexes, "versions": versions}
            if ({r["relation"] for r in columns} != REQUIRED_TABLES or versions != [{"version": "ops-0.1.0"}]
                    or record_hash(schema) != spec.schema_sha256):
                raise ValueError("actual control schema differs from qualified schema")
            cur.execute(WORKERS)
            workers = [dict(r) for r in cur.fetchall()]
            active = set()
            for row in workers:
                age = row.get("age_seconds")
                if (row.get("state") in {"IDLE", "BUSY"} and isinstance(age, (float, int))
                        and not isinstance(age, bool) and 0 <= age <= spec.max_heartbeat_age_seconds):
                    active.add((row.get("host_role"), row.get("kind")))
            if any((w.host_role, w.kind) not in active for w in spec.workers):
                raise ValueError("control worker missing, stale, stopped or future-dated")
            # Liveness timestamps/worker IDs vary; they are gates, not identity.
            return {"native": native, "schema_sha256": record_hash(schema),
                    "workers": sorted([list(x) for x in active if x in {(w.host_role, w.kind) for w in spec.workers}])}
    finally:
        conn.rollback()


class ControlServiceLease:
    def __init__(self, control, spec: ControlSpec):
        from vkm_corpus.api.backends import PgControlPlane
        if not isinstance(control, PgControlPlane):
            raise ValueError("control identity requires actual PgControlPlane backend")
        self.control, self.spec = control, spec
        self.identity = self._observe()

    def _observe(self):
        from vkm_corpus.api.production import serving_code_identity, serving_dependencies_identity
        from vkm_corpus.api.errors import ApiFailure
        from vkm_corpus.update.contracts import ServiceIdentity
        try:
            body = self.control._run(lambda conn: read_native(conn, self.spec))
        except ApiFailure as exc:
            raise ValueError("native control identity unavailable") from exc
        native = body["native"]
        return ServiceIdentity(service="CONTROL", instance_sha256=record_hash(native),
            code_sha256=serving_code_identity(), dependencies_sha256=serving_dependencies_identity(),
            config_sha256=record_hash({"profile": self.spec.model_dump(mode="json"),
                "actual_session": {k: native[k] for k in ("current_role", "session_role", "search_path")}}),
            endpoint_sha256=record_hash({k: native[k] for k in ("system_identifier", "database_oid", "server_address", "server_port")}),
            runtime_sha256=record_hash({"version": native["server_version"], "started": native["postmaster_started"]}),
            resources={"ops_schema": body["schema_sha256"]},
            capabilities=("job_status", "control_schema", "required_worker_liveness"))

    def observe(self):
        actual = self._observe()
        if actual != self.identity:
            raise ValueError("qualified control native identity changed")
        return actual
