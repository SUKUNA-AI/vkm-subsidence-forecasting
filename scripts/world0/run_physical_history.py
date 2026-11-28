"""Admit the current C1 dry history before launching a compatible private driver."""
from argparse import ArgumentParser, REMAINDER
import hashlib
import json
import os
import subprocess
from pathlib import Path
from time import perf_counter

import numpy as np

from vkm_world.representations.world0.binding import (
    file_digest, physical_history_materials, verify_native_history_state,
)


APPROVED_GEOMETRY = "C1-27ff390288fa392a654b6558"
APPROVED_SOURCE_SHA256 = "4c3b8e33469392b21f3bce022e9aa6d1b6f54c3d9d9ecd681fafc90b9ebded5f"
SECONDS_PER_JULIAN_YEAR = 31_557_600
C4_PAYLOAD_NAMES = {
    "FIRST_1939_CHAMBERS.geojson", "FIRST_1939_CHAMBERS.parquet",
    "FIRST_1939_PIECES.parquet", "FIRST_1939_PIECE_RINGS.npz",
}


def read(path):
    return json.loads(path.read_bytes())


def run(args):
    start = perf_counter()
    pin = read(args.pin)
    if (pin["geometry_id"], pin["source_field"]["sha256"]) != (APPROVED_GEOMETRY, APPROVED_SOURCE_SHA256):
        raise ValueError("pin is outside the approved PHYSICAL_HISTORY_2025 route")
    field = args.private_root / pin["source_field"]["path"]
    authority = read(args.private_root / pin["acceptance_path"])
    if (authority["preserved_c1"]["geometry_id"] != pin["geometry_id"] or
            authority["scientific_status"] != pin["owner_status"]):
        raise ValueError("different owner geometry in PRIVATE")
    carrier = args.source_run / "P00/drape_01/CARRIER.npz"
    receipt_path = carrier.with_name("RECEIPT.json")
    receipt = read(receipt_path)
    world_dir = args.source_run / "P07/named_A"
    identity_path = world_dir / "IDENTITY.json"
    ident = read(identity_path)
    cards = physical_history_materials(field, carrier, pin, receipt, ident["body_cards"])
    if (ident["geometry_id"] != pin["geometry_id"] or
            ident["parent_field_sha256"] != pin["source_field"]["sha256"] or
            ident["carrier_sha256"] != file_digest(carrier) or
            ident["carrier_receipt_sha256"] != file_digest(receipt_path)):
        raise ValueError("world and mechanical carrier are incompatible")
    context = ident.copy()
    signature = context.pop("signature")
    actual_signature = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    if actual_signature != signature:
        raise ValueError("corrupt world signature")
    if not ident.get("pressure_contact_scope") or not ident.get("external_load"):
        raise ValueError("missing C5 scope")
    props = np.asarray([[card[key] for key in ("E_pa", "nu", "rho_kg_m3")] for card in cards])
    if (not np.isfinite(props).all() or np.any(props[:, [0, 2]] <= 0) or
            np.any(props[:, 1] <= -1) or np.any(props[:, 1] >= .5)):
        raise ValueError("unusable C2 parameters")
    state_1933 = world_dir / "STATE_1933_0.npz"
    with np.load(state_1933, allow_pickle=False) as initial:
        if initial["signature"].item() != signature or initial["time_year"].item() != 1933.0:
            raise ValueError("foreign initial C3")
    with np.load(world_dir / "MODEL_ARRAYS.npz", allow_pickle=False) as model:
        if model["geometry_id"].item() != pin["geometry_id"]:
            raise ValueError("foreign assembled matrices")
        if set(np.unique(model["layer"])) != set(range(len(cards))):
            raise ValueError("assembled carrier does not use all named C2 materials")
        np.testing.assert_array_equal(model["rho_kg_m3"], props[model["layer"], 2])
    clock = read(world_dir / "RUN.json")
    if clock["identity_sha256"] != signature or clock["initial"]["time_year"] != 1933.0:
        raise ValueError("run receipt has foreign C3")
    event_dir = args.source_run / "P10_P12"
    event = read(event_dir / "FIRST_1939_TRANSACTION.json")
    if (event["geometry_id"] != pin["geometry_id"] or
            event["field_sha"] != pin["source_field"]["sha256"] or
            event["mechanical_guard_limit"] != .2 or event["clock_year"] != clock["last_epoch"]):
        raise ValueError("C4 event is foreign or disagrees with the own PRE")
    if file_digest(args.calendar) != event["calendar_file_sha"]:
        raise ValueError("C4 calendar bytes mismatch")
    with args.calendar.open(encoding="utf8") as stream:
        first = json.loads(next(stream))
    elapsed = (event["clock_year"] - clock["initial"]["time_year"]) * SECONDS_PER_JULIAN_YEAR
    if (event["clock_s"] != elapsed or event["clock_s"] != first["clock_s"] or
            event["clock_year"] != first["year"] or event["atomic_event_ids"] != first["cohort_ids"]):
        raise ValueError("C4 first atomic group or clock disagrees with its calendar")
    payloads = event["input_and_output_files"]
    if set(payloads) != C4_PAYLOAD_NAMES:
        raise ValueError("C4 payload set is incomplete or foreign")
    for name, digest in payloads.items():
        if file_digest(event_dir / name) != digest:
            raise ValueError(f"C4 payload changed: {name}")
    chamber_ids = [row["properties"]["event_id"]
                   for row in read(event_dir / "FIRST_1939_CHAMBERS.geojson")["features"]]
    if len(chamber_ids) != event["contours"] or set(chamber_ids) != set(first["cohort_ids"]):
        raise ValueError("C4 chamber geometry disagrees with the first atomic group")
    state = world_dir / "STATE_LAST.npz"
    verify_native_history_state(state, clock["final_sha256"], signature, event["clock_year"])
    if not args.runtime.is_file():
        raise ValueError("pinned runtime missing")
    runtime_hashes = json.loads(subprocess.check_output(
        [str(args.runtime), "-c",
         "import hashlib,json,pathlib; from vkm_world.representations.world0 import maxwell_coefficients as m,native_prism as p; "
         "print(json.dumps({k:hashlib.sha256(pathlib.Path(s.__file__).read_bytes()).hexdigest() for k,s in [('maxwell',m),('prism',p)]}))"],
        env={k:v for k,v in os.environ.items() if k.upper()!="PYTHONPATH"}, text=True))
    if runtime_hashes != {"maxwell":ident["world0_maxwell_module_sha256"], "prism":ident["world0_prism_module_sha256"]}:
        raise ValueError("restart runtime differs from the qualified C3 law/assembly")
    if (args.driver is None) != (args.driver_sha256 is None):
        raise ValueError("supply both an explicit current driver and its SHA-256")
    if args.driver is not None:
        driver = args.driver.resolve(strict=True)
        if not driver.is_file() or driver.suffix != ".py" or file_digest(driver) != args.driver_sha256:
            raise ValueError("current history driver is missing or has changed bytes")
    result = {"scope":"Stage A3 actual current input and conditional PRE readback",
              "geometry_id":pin["geometry_id"],"source_field_sha256":file_digest(field),
              "carrier_sha256":file_digest(carrier),"world_signature":signature,
              "named_bodies":len(cards),"C3_native_start_sha256":file_digest(state_1933),
              "C5_scope":ident["pressure_contact_scope"],"event_clock_s":event["clock_s"],
              "contours":event["contours"],"C4_guard":.2,"C4_payloads_verified":len(payloads),
              "calendar_sha256":event["calendar_file_sha"], "atomic_event_ids":first["cohort_ids"],
              "state_sha256":file_digest(state),"year":event["clock_year"],
              "runtime_modules":runtime_hashes,"history_complete_to_2025":False,
              "driver_sha256":args.driver_sha256,
              "elapsed_s":perf_counter()-start,
              "next_action":"Solve in the pinned conditional world and qualify each prospective POST separately."}
    with args.output.open("x", encoding="utf8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if args.driver is None:
        return 0
    env = {key: value for key, value in os.environ.items() if key.upper() != "PYTHONPATH"}
    env["AMHF_PHYSICAL_HISTORY_BINDING"] = str(args.output.resolve(strict=True))
    env["AMHF_PHYSICAL_HISTORY_INPUT_ROOT"] = str(args.source_run.resolve(strict=True))
    child_args = args.driver_args[1:] if args.driver_args[:1] == ["--"] else args.driver_args
    return subprocess.run([str(args.runtime.resolve(strict=True)), str(driver), *child_args],
                          cwd=args.driver_cwd, env=env,check=False).returncode


if __name__ == "__main__":
    parser = ArgumentParser(__doc__)
    for name in ("pin", "private-root", "source-run", "calendar", "runtime", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--driver", type=Path)
    parser.add_argument("--driver-sha256")
    parser.add_argument("--driver-cwd", type=Path, default=Path.cwd())
    parser.add_argument("driver_args", nargs=REMAINDER)
    raise SystemExit(run(parser.parse_args()))
