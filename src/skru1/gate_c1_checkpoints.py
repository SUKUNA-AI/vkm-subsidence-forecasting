"""Fail-closed, work-only checkpoint management for Gate C1.

Ranked checkpoints are full training states.  Inner fits rank epochs by the
frozen early-stopping objective; fixed-epoch outer refits retain the five most
recent epochs and always select the preregistered final epoch.  No outer label
is accepted by this module.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import random
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
import torch

from .data_contracts import ContractViolation, sha256_file
from .gate_c1_interfaces import canonical_json_sha256


INNER_POLICY = "metric_ascending_then_epoch"
OUTER_POLICY = "latest_epoch_fixed_final"


class TopKCheckpointManager:
    """Retain five ranked states and one resumable stage checkpoint."""

    def __init__(
        self,
        *,
        root: Path,
        fit_id: str,
        role: str,
        keep_top_k: int,
        stage_interval_epochs: int,
        provenance: Mapping[str, Any],
    ) -> None:
        self.root = root.resolve()
        self.fit_id = str(fit_id)
        self.role = str(role)
        self.keep_top_k = int(keep_top_k)
        self.stage_interval_epochs = int(stage_interval_epochs)
        self.provenance = dict(provenance)
        if len(self.fit_id) != 64 or any(character not in "0123456789abcdef" for character in self.fit_id):
            raise ContractViolation("Gate C1 checkpoint fit_id must be a lowercase SHA-256")
        if self.role not in {"inner", "outer"}:
            raise ContractViolation("Gate C1 checkpoint role must be inner or outer")
        if self.keep_top_k != 5:
            raise ContractViolation("Gate C1 must retain exactly top five checkpoints")
        if self.stage_interval_epochs < 1:
            raise ContractViolation("Gate C1 checkpoint stage interval must be positive")
        if any("label" in str(key).lower() for key in self.provenance):
            raise ContractViolation("Checkpoint provenance must not contain outer labels")
        self.context_sha256 = canonical_json_sha256(
            {
                "fit_id": self.fit_id,
                "role": self.role,
                "keep_top_k": self.keep_top_k,
                "stage_interval_epochs": self.stage_interval_epochs,
                "provenance": self.provenance,
            }
        )
        self.directory = self._resolve_work_directory()
        self.recovery_path = self.directory / "latest_stage.pt"
        self.recovery_sidecar_path = self.directory / "latest_stage.json"
        self.manifest_path = self.directory / "manifest.json"
        self.ranked: list[dict[str, Any]] = []
        self.resumed_from_recovery = False

    @property
    def ranking_policy(self) -> str:
        return INNER_POLICY if self.role == "inner" else OUTER_POLICY

    def resume(
        self,
        *,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        shuffle_generator: torch.Generator,
        device: torch.device,
    ) -> dict[str, Any] | None:
        """Restore the last completed stage only after sidecar/hash checks."""

        if not self.recovery_path.exists() and not self.recovery_sidecar_path.exists():
            return None
        if not self.recovery_path.is_file() or not self.recovery_sidecar_path.is_file():
            raise ContractViolation("Gate C1 checkpoint recovery record is partial")
        sidecar = json.loads(self.recovery_sidecar_path.read_text(encoding="utf-8"))
        if sidecar.get("context_sha256") != self.context_sha256:
            raise ContractViolation("Gate C1 checkpoint recovery context changed")
        if sidecar.get("checkpoint_sha256") != sha256_file(self.recovery_path):
            raise ContractViolation("Gate C1 recovery checkpoint hash mismatch")
        payload = torch.load(self.recovery_path, map_location=device, weights_only=False)
        if payload.get("context_sha256") != self.context_sha256:
            raise ContractViolation("Gate C1 recovery payload context changed")
        model.load_state_dict(payload["current"]["model_state_dict"])
        optimizer.load_state_dict(payload["current"]["optimizer_state_dict"])
        shuffle_generator.set_state(payload["current"]["shuffle_generator_state"].cpu())
        torch.set_rng_state(payload["current"]["torch_cpu_rng_state"].cpu())
        if device.type == "cuda":
            torch.cuda.set_rng_state(payload["current"]["torch_cuda_rng_state"].cpu(), device)
        random.setstate(payload["current"]["python_rng_state"])
        np.random.set_state(payload["current"]["numpy_rng_state"])
        self.ranked = list(payload["ranked"])
        self.resumed_from_recovery = True
        return {
            "epoch": int(payload["epoch"]),
            "stale_epochs": int(payload["stale_epochs"]),
            "terminal": bool(payload["terminal"]),
        }

    def observe(
        self,
        *,
        epoch: int,
        metric: float,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        shuffle_generator: torch.Generator,
        stale_epochs: int,
        device: torch.device,
        force_stage: bool = False,
        terminal: bool = False,
    ) -> None:
        """Rank the epoch and atomically persist the latest recovery stage."""

        epoch = int(epoch)
        metric = float(metric)
        if epoch < 1 or not np.isfinite(metric):
            raise ContractViolation("Gate C1 checkpoint epoch/metric is invalid")
        rank_key = self._rank_key(epoch, metric)
        qualifies = len(self.ranked) < self.keep_top_k or rank_key < self._rank_key(
            int(self.ranked[-1]["epoch"]), float(self.ranked[-1]["metric"])
        )
        current = None
        if qualifies:
            current = self._capture_state(
                epoch=epoch,
                metric=metric,
                model=model,
                optimizer=optimizer,
                shuffle_generator=shuffle_generator,
                device=device,
            )
            self.ranked.append(current)
            self.ranked.sort(key=lambda record: self._rank_key(record["epoch"], record["metric"]))
            del self.ranked[self.keep_top_k :]
        if force_stage or terminal or epoch % self.stage_interval_epochs == 0:
            if current is None:
                current = self._capture_state(
                    epoch=epoch,
                    metric=metric,
                    model=model,
                    optimizer=optimizer,
                    shuffle_generator=shuffle_generator,
                    device=device,
                )
            self._write_recovery(
                epoch=epoch,
                stale_epochs=int(stale_epochs),
                terminal=bool(terminal),
                current=current,
            )

    def finalize(self, *, model: torch.nn.Module) -> dict[str, Any]:
        """Persist ranked states, restore the selected state, and write a manifest."""

        if not self.ranked:
            raise ContractViolation("Gate C1 checkpoint manager has no finite ranked state")
        self.directory.mkdir(parents=True, exist_ok=True)
        records = []
        for rank, state in enumerate(self.ranked, start=1):
            path = self.directory / f"rank_{rank:02d}.pt"
            self._torch_save_atomic(path, self._cpu_tree(state))
            records.append(
                {
                    "rank": rank,
                    "epoch": int(state["epoch"]),
                    "metric": float(state["metric"]),
                    "path": path.relative_to(self.root).as_posix(),
                    "sha256": sha256_file(path),
                    "full_training_state": True,
                }
            )
        selected = self.ranked[0]
        model.load_state_dict(selected["model_state_dict"])
        recovery_sidecar = json.loads(self.recovery_sidecar_path.read_text(encoding="utf-8"))
        manifest = {
            "schema_version": 1,
            "status": "COMPLETE",
            "fit_id": self.fit_id,
            "role": self.role,
            "context_sha256": self.context_sha256,
            "ranking_policy": self.ranking_policy,
            "selection_policy": "rank_1_inner_objective" if self.role == "inner" else "fixed_final_epoch",
            "keep_top_k": self.keep_top_k,
            "retained_checkpoint_count": len(records),
            "top_k_fully_populated": len(records) == self.keep_top_k,
            "selected_rank": 1,
            "selected_epoch": int(selected["epoch"]),
            "selected_metric": float(selected["metric"]),
            "checkpoints": records,
            "latest_stage": {
                "path": self.recovery_path.relative_to(self.root).as_posix(),
                "sha256": recovery_sidecar["checkpoint_sha256"],
                "epoch": int(recovery_sidecar["epoch"]),
                "terminal": bool(recovery_sidecar["terminal"]),
            },
            "resumed_from_recovery": self.resumed_from_recovery,
            "persistence_scope": "work_only",
            "outer_labels_used_for_ranking": False,
            "provenance": self.provenance,
        }
        manifest["manifest_content_sha256"] = canonical_json_sha256(manifest)
        self._write_json_atomic(self.manifest_path, manifest)
        return {
            "checkpoint_manifest": self.manifest_path.relative_to(self.root).as_posix(),
            "checkpoint_manifest_sha256": sha256_file(self.manifest_path),
            "checkpoint_role": self.role,
            "checkpoint_ranking_policy": self.ranking_policy,
            "retained_checkpoint_count": len(records),
            "selected_checkpoint_epoch": int(selected["epoch"]),
            "resumed_from_recovery": self.resumed_from_recovery,
        }

    def _rank_key(self, epoch: int, metric: float) -> tuple[float, int]:
        if self.role == "inner":
            return float(metric), int(epoch)
        return -float(epoch), -int(epoch)

    def _capture_state(
        self,
        *,
        epoch: int,
        metric: float,
        model: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        shuffle_generator: torch.Generator,
        device: torch.device,
    ) -> dict[str, Any]:
        return {
            "epoch": int(epoch),
            "metric": float(metric),
            "model_state_dict": self._clone_tree(model.state_dict()),
            "optimizer_state_dict": self._clone_tree(optimizer.state_dict()),
            "shuffle_generator_state": shuffle_generator.get_state().clone(),
            "torch_cpu_rng_state": torch.get_rng_state().clone(),
            "torch_cuda_rng_state": (
                torch.cuda.get_rng_state(device).clone() if device.type == "cuda" else torch.empty(0, dtype=torch.uint8)
            ),
            "python_rng_state": deepcopy(random.getstate()),
            "numpy_rng_state": deepcopy(np.random.get_state()),
        }

    def _write_recovery(
        self,
        *,
        epoch: int,
        stale_epochs: int,
        terminal: bool,
        current: Mapping[str, Any],
    ) -> None:
        payload = {
            "schema_version": 1,
            "context_sha256": self.context_sha256,
            "epoch": int(epoch),
            "stale_epochs": int(stale_epochs),
            "terminal": bool(terminal),
            "current": self._cpu_tree(current),
            "ranked": self._cpu_tree(self.ranked),
        }
        self._torch_save_atomic(self.recovery_path, payload)
        sidecar = {
            "schema_version": 1,
            "context_sha256": self.context_sha256,
            "epoch": int(epoch),
            "stale_epochs": int(stale_epochs),
            "terminal": bool(terminal),
            "checkpoint_sha256": sha256_file(self.recovery_path),
        }
        self._write_json_atomic(self.recovery_sidecar_path, sidecar)

    def _resolve_work_directory(self) -> Path:
        path = (self.root / "work" / "gate_c1" / "checkpoints" / self.role / self.fit_id).resolve()
        expected = (self.root / "work" / "gate_c1" / "checkpoints").resolve()
        try:
            path.relative_to(expected)
        except ValueError as exc:
            raise ContractViolation("Gate C1 checkpoint path escapes work/") from exc
        return path

    def _torch_save_atomic(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic = (self.root / "work" / "gate_c1" / "checkpoint_atomic").resolve()
        atomic.mkdir(parents=True, exist_ok=True)
        temporary = atomic / f"{path.name}.{uuid4().hex}.tmp"
        torch.save(payload, temporary)
        temporary.replace(path)

    def _write_json_atomic(self, path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic = (self.root / "work" / "gate_c1" / "checkpoint_atomic").resolve()
        atomic.mkdir(parents=True, exist_ok=True)
        temporary = atomic / f"{path.name}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    @classmethod
    def _clone_tree(cls, value: Any) -> Any:
        if isinstance(value, torch.Tensor):
            return value.detach().clone()
        if isinstance(value, dict):
            return {key: cls._clone_tree(child) for key, child in value.items()}
        if isinstance(value, list):
            return [cls._clone_tree(child) for child in value]
        if isinstance(value, tuple):
            return tuple(cls._clone_tree(child) for child in value)
        return deepcopy(value)

    @classmethod
    def _cpu_tree(cls, value: Any) -> Any:
        if isinstance(value, torch.Tensor):
            return value.detach().cpu().clone()
        if isinstance(value, dict):
            return {key: cls._cpu_tree(child) for key, child in value.items()}
        if isinstance(value, list):
            return [cls._cpu_tree(child) for child in value]
        if isinstance(value, tuple):
            return tuple(cls._cpu_tree(child) for child in value)
        return deepcopy(value)


def validate_checkpoint_manifest(
    root: Path,
    relative_path: str,
    expected_sha256: str,
    *,
    expected_role: str,
) -> dict[str, Any]:
    """Validate a completed work-only top-five checkpoint manifest."""

    root = root.resolve()
    path = (root / relative_path).resolve()
    expected_root = (root / "work" / "gate_c1" / "checkpoints").resolve()
    try:
        path.relative_to(expected_root)
    except ValueError as exc:
        raise ContractViolation("Checkpoint manifest escapes work/gate_c1/checkpoints") from exc
    if not path.is_file() or sha256_file(path) != expected_sha256:
        raise ContractViolation("Checkpoint manifest is missing or changed")
    payload = json.loads(path.read_text(encoding="utf-8"))
    content_digest = payload.pop("manifest_content_sha256", None)
    if content_digest != canonical_json_sha256(payload):
        raise ContractViolation("Checkpoint manifest content hash mismatch")
    payload["manifest_content_sha256"] = content_digest
    if (
        payload.get("status") != "COMPLETE"
        or payload.get("role") != expected_role
        or int(payload.get("keep_top_k", -1)) != 5
        or payload.get("outer_labels_used_for_ranking") is not False
        or payload.get("persistence_scope") != "work_only"
    ):
        raise ContractViolation("Checkpoint manifest policy mismatch")
    checkpoints = payload.get("checkpoints", [])
    if len(checkpoints) != int(payload.get("retained_checkpoint_count", -1)) or not checkpoints:
        raise ContractViolation("Checkpoint manifest retained-state count mismatch")
    for record in checkpoints:
        checkpoint = (root / str(record["path"])).resolve()
        try:
            checkpoint.relative_to(expected_root)
        except ValueError as exc:
            raise ContractViolation("Ranked checkpoint escapes work/") from exc
        if not checkpoint.is_file() or sha256_file(checkpoint) != record.get("sha256"):
            raise ContractViolation("Ranked checkpoint hash mismatch")
    return payload
