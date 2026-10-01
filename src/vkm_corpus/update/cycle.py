"""Bounded, resumable stage execution with immutable attempt receipts.

Adapter results are receipts, never shell output guessed to be PASS. Runtime
adapters must be registered by local code; arbitrary commands from a manifest
are deliberately not part of this interface.
"""
from __future__ import annotations

import json
import hashlib
import copy
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from vkm_corpus.parquet.atomic import create_exclusive, sha256_of, write_bytes
from vkm_corpus.update.contracts import CampaignManifest
from vkm_evidence.contracts import canonical_bytes, record_hash


class UpdateCycle:
    def __init__(self, root: Path, originals: Path, *, producer_guard: Callable[[], dict],
                 adapters: dict[str, Callable], memory_available: Callable[[], float],
                 gate_verifier: Callable[[str], bool]):
        self.root, self.originals = Path(root).resolve(), Path(originals).resolve()
        self.guard, self.adapters, self.memory_available = producer_guard, adapters, memory_available
        self.gate_verifier = gate_verifier

    def plan(self, campaign: CampaignManifest) -> dict:
        identity = self.guard()
        reasons = []
        identity_sha = identity.get("identity_sha256", record_hash(identity))
        if identity.get("code_revision", identity.get("commit")) != campaign.code_commit or identity_sha != campaign.producer_identity_sha256:
            reasons.append("PRODUCER_IDENTITY_CHANGED")
        if identity.get("code_dirty", identity.get("dirty")) is not False or identity.get("profile") != "production":
            reasons.append("PRODUCER_NOT_QUALIFIED")
        for item in campaign.inputs:
            if item.lifecycle != "ACTIVE":
                continue
            path = self.originals / item.logical_path
            if path.is_symlink() or not path.resolve().is_relative_to(self.originals):
                reasons.append("INPUT_PATH_REJECTED:" + item.source_id)
            elif not path.is_file() or path.stat().st_size != item.size_bytes or sha256_of(path) != item.source_sha256:
                reasons.append("INPUT_BYTES_CHANGED:" + item.source_id)
        for gate in campaign.gates:
            if gate.status != "PASS" or not self.gate_verifier(gate.evidence_sha256):
                reasons.append("QUALIFICATION_GATE:" + gate.gate_id)
        for stage in campaign.stages:
            if stage.operation not in self.adapters:
                reasons.append("ADAPTER_UNAVAILABLE:" + stage.operation)
        body = {"campaign_sha256": campaign.sha256, "producer_identity_sha256": identity_sha,
                "status": "READY" if not reasons else "BLOCKED", "reasons": sorted(set(reasons)),
                "stages": [s.model_dump(mode="json") for s in campaign.stages]}
        return {**body, "plan_sha256": record_hash(body)}

    def _path(self, name):
        path = self.root / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("update path escapes runtime root")
        return path

    def status(self, campaign: CampaignManifest) -> dict:
        path = self._path(campaign.sha256 + ".state.json")
        if not path.exists():
            return {"campaign_sha256": campaign.sha256, "status": "NOT_RUN", "stages": {}}
        state = json.loads(path.read_bytes())
        if (not isinstance(state, dict) or set(state) != {"campaign_sha256", "status", "stages"} or
                state["campaign_sha256"] != campaign.sha256 or not isinstance(state["stages"], dict)):
            raise ValueError("update state identity mismatch")
        stages = {s.stage_id: s for s in campaign.stages}
        if set(state["stages"]) - stages.keys():
            raise ValueError("update state contains unknown stage")
        if state["status"] not in {"NOT_RUN", "RUNNING", "PASS", "FAIL", "BLOCKED"}:
            raise ValueError("invalid persisted cycle status")
        if state["status"] == "PASS" and (set(state["stages"]) != stages.keys() or
                any(not isinstance(e, dict) or e.get("status") != "PASS" for e in state["stages"].values())):
            raise ValueError("cycle PASS contradicts stage selectors")
        receipts = {}
        for stage_id, entry in state["stages"].items():
            result = self._receipt(campaign, stages[stage_id], entry)
            receipts[stage_id] = result
            if entry["status"] == "PASS":
                for output in result["outputs"]:
                    path = self._path(output["path"])
                    if not path.is_file() or sha256_of(path) != output["sha256"]:
                        entry["status"] = "INVALIDATED"
                        break
        for stage in campaign.stages:
            entry = state["stages"].get(stage.stage_id)
            if entry and entry["status"] in {"PASS", "RUNNING"} and any(
                    state["stages"].get(d, {}).get("status") != "PASS" or
                    receipts[stage.stage_id]["dependency_receipts"][d] != state["stages"][d]["receipt_sha256"]
                    for d in stage.depends_on):
                entry["status"] = "INVALIDATED"
        if any(e["status"] == "INVALIDATED" for e in state["stages"].values()):
            state["status"] = "INVALIDATED"
        return state

    @staticmethod
    def _request(campaign, stage):
        return record_hash({"campaign": campaign.sha256, "stage": stage.stage_id,
                            "input": stage.input_sha256, "config": stage.config_sha256})

    def _outputs(self, result):
        outputs = result.get("outputs")
        if not isinstance(outputs, list):
            raise ValueError("stage outputs must be an explicit list")
        seen = set()
        for output in outputs:
            if (not isinstance(output, dict) or set(output) != {"path", "sha256"} or
                    not isinstance(output["path"], str) or not output["path"] or
                    not isinstance(output["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", output["sha256"])):
                raise ValueError("invalid stage output identity")
            path = self._path(output["path"])
            if path in seen:
                raise ValueError("duplicate stage output")
            seen.add(path)

    def _receipt(self, campaign, stage, entry, *, derived=False):
        if (not isinstance(entry, dict) or set(entry) != {"status", "attempts", "receipt_sha256"} or
                type(entry["attempts"]) is not int or not 1 <= entry["attempts"] <= stage.attempts or
                not isinstance(entry["receipt_sha256"], str) or
                not re.fullmatch(r"[0-9a-f]{64}", entry["receipt_sha256"])):
            raise ValueError("invalid stage selector")
        data = self._path(entry["receipt_sha256"] + ".receipt.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["receipt_sha256"]:
            raise ValueError("update receipt changed")
        result = json.loads(data)
        owner = {"campaign_sha256": campaign.sha256, "stage_id": stage.stage_id,
                 "request_id": self._request(campaign, stage), "attempt": entry["attempts"]}
        if (not isinstance(result, dict) or any(result.get(k) != v for k, v in owner.items()) or
                type(result.get("attempt")) is not int):
            raise ValueError("update receipt owner identity mismatch")
        dependencies = result.get("dependency_receipts")
        if (not isinstance(dependencies, dict) or set(dependencies) != set(stage.depends_on) or
                any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in dependencies.values())):
            raise ValueError("receipt dependency identities missing or malformed")
        status = result.get("status")
        if (status not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN", "RUNNING"} or
                (entry["status"] != status and not (derived and entry["status"] == "INVALIDATED" and status in {"PASS", "RUNNING"}))):
            raise ValueError("stage selector status contradicts receipt")
        self._outputs(result)
        if status == "RUNNING" and result["outputs"]:
            raise ValueError("RUNNING intent cannot claim completed outputs")
        return result

    def execute(self, campaign: CampaignManifest, confirmation: str, *, allow_gpu: bool = False) -> dict:
        plan = self.plan(campaign)
        if plan["status"] != "READY" or confirmation != plan["plan_sha256"]:
            raise ValueError("fresh qualified plan confirmation required")
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self._path("cycle.lock")
        token = uuid.uuid4().hex
        if not create_exclusive(lock, token):
            raise ValueError("update cycle busy; interrupted leases require recovery")
        try:
            state = self.status(campaign)
            for stage in campaign.stages:
                previous = state["stages"].get(stage.stage_id, {})
                if previous.get("status") == "PASS":
                    continue
                if any(state["stages"].get(d, {}).get("status") != "PASS" for d in stage.depends_on):
                    raise ValueError("stage dependency incomplete")
                if stage.compute == "GPU" and not allow_gpu:
                    raise ValueError("GPU workload not enabled")
                if stage.memory_gib > self.memory_available():
                    raise ValueError("stage memory guard rejected")
                if shutil.disk_usage(self.root).free / 2**30 < stage.min_free_disk_gib:
                    raise ValueError("stage disk guard rejected")
                if self.plan(campaign)["plan_sha256"] != confirmation:
                    raise ValueError("inputs or producer changed during cycle")
                attempts = previous.get("attempts", 0)
                resume_inflight = previous.get("status") == "RUNNING"
                if attempts >= stage.attempts and not resume_inflight:
                    raise ValueError("bounded stage retry exhausted")
                attempt_number = attempts if resume_inflight else attempts + 1
                request_id = self._request(campaign, stage)
                dependencies = {d: state["stages"][d]["receipt_sha256"] for d in stage.depends_on}
                # Durable intent precedes adapter execution. A retry gets the same
                # request ID, so an interrupted side effect must be idempotent.
                state["stages"][stage.stage_id] = {"status": "RUNNING", "attempts": attempt_number,
                    "receipt_sha256": self._intent(campaign, stage, request_id, attempt_number, dependencies)}
                state["status"] = "RUNNING"
                self._save(campaign, state)
                try:
                    result = self.adapters[stage.operation](stage, request_id, self.root)
                    if (not isinstance(result, dict) or
                            {"campaign_sha256", "stage_id", "request_id", "attempt", "attempts", "receipt_sha256",
                             "dependency_receipts"}.intersection(result)):
                        raise ValueError("adapter cannot override receipt owner fields")
                    if result.get("status") not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}:
                        raise ValueError("invalid stage result status")
                    self._outputs(result)
                    for output in result["outputs"]:
                        path = self._path(output["path"])
                        if sha256_of(path) != output["sha256"]:
                            raise ValueError("stage output hash mismatch")
                    status = result["status"]
                except Exception as exc:
                    result = {"status": "FAIL", "error_type": type(exc).__name__, "outputs": []}
                    status = "FAIL"
                receipt = {**result, "campaign_sha256": campaign.sha256, "stage_id": stage.stage_id,
                           "request_id": request_id, "attempt": attempt_number, "dependency_receipts": dependencies}
                data = canonical_bytes(receipt)
                sha = hashlib.sha256(data).hexdigest()
                write_bytes(self._path("tmp"), self._path(sha + ".receipt.json"), data)
                state["stages"][stage.stage_id] = {"status": status, "attempts": attempt_number, "receipt_sha256": sha}
                state["status"] = "RUNNING" if status == "PASS" else status
                self._save(campaign, state)
                if status != "PASS":
                    return state
            state["status"] = "PASS"
            self._save(campaign, state)
            return state
        finally:
            if lock.read_text(encoding="ascii") != token:
                raise ValueError("cycle lock owner changed")
            lock.unlink()

    def _intent(self, campaign, stage, request_id, attempt, dependencies):
        data = canonical_bytes({"campaign_sha256": campaign.sha256, "status": "RUNNING", "request_id": request_id,
                                "stage_id": stage.stage_id, "attempt": attempt, "dependency_receipts": dependencies,
                                "outputs": []})
        sha = hashlib.sha256(data).hexdigest()
        write_bytes(self._path("tmp"), self._path(sha + ".receipt.json"), data)
        return sha

    def _save(self, campaign, state):
        # INVALIDATED is a derived observation, never a contradictory selector
        # persisted over a PASS/RUNNING receipt. Recompute bytes/dependencies on read.
        stored = copy.deepcopy(state)
        stages = {s.stage_id: s for s in campaign.stages}
        for stage_id, entry in stored["stages"].items():
            entry["status"] = self._receipt(campaign, stages[stage_id], entry, derived=True)["status"]
        write_bytes(self._path("tmp"), self._path(campaign.sha256 + ".state.json"), canonical_bytes(stored), overwrite=True)
