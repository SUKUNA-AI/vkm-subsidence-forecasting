"""Deterministic test doubles (no models, no GPU): a word-level tokenizer with the ``tokenizers`` API subset used by
:class:`~vkm_corpus.embeddings.tokenize.SpecTokenizer`, an encoder backend with the ``llama-server`` client API and an
OpenSearch transport. Used by ``tests/corpus/test_embeddings_*`` and ``test_retrieval_service_*``."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from vkm_corpus.embeddings.llama import EmbedResult

_WORD = re.compile(r"\w+|[^\w\s]", re.UNICODE)


@dataclass
class _Enc:
    ids: list[int]


class FakeTokenizer:
    """Word/punctuation tokenizer. ``bos``/``eos`` emulate a post-processor (``<s> A </s>`` or ``A <eos>``); ids are
    stable within one instance (vocabulary grows on demand). Truncation keeps the specials, like ``tokenizers``."""

    def __init__(self, *, bos: str | None = "<s>", eos: str | None = "</s>",
                 specials: Sequence[str] = ("<pad>", "<unk>", "<mask>")) -> None:
        self.vocab: dict[str, int] = {}
        for t in (*specials, *(x for x in (bos, eos) if x)):
            self._id(t)
        for ch in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":   # a real vocabulary knows punctuation from the start
            self._id(ch)
        self.bos, self.eos = bos, eos
        self._trunc: int | None = None

    def _id(self, tok: str) -> int:
        if tok not in self.vocab:
            self.vocab[tok] = len(self.vocab)
        return self.vocab[tok]

    def add_special(self, tok: str) -> int:
        return self._id(tok)

    def token_to_id(self, tok: str) -> int | None:
        return self.vocab.get(tok)

    def enable_truncation(self, max_length: int) -> None:
        self._trunc = int(max_length)

    def no_truncation(self) -> None:
        self._trunc = None

    def _split(self, text: str) -> list[str]:
        out, i = [], 0
        specials = sorted((t for t in self.vocab if t.startswith(("[", "<"))), key=len, reverse=True)
        while i < len(text):
            m = next((s for s in specials if text.startswith(s, i)), None)
            if m:
                out.append(m)
                i += len(m)
                continue
            w = _WORD.match(text, i)
            if w:
                out.append(w.group(0))
                i = w.end()
            else:
                i += 1
        return out

    def encode(self, text: str, add_special_tokens: bool = True) -> _Enc:
        toks = [self._id(t) for t in self._split(text)]
        pre = [self.vocab[self.bos]] if (add_special_tokens and self.bos) else []
        post = [self.vocab[self.eos]] if (add_special_tokens and self.eos) else []
        if self._trunc is not None:
            room = max(0, self._trunc - len(pre) - len(post))
            toks = toks[:room]
        return _Enc(pre + toks + post)


def _vec(seed: str, dim: int) -> np.ndarray:
    h = hashlib.sha256(seed.encode()).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "little"))
    return rng.standard_normal(dim).astype(np.float32)


class FakeBackend:
    """llama-server client double: pooled → one vector per sequence (a function of all ids), token mode → one vector
    per id (a function of the id and its position). ``delay_s`` simulates GPU time (for concurrency tests)."""

    def __init__(self, dim: int, *, pooled: bool = True, delay_s: float = 0.0, name: str = "fake") -> None:
        self.dim, self.pooled, self.delay_s, self.name = dim, pooled, delay_s, name
        self.calls = 0
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()   # guards the counters of the double only

    def health(self) -> dict[str, Any]:
        return {"http_status": 200, "status": "ok"}

    def embed_ids(self, sequences: Sequence[Sequence[int]]) -> EmbedResult:
        with self._lock:
            self.calls += 1
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            out = []
            for seq in sequences:
                if self.pooled:
                    out.append(_vec(f"{self.name}|{list(seq)}", self.dim))
                else:
                    out.append(np.stack([_vec(f"{self.name}|{i}|{t}", self.dim) for i, t in enumerate(seq)]))
            return EmbedResult(out, self.delay_s, sum(len(s) for s in sequences))
        finally:
            with self._lock:
                self.inflight -= 1


class FakeSearchBackend:
    """OpenSearch double: BM25 = overlap of query words with doc text; k-NN = dot product with stored vectors."""

    def __init__(self, docs: dict[str, str], vectors: dict[str, np.ndarray]) -> None:
        self.docs, self.vectors = docs, vectors
        self.requests: list[tuple[str, dict]] = []

    def post(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((index, body))
        size = body.get("size", 10)
        q = body["query"]
        if "knn" in q:
            (field_name, clause), = q["knn"].items()
            v = np.asarray(clause["vector"], dtype=np.float32)
            scored = sorted(((float(v @ vec), oid) for oid, vec in self.vectors.items()), key=lambda t: (-t[0], t[1]))
        else:
            mm = q["multi_match"] if "multi_match" in q else q["bool"]["must"][0]["multi_match"]
            words = set(mm["query"].lower().split())
            scored = sorted(((float(len(words & set(t.lower().split()))), oid) for oid, t in self.docs.items()),
                            key=lambda t: (-t[0], t[1]))
            scored = [s for s in scored if s[0] > 0]
        hits = [{"_id": oid, "_score": s, "_source": {"id": oid, "object_type": "BLOCK"}} for s, oid in scored[:size]]
        return {"hits": {"hits": hits}}
