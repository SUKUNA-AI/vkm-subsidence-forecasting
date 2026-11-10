"""Exact matrix-free static condensation of a buried vector interface."""
from __future__ import annotations

import hashlib
import json

import numpy as np
from scipy.sparse import csc_matrix
from scipy.sparse.linalg import LinearOperator, cg, splu


def signature(matrix, eliminated, context):
    k = csc_matrix(matrix, copy=True)
    k.sum_duplicates()
    k.sort_indices()
    digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode())
    for values in (k.shape, k.indptr, k.indices, k.data, np.asarray(eliminated)):
        digest.update(np.asarray(values).tobytes())
    return digest.hexdigest()


class SchurOperator:
    """Eliminate a declared internal DOF set and keep the complete inverse coupling."""

    def __init__(self, matrix, eliminated, context):
        self.k = csc_matrix(matrix, copy=True)
        self.i = np.unique(np.asarray(eliminated, dtype=int))
        self.r = np.setdiff1d(np.arange(self.k.shape[0]), self.i)
        if not len(self.i) or not len(self.r) or self.i[0] < 0 or self.i[-1] >= self.k.shape[0]:
            raise ValueError("bad internal degree-of-freedom partition")
        self.bound_signature = signature(self.k, self.i, context)
        d = self.k.diagonal()
        if np.any(d <= 0):
            raise ValueError("positive stiffness diagonal required")
        self.scale = 1/np.sqrt(d)
        scale = self.scale
        a = self.k.multiply(scale[:, None]).multiply(scale[None, :]).tocsc()
        self.ii = a[self.i][:, self.i]
        self.rr = a[self.r][:, self.r]
        self.ir = a[self.i][:, self.r]
        self.ri = a[self.r][:, self.i]
        self.factor_i = splu(self.ii, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0)
        self.factor_r = splu(self.rr, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0)
        self.operator = LinearOperator(self.rr.shape, matvec=self.schur, dtype=float)
        self.preconditioner = LinearOperator(self.rr.shape, matvec=self.factor_r.solve, dtype=float)

    def assert_applicable(self, matrix, eliminated, context):
        if signature(matrix, np.unique(eliminated), context) != self.bound_signature:
            raise ValueError("stale static-condensation operator")

    def schur(self, vector):
        return self.rr @ vector - self.ri @ self.factor_i.solve(self.ir @ vector)

    def solve(self, force):
        b = self.scale * force
        body_i = self.factor_i.solve(b[self.i])
        reduced_rhs = b[self.r] - self.ri @ body_i
        iterations = 0

        def callback(_):
            nonlocal iterations
            iterations += 1

        q, info = cg(self.operator, reduced_rhs, M=self.preconditioner, rtol=2e-12,
                     atol=1e-12, maxiter=400, callback=callback)
        if info != 0:
            raise RuntimeError(f"Schur balance failed, cg status {info}")
        us = np.empty_like(b)
        us[self.r] = q
        us[self.i] = self.factor_i.solve(b[self.i] - self.ir @ q)
        return self.scale * us, {
            "iterations": iterations,
            "reduced_work_j": float(q @ reduced_rhs),
            "full_quadratic_work_from_reduced_j": float(q @ self.schur(q) + b[self.i] @ body_i),
            "reduced_force_residual_scaled": float(np.max(np.abs(self.schur(q) - reduced_rhs))),
        }
