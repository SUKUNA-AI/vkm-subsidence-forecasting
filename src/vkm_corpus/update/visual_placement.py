"""Live target-weight buffer placement; no mmproj/kernel/full-residency proof."""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vkm_evidence.contracts import Sha256, StrictModel, record_hash

Pointer = Annotated[int, Field(strict=True, ge=1, le=2**64-1)]
Count = Annotated[int, Field(strict=True, ge=1, le=2**64-1)]


class TargetWeightObservation(StrictModel):
    scope: Literal["LLAMA_TARGET_WEIGHT_PLACEMENT"]
    layer_count: int = Field(strict=True, ge=1, le=1024)
    total_layer_count: int = Field(strict=True, ge=1, le=1024)
    tensor_count: int = Field(strict=True, ge=1, le=1024)
    unique_buffers: int = Field(strict=True, ge=1, le=1024)
    groups: dict[str, Literal["GPU", "HOST", "MIXED"]]
    gpu_buffer_bytes: int = Field(strict=True, ge=0, le=2**64-1)
    host_buffer_bytes: int = Field(strict=True, ge=0, le=2**64-1)
    gpu_devices: tuple[str, ...] = Field(max_length=1024)
    mmproj: Literal["NOT_PROVEN"]
    context_buffers: Literal["NOT_PROVEN"]
    kernel_execution: Literal["NOT_PROVEN"]
    full_gpu_residency: Literal["NOT_PROVEN"]
    performance: Literal["NOT_PROVEN"]


class OwnedTargetWeightPlacement(StrictModel):
    schema_version: Literal["vkm-owned-target-weight-placement/1"] = "vkm-owned-target-weight-placement/1"
    scope: Literal["VISUAL_OWNER_PRODUCTION"] = "VISUAL_OWNER_PRODUCTION"
    instance_sha256: Sha256
    process_sha256: Sha256
    witness_sha256: Sha256
    placement_sha256: Sha256
    observation: TargetWeightObservation


class TargetWeightTensor(StrictModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    group: str = Field(pattern=r"^(other|output|blk\.(0|[1-9][0-9]{0,3}))$")
    aliases: tuple[Literal["INPUT", "OUTPUT"], ...] = Field(max_length=2)
    tensor: Pointer
    storage_tensor: Pointer
    data: Pointer
    storage_data: Pointer
    buffer: Pointer
    base: Pointer
    buft: Pointer
    device: Pointer
    tensor_bytes: Count
    storage_bytes: Count
    buffer_bytes: Count
    host: bool = Field(strict=True)
    device_kind: Literal["CPU", "GPU"]
    device_name: str = Field(min_length=1, max_length=128)
    buffer_name: str = Field(min_length=1, max_length=128)
    view_chain: tuple[Pointer, ...] = Field(min_length=1, max_length=9)
    view_offsets: tuple[Annotated[int, Field(strict=True, ge=0, le=2**64-1)], ...] = Field(max_length=8)
    tensor_type: int = Field(strict=True, ge=0, le=1024)
    shape: tuple[Annotated[int, Field(strict=True, ge=1, le=2**40)], ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def _allocation(self):
        if (self.view_chain[0] != self.tensor or self.view_chain[-1] != self.storage_tensor
                or len(set(self.view_chain)) != len(self.view_chain)
                or any(s in self.buffer_name.upper() for s in ("SPLIT", "META"))
                or self.base + self.buffer_bytes > 2**64-1 or self.tensor_bytes > self.buffer_bytes
                or not self.base <= self.data <= self.base + self.buffer_bytes - self.tensor_bytes
                or len(self.view_offsets) + 1 != len(self.view_chain)
                or self.storage_bytes > self.buffer_bytes
                or not self.base <= self.storage_data <= self.base + self.buffer_bytes - self.storage_bytes
                or self.data != self.storage_data + sum(self.view_offsets)
                or self.tensor_bytes > self.storage_bytes or sum(self.view_offsets) > self.storage_bytes - self.tensor_bytes):
            raise ValueError("unsupported or inconsistent actual target-weight allocation")
        match = re.match(r"^blk\.([0-9]+)\.", self.name)
        if match and self.group != "blk." + str(int(match[1])):
            raise ValueError("target tensor group differs from native layer")
        if self.aliases and (self.aliases != ("INPUT", "OUTPUT") or self.group != "output"):
            raise ValueError("unsupported target tensor alias roles")
        return self

    @property
    def domain(self):
        # CUDA_Host can have a GPU-associated device. It remains host memory.
        return "GPU" if self.device_kind == "GPU" and not self.host else "HOST"

class TargetWeightPlacement(StrictModel):
    schema_version: Literal["vkm-native-target-weight-placement/1"]
    scope: Literal["LLAMA_TARGET_WEIGHT_PLACEMENT"]
    model_handle: Pointer
    context_handle: Pointer
    no_alloc: bool = Field(strict=True)
    layer_count: int = Field(strict=True, ge=1, le=1024)
    total_layer_count: int = Field(strict=True, ge=1, le=1024)
    tensors: tuple[TargetWeightTensor, ...] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _identities(self):
        if self.no_alloc:
            raise ValueError("target weights were not actually allocated")
        identities = [(value.name, value.tensor) for value in self.tensors]
        if self.layer_count > self.total_layer_count or identities != sorted(set(identities)):
            raise ValueError("target tensor occurrences must be unique in stable order")
        buffers = {}
        for value in self.tensors:
            if value.group.startswith("blk.") and int(value.group[4:]) >= self.total_layer_count:
                raise ValueError("target tensor layer outside actual model")
            identity = (value.base, value.buft, value.device, value.buffer_bytes, value.host,
                        value.device_kind, value.device_name, value.buffer_name)
            if value.buffer in buffers and buffers[value.buffer] != identity:
                raise ValueError("conflicting target buffer identity")
            buffers[value.buffer] = identity
        return self

    @property
    def sha256(self):
        # Every tensor/view/data/buffer/device binding is hashed, never counts only.
        return record_hash(self)

    def summary(self):
        groups, allocations = {}, {}
        for value in self.tensors:
            groups.setdefault(value.group, set()).add(value.domain)
            if value.aliases:
                groups.setdefault("other", set()).add(value.domain)
            allocations[value.buffer] = value
        classified = {name: next(iter(domains)) if len(domains) == 1 else "MIXED"
                      for name, domains in sorted(groups.items())}
        cuda = {value.device_name for value in self.tensors if value.domain == "GPU"}
        return {"layer_count": self.layer_count, "total_layer_count": self.total_layer_count, "tensor_count": len(self.tensors),
                "unique_buffers": len(allocations), "groups": classified,
                "gpu_buffer_bytes": sum(v.buffer_bytes for v in allocations.values() if v.domain == "GPU"),
                "host_buffer_bytes": sum(v.buffer_bytes for v in allocations.values() if v.domain == "HOST"),
                "gpu_devices": sorted(cuda), "scope": self.scope,
                "mmproj": "NOT_PROVEN", "context_buffers": "NOT_PROVEN", "kernel_execution": "NOT_PROVEN",
                "full_gpu_residency": "NOT_PROVEN", "performance": "NOT_PROVEN"}


def require_cuda_weight_profile(placement: TargetWeightPlacement, *, groups=20, layers=28):
    """Qualification of this workload; generic owners can observe host placement."""
    summary = placement.summary()
    # Pinned native selection: start = n_layer_all + 1 - n_gpu_layers;
    # output is assigned as layer n_layer_all. Check exact initial topology.
    first = max(layers + 1 - groups, 0)
    expected = {"blk." + str(i): "HOST" if i < first else "GPU" for i in range(layers)}
    expected.update(output="GPU", other="HOST")
    if (summary["layer_count"] != layers or summary["total_layer_count"] != layers
            or summary["gpu_devices"] != ["CUDA0"] or summary["gpu_buffer_bytes"] <= 0
            or summary["groups"] != expected):
        raise ValueError("actual CUDA target-weight groups do not meet the approved profile")
    return summary
