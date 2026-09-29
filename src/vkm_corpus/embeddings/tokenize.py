"""Token ids for queries and documents, exactly as the reference libraries build them (§38: the tokenizer is part of
the query signature).

The service never lets the backend tokenize: it builds ids here with the pinned ``tokenizer.json`` (HF ``tokenizers``,
Rust, no torch) and sends them to llama.cpp. The reference harness checks that these ids equal the ids of the official
path (sentence-transformers / PyLate / Stanford ColBERT) for every probe text.

Rules:

* dense (and BGE-M3): ``prefix + text`` encoded with the tokenizer post-processor (model specials), truncated to
  ``max_len`` including specials (``tokenizers`` truncation keeps post-processor tokens);
* visual text towers (``spec.query_template``, Qwen3-VL-Embedding): queries are the chat template rendered with the
  instruction (the query prefix) and the text, encoded with the post-processor (it appends the pooled end token) and
  truncated the same way — the string sentence-transformers renders, so the ids are the official ones;
* ColBERT ``pylate``: encode to ``maxlen - 1`` (specials included); queries with expansion are padded with the pad token
  to ``maxlen - 1``; then the marker id is inserted at position 1, after the first token whatever it is
  (``ColBERT.insert_prefix_token``); expansion positions are attended;
* ColBERT ``stanford`` (jina-colbert-v2, trained with the Stanford code): encode ``". " + text`` to ``maxlen`` and
  overwrite position 1 (the placeholder) with the marker; queries are padded with the mask token to ``query_maxlen``
  and all positions are attended (``attend_to_mask_tokens``).

Document outputs of ColBERT models drop punctuation positions (``mask_punctuation`` / PyLate skiplist); BGE-M3 drops
the CLS position of its ColBERT vectors. ``Encoded.keep`` carries that mask.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from vkm_corpus.embeddings.specs import EncoderSpec, Role

# ColBERT mask_punctuation (Stanford) / PyLate skiplist: ASCII punctuation symbols
PUNCTUATION = tuple("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


@dataclass(frozen=True)
class Encoded:
    ids: tuple[int, ...]
    keep: tuple[bool, ...] | None = None   # positions whose output vectors are kept (None = all)

    def __len__(self) -> int:
        return len(self.ids)

    def kept_positions(self) -> list[int]:
        return list(range(len(self.ids))) if self.keep is None else [i for i, k in enumerate(self.keep) if k]


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _token_str(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("content")
    return value if isinstance(value, str) else None


class SpecTokenizer:
    """Tokenizer of one encoder spec. ``tokenizer`` is a ``tokenizers.Tokenizer`` or an object with the same
    ``encode``/``token_to_id``/``no_truncation``/``enable_truncation`` API (tests use a fake)."""

    def __init__(self, spec: EncoderSpec, tokenizer: Any, *, tokenizer_sha256: str | None = None,
                 pad_id: int | None = None) -> None:
        self.spec = spec
        self.tok = tokenizer
        self.tokenizer_sha256 = tokenizer_sha256
        self.pad_id = pad_id
        self.marker_ids: dict[str, int | None] = {"query": None, "document": None}
        self.expansion_id: int | None = None
        self.skip_ids: frozenset[int] = frozenset()
        self.drop_ids: frozenset[int] = frozenset()
        cb = spec.colbert
        if cb is None:
            return
        for role, marker in (("query", cb.query_marker), ("document", cb.doc_marker)):
            if marker:
                mid = tokenizer.token_to_id(marker)
                if mid is None:
                    raise ValueError(f"{spec.key}: marker {marker!r} is not a token of the tokenizer")
                self.marker_ids[role] = mid
        if cb.query_maxlen and cb.attend_to_expansion:
            if cb.expansion_token:
                self.expansion_id = tokenizer.token_to_id(cb.expansion_token)
            else:
                self.expansion_id = pad_id
            if self.expansion_id is None:
                raise ValueError(f"{spec.key}: query expansion needs the expansion/pad token id")
        if cb.skip_punctuation_in_docs:
            skip: set[int] = set()
            for sym in PUNCTUATION:
                tid = tokenizer.token_to_id(sym)      # PyLate: convert_tokens_to_ids(symbol)
                if tid is not None:
                    skip.add(tid)
                if cb.style == "stanford":            # Stanford: also tokenizer.encode(symbol)[0]
                    enc = self._encode_plain(sym)
                    if enc:
                        skip.add(enc[0])
            self.skip_ids = frozenset(skip)
        drop = {tokenizer.token_to_id(t) for t in cb.drop_special_tokens}
        self.drop_ids = frozenset(i for i in drop if i is not None)

    # -------------------------------------------------------------------------------------------------- construction
    @classmethod
    def from_dir(cls, spec: EncoderSpec, model_dir: str | Path, *,
                 expected_sha256: str | None = None) -> "SpecTokenizer":
        """``tokenizer.json`` (+ the pad token from ``tokenizer_config.json`` when present) of a model snapshot."""
        from tokenizers import Tokenizer  # lazy: optional dependency

        model_dir = Path(model_dir)
        path = model_dir / spec.tokenizer_file
        sha = file_sha256(path)
        if expected_sha256 and sha != expected_sha256:
            raise ValueError(f"{spec.key}: tokenizer.json sha256 mismatch ({sha} != {expected_sha256})")
        tok = Tokenizer.from_file(str(path))
        pad_id = None
        cfg = model_dir / "tokenizer_config.json"
        if cfg.is_file():
            pad = _token_str(json.loads(cfg.read_text(encoding="utf-8")).get("pad_token"))
            pad_id = tok.token_to_id(pad) if pad else None
        return cls(spec, tok, tokenizer_sha256=sha, pad_id=pad_id)

    # ---------------------------------------------------------------------------------------------------- encoding
    def _encode_plain(self, text: str) -> list[int]:
        self.tok.no_truncation()
        return list(self.tok.encode(text, add_special_tokens=False).ids)

    def _encode(self, text: str, max_len: int) -> list[int]:
        self.tok.enable_truncation(max_len)
        try:
            return list(self.tok.encode(text, add_special_tokens=True).ids)
        finally:
            self.tok.no_truncation()

    def encode(self, text: str, role: Role, *, max_len: int | None = None, prefix: str | None = None) -> Encoded:
        """``prefix`` overrides the spec instruction of dense models (a MODEL_CHOICE recorded in the signature)."""
        spec, cb = self.spec, self.spec.colbert
        if spec.query_template and role == "query":
            instruction = spec.query_prefix if prefix is None else prefix
            rendered = spec.query_template.replace("{instruction}", instruction).replace("{text}", text)
            return Encoded(tuple(self._encode(rendered, min(max_len or spec.max_len, spec.max_len))))
        if cb is None or spec.family == "multi":
            pre = spec.prefix(role) if prefix is None else prefix
            ids = self._encode(pre + text, min(max_len or spec.max_len, spec.max_len))
            keep = tuple(i not in self.drop_ids for i in ids) if self.drop_ids else None
            return Encoded(tuple(ids), keep)
        is_query = role == "query"
        limit = cb.query_maxlen if (is_query and cb.query_maxlen) else (max_len or cb.doc_maxlen)
        limit = min(limit, spec.max_len)
        marker = self.marker_ids[role]
        expand = is_query and bool(cb.query_maxlen) and cb.attend_to_expansion
        if cb.style == "stanford":
            ids = self._encode(". " + text, limit)
            if marker is not None:
                ids[1] = marker
            if expand and len(ids) < limit:
                ids += [self.expansion_id] * (limit - len(ids))
        else:
            inner = limit - 1 if marker is not None else limit
            ids = self._encode(text, inner)
            if expand and len(ids) < inner:
                ids += [self.expansion_id] * (inner - len(ids))
            if marker is not None:
                ids = ids[:1] + [marker] + ids[1:]
        if is_query:
            keep = tuple(i not in self.drop_ids for i in ids) if self.drop_ids else None
        else:
            keep = tuple(not (i in self.skip_ids or i in self.drop_ids) for i in ids)
        return Encoded(tuple(ids), keep)

    def encode_batch(self, texts: Sequence[str], role: Role, *, max_len: int | None = None) -> list[Encoded]:
        return [self.encode(t, role, max_len=max_len) for t in texts]

    def encode_context(self, chunks: Sequence[str], *, sep_token: str, max_len: int | None = None
                       ) -> tuple[Encoded, list[tuple[int, int]]]:
        """Contextual (late-chunking) document: chunks joined by ``sep_token`` into one sequence; returns the ids and
        the half-open token span of every chunk (pplx-embed-context)."""
        sep_id = self.tok.token_to_id(sep_token)
        if sep_id is None:
            raise ValueError(f"{self.spec.key}: separator {sep_token!r} unknown")
        ids = self._encode(sep_token.join(chunks), min(max_len or self.spec.max_len, self.spec.max_len))
        spans: list[tuple[int, int]] = []
        start = 0
        for pos, tid in enumerate(ids):
            if tid == sep_id:
                spans.append((start, pos))
                start = pos + 1
        spans.append((start, len(ids)))
        return Encoded(tuple(ids)), spans
