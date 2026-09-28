"""Scoring head of jina-reranker-m0 outside llama.cpp (``mlp_weights.npz`` of ``jinaai/jina-reranker-m0-GGUF``).

The GGUF holds only the language model; llama-server returns the last hidden state (``--pooling last``,
``embd_normalize = -1``) of the score token, and the score is
``sigmoid(relu(h · W1 + b1) · W2 + b2 − logit_bias)`` — the same as ``JinaVLForRanking.forward``
(``sigmoid(score(h_last) − 2.65)``). numpy is imported lazily.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HIDDEN_SIZE = 1536
EXPECTED_LOGIT_BIAS = 2.65


@dataclass(frozen=True)
class M0Head:
    w1: Any
    b1: Any
    w2: Any
    b2: Any
    logit_bias: float
    sha256: str

    @classmethod
    def from_npz(cls, path: str | Path, expected_sha256: str | None = None) -> "M0Head":
        import numpy as np

        raw = Path(path).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise ValueError(f"mlp_weights.npz sha256 mismatch: {digest} != {expected_sha256}")
        with np.load(Path(path)) as data:
            w1, b1 = data["W1"].astype(np.float32), data["b1"].astype(np.float32)
            w2, b2 = data["W2"].astype(np.float32), data["b2"].astype(np.float32)
            bias = float(np.asarray(data["logit_bias"]).reshape(-1)[0])
        if w1.shape != (HIDDEN_SIZE, HIDDEN_SIZE) or b1.shape != (HIDDEN_SIZE,):
            raise ValueError(f"unexpected W1/b1 shapes {w1.shape} {b1.shape}")
        if w2.reshape(-1).shape != (HIDDEN_SIZE,) or b2.reshape(-1).shape != (1,):
            raise ValueError(f"unexpected W2/b2 shapes {w2.shape} {b2.shape}")
        return cls(w1=w1, b1=b1, w2=w2.reshape(HIDDEN_SIZE, 1), b2=b2.reshape(1), logit_bias=bias, sha256=digest)

    def logits(self, hidden: Any) -> Any:
        import numpy as np

        x = np.asarray(hidden, dtype=np.float32)
        if x.ndim == 1:
            x = x[None, :]
        if x.shape[-1] != HIDDEN_SIZE:
            raise ValueError(f"hidden size {x.shape[-1]} != {HIDDEN_SIZE}")
        h = np.maximum(0.0, x @ self.w1 + self.b1)
        return (h @ self.w2 + self.b2).reshape(-1) - np.float32(self.logit_bias)

    def scores(self, hidden: Any) -> list[float]:
        import numpy as np

        z = self.logits(hidden).astype(np.float64)
        return [float(v) for v in 1.0 / (1.0 + np.exp(-z))]
