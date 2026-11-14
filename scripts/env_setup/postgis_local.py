#!/usr/bin/env python3
"""Isolated synthetic PostGIS qualification; never connects to EDGE or native PG.

Docker Desktop/Engine must already run. Output belongs in ignored private work.
No cleanup removes volumes. Existing containers require exact labels/image/mounts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

IMAGE = "postgis/postgis@sha256:c5d1ba4292ff7b4471ff41cd515c677633797b72cdd2a7bb691cadaab91f1a1f"
PREFIX = "vkm-env-20261114"
SOURCE = PREFIX + "-postgis37"
RESTORE = PREFIX + "-postgis37-restore"
LABEL = "vkm.env.scope=synthetic-local-20261114"
DB = "vkm_fixture"


class Runner:
    def __init__(self, output: Path):
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.log = []

    def run(self, args, data=None, *, binary=False, env=None, check=True):
        proc = subprocess.run(args, input=data, capture_output=True,
                              text=not binary, encoding=None if binary else "utf-8",
                              timeout=180, env=env)
        # Credentials are inherited env only, never arguments/logged values.
        self.log.append({"argv": args, "exit_code": proc.returncode,
                         "stdout": (f"<{len(proc.stdout)} bytes>" if binary else proc.stdout),
                         "stderr": (proc.stderr.decode("utf-8", "replace") if binary else proc.stderr)})
        self.save_log()
        if check and proc.returncode:
            raise RuntimeError(f"Command failed ({proc.returncode}): {args[:4]}: {proc.stderr!r}")
        return proc.stdout

    def save_log(self):
        (self.output / "commands.json").write_text(json.dumps(self.log, indent=2), encoding="utf-8")

    def sql(self, name, sql, db=DB):
        return self.run(["docker", "exec", "-i", name, "psql", "-X", "-q", "-A", "-t",
                         "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", db], sql)

    def inspect(self, name):
        result = self.run(["docker", "container", "inspect", name,
                           "--format", '{"Config":{"Image":{{json .Config.Image}},"Labels":{{json .Config.Labels}}},"Mounts":{{json .Mounts}},"HostConfig":{"PortBindings":{{json .HostConfig.PortBindings}}},"State":{"Running":{{json .State.Running}}}}'], check=False)
        if not result.strip():
            if any(marker in self.log[-1]["stderr"] for marker in ("No such object", "No such container")):
                return None
            raise RuntimeError("Cannot inspect container; Docker service/permissions unavailable")
        raw = json.loads(result)
        return raw

    def validate(self, raw, name, volume):
        if raw["Config"].get("Labels", {}).get("vkm.env.scope") != LABEL.split("=", 1)[1]:
            raise RuntimeError("Refusing container without this run's label")
        if raw["Config"]["Image"] != IMAGE:
            raise RuntimeError("Existing container uses a different image")
        mounts = raw["Mounts"]
        if len(mounts) != 1 or mounts[0].get("Name") != volume or mounts[0]["Destination"] != "/var/lib/postgresql":
            raise RuntimeError("Unexpected mount; refusing to touch container")
        bindings = raw["HostConfig"].get("PortBindings", {})
        expected = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55438"}]} if name == SOURCE else {}
        if bindings != expected:
            raise RuntimeError("Unexpected published ports")

    def start(self, name):
        volume = name + "-data"
        current = self.inspect(name)
        if current:
            self.validate(current, name, volume)
            if not current["State"]["Running"]:
                self.run(["docker", "start", name])
        else:
            existing = self.run(["docker", "volume", "ls", "--format", "{{.Name}}"])
            if volume in existing.splitlines():
                labels = json.loads(self.run(["docker", "volume", "inspect", volume, "--format", "{{json .Labels}}"] )) or {}
                if labels.get("vkm.env.scope") != LABEL.split("=", 1)[1]:
                    raise RuntimeError("Refusing existing unlabeled volume")
            else:
                self.run(["docker", "volume", "create", "--label", LABEL, volume])
            env = dict(os.environ, POSTGRES_PASSWORD=secrets.token_urlsafe(32))
            args = ["docker", "run", "-d", "--name", name, "--label", LABEL,
                    "--cpus", "2", "--memory", "1g", "--pids-limit", "256",
                    "--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_DB=" + DB,
                    "--mount", f"type=volume,src={volume},dst=/var/lib/postgresql"]
            if name == SOURCE:
                args += ["--publish", "127.0.0.1:55438:5432"]
            self.run(args + [IMAGE], env=env)
        deadline = time.monotonic() + 60
        while True:
            # Init scripts may temporarily expose a socket before final startup.
            text = self.run(["docker", "logs", name], check=False)
            if "PostgreSQL init process complete" in text or current:
                probe = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", DB], capture_output=True)
                if probe.returncode == 0:
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("PostgreSQL readiness deadline exceeded")
            time.sleep(1)
        self.validate(self.inspect(name), name, volume)


SEED = """
CREATE EXTENSION IF NOT EXISTS postgis;
DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='fixture_owner') THEN
 CREATE ROLE fixture_owner NOLOGIN; CREATE ROLE fixture_reader NOLOGIN; END IF; END $$;
CREATE SCHEMA IF NOT EXISTS synthetic AUTHORIZATION fixture_owner;
CREATE TABLE IF NOT EXISTS synthetic.geometry(
 id text PRIMARY KEY, frame text NOT NULL, units text NOT NULL,
 frame_metadata jsonb NOT NULL, geom geometry,
 CHECK (geom IS NULL OR ST_SRID(geom)=0));
ALTER TABLE synthetic.geometry OWNER TO fixture_owner;
REVOKE ALL ON SCHEMA synthetic FROM PUBLIC;
REVOKE ALL ON synthetic.geometry FROM PUBLIC;
GRANT USAGE ON SCHEMA synthetic TO fixture_reader;
GRANT SELECT ON synthetic.geometry TO fixture_reader;
INSERT INTO synthetic.geometry(id,frame,units,frame_metadata,geom) VALUES
('point-001','SYNTHETIC_PIXEL','px','{"epistemic":"SYNTHETIC","crs":"UNKNOWN"}',ST_GeomFromText('POINT(10 20)',0)),
('line-001','SYNTHETIC_PIXEL','px','{"epistemic":"SYNTHETIC","crs":"UNKNOWN"}',ST_GeomFromText('LINESTRING(0 0,5 5,10 0)',0)),
('polygon-001','SYNTHETIC_PIXEL','px','{"epistemic":"SYNTHETIC","crs":"UNKNOWN"}',ST_GeomFromText('POLYGON((0 0,10 0,10 10,0 10,0 0),(2 2,2 4,4 4,4 2,2 2))',0)),
('null-001','SYNTHETIC_PIXEL','px','{"epistemic":"SYNTHETIC","crs":"UNKNOWN"}',NULL),
('local-point-001','SYNTHETIC_LOCAL_M','m','{"epistemic":"SYNTHETIC","crs":"UNKNOWN"}',ST_GeomFromText('POINT(10 20)',0))
ON CONFLICT(id) DO UPDATE SET frame=EXCLUDED.frame,units=EXCLUDED.units,frame_metadata=EXCLUDED.frame_metadata,geom=EXCLUDED.geom;
"""

SNAPSHOT = """
SELECT json_build_object(
 'versions',json_build_object('postgres',version(),'postgis',postgis_full_version()),
 'rows',(SELECT json_agg(r ORDER BY id) FROM (SELECT id,frame,units,frame_metadata,
 encode(ST_AsBinary(geom,'NDR'),'hex') wkb_hex,ST_SRID(geom) srid,
 ST_GeometryType(geom) type,ST_IsValid(geom) valid,
 CASE WHEN GeometryType(geom)='POLYGON' THEN ST_NumInteriorRings(geom) END holes,
 ST_AsGeoJSON(geom)::json coordinates FROM synthetic.geometry)r),
 'ownership',(SELECT json_agg(r) FROM (SELECT nspname,pg_get_userbyid(nspowner) owner,nspacl::text acl
 FROM pg_namespace WHERE nspname='synthetic')r),
 'table_privileges',(SELECT json_agg(r) FROM (SELECT pg_get_userbyid(relowner) owner,relacl::text acl
 FROM pg_class WHERE oid='synthetic.geometry'::regclass)r),
 'roles',(SELECT json_agg(r ORDER BY rolname) FROM (SELECT rolname,rolsuper,rolcanlogin FROM pg_roles
 WHERE rolname IN ('fixture_owner','fixture_reader'))r),
 'checks',json_build_object(
 'all_srid_zero',(SELECT bool_and(ST_SRID(geom)=0) FROM synthetic.geometry),
 'wkb_roundtrip',(SELECT bool_and(ST_Equals(geom,ST_GeomFromWKB(ST_AsBinary(geom),0))) FROM synthetic.geometry WHERE geom IS NOT NULL),
 'one_null',(SELECT count(*)=1 FROM synthetic.geometry WHERE geom IS NULL),
 'point_coordinates',(SELECT ST_X(geom)=10 AND ST_Y(geom)=20 FROM synthetic.geometry WHERE id='point-001'),
 'line_coordinates',(SELECT ST_NPoints(geom)=3 AND ST_X(ST_PointN(geom,2))=5 AND ST_Y(ST_PointN(geom,2))=5 FROM synthetic.geometry WHERE id='line-001'),
 'hole_preserved',(SELECT ST_NumInteriorRings(geom)=1 FROM synthetic.geometry WHERE id='polygon-001'),
 'hole_excludes_point',(SELECT NOT ST_Contains(geom,ST_Point(3,3)) FROM synthetic.geometry WHERE id='polygon-001'),
 'spatial_query',(SELECT ST_Contains(geom,ST_Point(1,1)) AND ST_Area(geom)=96 FROM synthetic.geometry WHERE id='polygon-001' AND frame='SYNTHETIC_PIXEL' AND units='px'),
 'reader_select',has_table_privilege('fixture_reader','synthetic.geometry','SELECT'),
 'reader_cannot_insert',NOT has_table_privilege('fixture_reader','synthetic.geometry','INSERT'),
 'owner',(SELECT pg_get_userbyid(relowner)='fixture_owner' FROM pg_class WHERE oid='synthetic.geometry'::regclass)));
"""


def logical(snapshot):
    return {key: snapshot[key] for key in ("rows", "ownership", "table_privileges", "roles")}


def snapshot(runner, name):
    result = json.loads(runner.sql(name, SNAPSHOT))
    if not all(value is True for value in result["checks"].values()):
        raise AssertionError(result["checks"])
    result["logical_sha256"] = hashlib.sha256(json.dumps(logical(result), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["setup", "verify", "roundtrip", "stop"])
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--fixture", type=Path, help="JSON {features:[{id,frame,unit,wkb_hex,srid:0}]} from GPKG/DuckDB readback")
    args = ap.parse_args()
    r = Runner(args.output)
    receipt = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(), "image": IMAGE,
               "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "action": args.action, "production_touched": False}
    try:
        if args.action == "stop":
            for name in (SOURCE, RESTORE):
                raw = r.inspect(name)
                if raw:
                    r.validate(raw, name, name + "-data")
                    r.run(["docker", "stop", name])
            receipt["status"] = "STOPPED_VOLUMES_RETAINED"
        elif args.action == "roundtrip":
            if not args.fixture:
                ap.error("--fixture is required for roundtrip")
            r.validate(r.inspect(SOURCE), SOURCE, SOURCE + "-data")
            fixture = json.loads(args.fixture.read_text(encoding="utf-8-sig"))
            features = fixture["features"]
            if not features or len({row["id"] for row in features}) != len(features):
                raise ValueError("Fixture requires nonempty unique IDs")
            for row in features:
                row["units"] = row.get("units", row.get("unit"))
                if row["units"] is None:
                    raise ValueError("Feature needs unit or units")
            sql = "BEGIN; CREATE SCHEMA IF NOT EXISTS format_roundtrip; CREATE TABLE IF NOT EXISTS format_roundtrip.geometry(id text PRIMARY KEY,frame text NOT NULL,units text NOT NULL,geom geometry);\n"
            for row in features:
                wkb = row.get("wkb_hex")
                if wkb is not None:
                    bytes.fromhex(wkb)
                if row.get("srid", 0) != 0:
                    raise ValueError("Synthetic local/pixel fixture requires SRID 0")
                geom = f"ST_GeomFromWKB(decode({quote(wkb)},'hex'),0)" if wkb else "NULL"
                sql += f"INSERT INTO format_roundtrip.geometry VALUES({quote(row['id'])},{quote(row['frame'])},{quote(row['units'])},{geom}) ON CONFLICT(id) DO UPDATE SET frame=EXCLUDED.frame,units=EXCLUDED.units,geom=EXCLUDED.geom;\n"
            sql += "COMMIT;"
            r.sql(SOURCE, sql)
            rows = json.loads(r.sql(SOURCE, "SELECT json_agg(r ORDER BY id) FROM (SELECT id,frame,units,encode(ST_AsBinary(geom,'NDR'),'hex') wkb_hex,ST_SRID(geom) srid,ST_GeometryType(geom) type,ST_AsGeoJSON(geom)::json geometry,CASE WHEN GeometryType(geom)='POLYGON' THEN ST_NumInteriorRings(geom) END holes FROM format_roundtrip.geometry)r;"))
            expected = {row["id"]: row for row in features}
            if {row["id"] for row in rows} != set(expected):
                raise AssertionError("ID set changed")
            for row in rows:
                row["unit"] = row["units"]
                original = expected[row["id"]]
                if (row["frame"], row["units"]) != (original["frame"], original["units"]):
                    raise AssertionError("Frame metadata changed")
                wkb = original.get("wkb_hex")
                if wkb and r.sql(SOURCE, f"SELECT ST_Equals(geom,ST_GeomFromWKB(decode({quote(wkb)},'hex'),0)) FROM format_roundtrip.geometry WHERE id={quote(row['id'])};").strip() != "t":
                    raise AssertionError("Geometry changed")
                if wkb is None and row["wkb_hex"] is not None:
                    raise AssertionError("NULL changed")
            receipt.update(status="PASS", features=rows, fixture_sha256=hashlib.sha256(args.fixture.read_bytes()).hexdigest())
        else:
            if args.action == "setup":
                r.run(["docker", "pull", IMAGE])
                r.start(SOURCE)
                r.start(RESTORE)
                r.sql(SOURCE, SEED)
            else:
                for name in (SOURCE, RESTORE):
                    r.validate(r.inspect(name), name, name + "-data")
            before = snapshot(r, SOURCE)
            # pg_dumpall exports role definitions; synthetic NOLOGIN roles have no secrets.
            roles = r.run(["docker", "exec", SOURCE, "pg_dumpall", "-U", "postgres", "--roles-only", "--no-role-passwords"])
            (args.output / "roles.sql").write_text(roles, encoding="utf-8")
            # Replay exact dumped definitions for run-owned roles; bootstrap exists.
            role_sql = []
            for role in ("fixture_owner", "fixture_reader"):
                r.sql(RESTORE, f"DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role}; END IF; END $$;", "postgres")
                definitions = [line for line in roles.splitlines() if line.startswith(f"ALTER ROLE {role} WITH ")]
                if len(definitions) != 1:
                    raise AssertionError("Expected one dumped role definition")
                role_sql.extend(definitions)
            (args.output / "fixture_roles.sql").write_text("\n".join(role_sql), encoding="utf-8")
            r.sql(RESTORE, "\n".join(role_sql), "postgres")
            dump = r.run(["docker", "exec", SOURCE, "pg_dump", "-U", "postgres", "-d", DB, "-Fc", "--schema=synthetic"], binary=True)
            (args.output / "synthetic.dump").write_bytes(dump)
            # Both instances already contain the pinned PostGIS extension.
            r.sql(RESTORE, "CREATE EXTENSION IF NOT EXISTS postgis;")
            r.run(["docker", "exec", "-i", RESTORE, "pg_restore", "-U", "postgres", "-d", DB,
                   "--exit-on-error", "--clean", "--if-exists", "--single-transaction"], dump, binary=True)
            after = snapshot(r, RESTORE)
            if before["logical_sha256"] != after["logical_sha256"]:
                raise AssertionError("Restore logical digest differs")
            # A real restricted-role SELECT and rejected INSERT validate effective ACLs.
            restricted = r.sql(RESTORE, "BEGIN; SET LOCAL ROLE fixture_reader; SELECT count(*) FROM synthetic.geometry; ROLLBACK;")
            denial = r.run(["docker", "exec", "-i", RESTORE, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", DB], "BEGIN; SET LOCAL ROLE fixture_reader; INSERT INTO synthetic.geometry VALUES('forbidden','SYNTHETIC_PIXEL','px','{}',NULL); ROLLBACK;", check=False)
            if r.log[-1]["exit_code"] == 0 or "permission denied" not in r.log[-1]["stderr"]:
                raise AssertionError("Restricted INSERT unexpectedly succeeded")
            receipt.update(status="PASS", source=before, restored=after,
                           dump_sha256=hashlib.sha256(dump).hexdigest(),
                           restored_reader_select=restricted.strip(), restricted_insert_denied=True,
                           scope="synthetic schema; not a production restore rehearsal")
    except Exception as exc:
        receipt.update(status="FAIL", error=str(exc))
        raise
    finally:
        r.save_log()
        (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("status", "action", "image")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
