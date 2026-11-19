"""Optional exact coefficient storage for fixed-basis Maxwell rock groups.

A group stores only the union of structural row and column addresses of its
virgin operator. A caller must qualify a rock removal independently. This
adapter checks its own fixed-law, fixed-basis and no-new-coordinate contract.
New fill cohorts own independent state.
"""
from dataclasses import dataclass
import hashlib
import json
from numbers import Integral

import numpy as np

from .maxwell_coefficients import (
    MaxwellGroup, _check_csr, _immutable_copy, _real_array, ratios, update,
)


@dataclass(frozen=True)
class SupportedGroup:
    """Stable law identity and its CSR on sorted global basis addresses."""

    signature: str
    addresses: np.ndarray
    operator: MaxwellGroup


@dataclass(frozen=True)
class SupportedMemory:
    layout_sha256: str
    operators_sha256: str
    coefficients: np.ndarray


def _support(matrix):
    # CSR may retain explicit zeros. Keeping those addresses is harmless.
    rows = np.flatnonzero(matrix.indptr[1:] > matrix.indptr[:-1])
    return np.union1d(rows, matrix.indices)


def _hash_arrays(digest, arrays):
    for array in arrays:
        digest.update(json.dumps([array.dtype.str, array.shape]).encode())
        digest.update(memoryview(array).cast("B"))


def _array_for_basis(value, size):
    result = _real_array(value, 1, "finite supported Maxwell displacement required")
    if result.shape != (size,):
        raise ValueError("supported Maxwell displacements must match the fixed basis")
    return result.astype(np.float64, copy=False)


class FixedSupportHistory:
    """Layout and one admitted operator stage for fixed-group Maxwell rock.

    Compact input can be built directly by a sparse assembler. All addresses
    remain fixed during a chain, even if their operator row goes dormant.
    """

    def __init__(self, basis_signature, basis_size, groups):
        if not isinstance(basis_signature, str) or not basis_signature:
            raise ValueError("fixed basis signature required")
        if (not isinstance(basis_size, Integral) or isinstance(basis_size, bool)
                or not 0 < basis_size <= np.iinfo(np.int32).max):
            raise ValueError("positive int32-supported basis size required")
        self.basis_signature = basis_signature
        self.basis_size = int(basis_size)
        layout = hashlib.sha256(json.dumps(
            ["vkm.fixed-support-maxwell/1", basis_signature, self.basis_size],
        ).encode())
        operators = hashlib.sha256()
        entries, seen, offsets = [], set(), [0]
        for group in groups:
            signature = group.signature
            if not isinstance(signature, str) or not signature or signature in seen:
                raise ValueError("unique ordered Maxwell group signatures required")
            seen.add(signature)
            ids = np.asarray(group.addresses)
            if (ids.ndim != 1 or ids.dtype.kind not in "iu"
                    or np.any(ids < 0) or np.any(ids >= basis_size)
                    or np.any(ids[1:] <= ids[:-1])):
                raise ValueError("strictly sorted addresses within the fixed basis required")
            indices = _immutable_copy(ids.astype("<i4", copy=False))
            mat = group.operator._checked_stiffness(len(indices))
            entries.append(SupportedGroup(signature, indices, group.operator))
            offsets.append(offsets[-1] + len(indices))
            layout.update(json.dumps([signature, group.operator.rate_per_s.hex()]).encode())
            _hash_arrays(layout, (indices,))
            _hash_arrays(operators, (mat.data, mat.indices, mat.indptr))
        self.groups = tuple(entries)
        self._slices = tuple(slice(a,b) for a,b in zip(offsets[:-1],offsets[1:],strict=True))
        self.coefficient_count = offsets[-1]
        self.layout_sha256 = layout.hexdigest()
        operators.update(layout.digest())
        self.operators_sha256 = operators.hexdigest()
        self._terms = tuple(entry.operator for entry in self.groups)

    @classmethod
    def from_full_groups(cls, basis_signature, basis_size, signatures, groups):
        """Compress qualified full CSR operators. No dense G-by-DOF state is made."""
        pieces = []
        for signature, group in zip(signatures, groups, strict=True):
            mat = group._checked_stiffness(basis_size)
            ids = _support(mat)
            pieces.append(SupportedGroup(signature, ids, MaxwellGroup(
                group.rate_per_s, mat[ids][:, ids].tocsr(),
            )))
        return cls(basis_signature, basis_size, pieces)

    @staticmethod
    def _addresses(group, slot):
        ids = group.addresses
        if (ids.shape != (slot.stop - slot.start,) or ids.dtype != np.dtype("<i4")
                or ids.strides != (4,) or ids.flags.writeable):
            raise ValueError("changed fixed-support address buffer")
        return ids

    def _state_values(self, state):
        if (state.layout_sha256 != self.layout_sha256
                or state.operators_sha256 != self.operators_sha256):
            raise ValueError("checkpoint basis, group order/law or operator stage mismatch")
        values = _real_array(state.coefficients, 1, "finite supported Maxwell memory required")
        if values.shape != (self.coefficient_count,) or values.dtype != np.dtype("<f8"):
            raise ValueError("checkpoint fixed support or float64 representation mismatch")
        return values

    def _state(self, values):
        return SupportedMemory(self.layout_sha256, self.operators_sha256, _immutable_copy(values))

    def virgin(self):
        """Create zero coefficients for a qualified virgin state."""
        return self._state(np.zeros(self.coefficient_count, dtype="<f8"))

    def pack(self, dense):
        """Project already qualified dense coefficients on this same basis and group order."""
        q = _real_array(dense, 2, "finite dense Maxwell coefficients required")
        if q.shape != (len(self.groups), self.basis_size):
            raise ValueError("dense checkpoint must match the fixed basis and group order")
        values = np.empty(self.coefficient_count, dtype="<f8")
        for group, slot, old in zip(self.groups, self._slices, q, strict=True):
            values[slot] = old[self._addresses(group, slot)]
        return self._state(values)

    def reopen(self, coefficients, layout_sha256, operators_sha256):
        """Validate previous checkpoint bytes against the supplied exact stage."""
        candidate = SupportedMemory(layout_sha256, operators_sha256, coefficients)
        return self._state(self._state_values(candidate))

    def after_removal(self, old, groups):
        """Move unchanged memory to an independently qualified rock-removal stage.

        Fixed union addresses and group identities must match. A dormant
        coordinate may not reactivate. A diagonal increase beyond float64
        roundoff is also rejected. The physical removal proof belongs to the
        caller's event contract and is separate from this storage check.
        """
        values = self._state_values(old)
        stage = type(self)(self.basis_signature, self.basis_size, groups)
        if stage.layout_sha256 != self.layout_sha256:
            raise ValueError("changed fixed group order, law, basis or support addresses")
        for before, after, slot in zip(self.groups, stage.groups, self._slices, strict=True):
            n = len(self._addresses(before, slot))
            a = before.operator._checked_stiffness(n)
            b = after.operator._checked_stiffness(n)
            if np.setdiff1d(_support(b), _support(a), assume_unique=True).size:
                raise ValueError("rock removal cannot activate a group coordinate")
            d0, d1 = a.diagonal(), b.diagonal()
            limit = 64*np.finfo(float).eps*np.maximum(np.abs(d0),np.abs(d1))
            if np.any(d1-d0 > limit):
                raise ValueError("rock removal cannot increase group diagonal")
        return stage, stage._state(values)

    def update(self, old, displacement, dt_s):
        values = self._state_values(old)
        u = _array_for_basis(displacement, self.basis_size)
        r = np.asarray(ratios(self._terms, dt_s))
        trial = np.empty_like(values)
        for group, slot, ratio in zip(self.groups, self._slices, r, strict=True):
            dofs = self._addresses(group, slot)
            trial[slot] = update(values[None, slot], u[dofs], np.asarray([ratio]))[0]
        return self._state(trial)

    def memory_load(self, state, dt_s):
        values = self._state_values(state)
        r = ratios(self._terms, dt_s)
        force = np.zeros(self.basis_size)
        for group, slot, ratio in zip(self.groups,self._slices,r,strict=True):
            dofs = self._addresses(group, slot)
            operator = group.operator._checked_stiffness(len(dofs))
            force[dofs] += (operator @ values[slot]) / (1.+ratio)
        if not np.isfinite(force).all():
            raise ValueError("nonfinite supported Maxwell load")
        return force

    def work_terms(self, elastic, u0, u1, old, trial, dt_s):
        """Fixed-operator work: stored, viscous loss and endpoint excess."""
        _check_csr(elastic)
        if elastic.shape != (self.basis_size,self.basis_size):
            raise ValueError("elastic operator does not match the fixed basis")
        u0 = _array_for_basis(u0,self.basis_size)
        u1 = _array_for_basis(u1,self.basis_size)
        q0 = self._state_values(old)
        q1 = self._state_values(trial)
        r = ratios(self._terms,dt_s)
        du = u1-u0
        um = .5*(u1+u0)
        stored = float(du @ (elastic @ um))
        excess = .5*float(du @ (elastic @ du))
        dissipated = 0.
        for group,slot,ratio in zip(self.groups,self._slices,r,strict=True):
            dofs = self._addresses(group, slot)
            S = group.operator._checked_stiffness(len(dofs))
            q_old,q_new = q0[slot],q1[slot]
            if not ratio and not np.array_equal(q_old,q_new):
                raise ValueError("supported Maxwell memory changed at zero ratio")
            dq = q_new-q_old
            qm = .5*(q_new+q_old)
            local_du,local_um = du[dofs],um[dofs]
            S_dq = S @ dq
            dq2 = float(dq @ S_dq)
            stored -= float(local_du @ (S @ qm)) + float((local_um-qm) @ S_dq)
            excess += .5*dq2-float(local_du @ S_dq)
            if ratio:
                dissipated += dq2/ratio
        if not np.isfinite([stored,dissipated,excess]).all():
            raise ValueError("nonfinite supported Maxwell work")
        return stored,dissipated,excess
