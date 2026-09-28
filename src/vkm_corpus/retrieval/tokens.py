"""Token counting of the gateway with the tokenizers of the served models (CP-18 (а), H-13).

* v3.5 (text): the listwise prompt of ``jinaai/jina-reranker-v3.5`` @ ``e8a93f33…`` is rebuilt exactly as
  ``format_docs_prompts_func`` of its ``modeling.py`` (no instruction, ``no_thinking=True``) and tokenised with the same
  ``tokenizer.json`` (sha256 ``4e95945a…``) that the text service uses. For one block (always the case under the
  4096-token budget) this equals ``exact_total_tokens`` of the service, which re-checks the budget itself
  (``token_budget`` of ``POST /v1/rerank``), so the service stays the authority and the gateway only rejects early.
* m0 (visual): the query is counted with the ``tokenizer.json`` of ``jinaai/jina-reranker-m0``.

The ``tokenizers`` package is imported lazily.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

V35_SPECIAL_TOKENS = {"query_embed_token": "<|rerank_token|>", "doc_embed_token": "<|embed_token|>"}
_V35_PREFIX = (
    "<|im_start|>system\n"
    "You are a search relevance expert who can determine a ranking of the passages based on how relevant they are to "
    "the query. If the query is a question, how relevant a passage is depends on how well it answers the question. "
    "If not, try to analyze the intent of the query and assess how well each passage satisfies the intent. If an "
    "instruction is provided, you should follow the instruction when determining the ranking."
    "<|im_end|>\n<|im_start|>user\n"
)
_V35_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

# special-token literals of the Qwen family and the mtmd media marker (stripped from visual queries)
SPECIAL_TOKEN_RE = re.compile(r"<\|[A-Za-z0-9_]+\|>|<__media__>")


def _sanitize(text: str) -> str:
    for token in V35_SPECIAL_TOKENS.values():
        text = text.replace(token, "")
    return text


def v35_listwise_prompt(query: str, docs: list[str]) -> str:
    """Exact replica of ``format_docs_prompts_func(query, docs, instruction=None, no_thinking=True)`` @ e8a93f33."""
    query = _sanitize(query)
    docs = [_sanitize(d) for d in docs]
    doc_tok, query_tok = V35_SPECIAL_TOKENS["doc_embed_token"], V35_SPECIAL_TOKENS["query_embed_token"]
    prompt = (
        f"I will provide you with {len(docs)} passages, each indicated by a numerical identifier. "
        f"Rank the passages based on their relevance to query: {query}\n"
    )
    prompt += "\n".join(f'<passage id="{i}">\n{doc}{doc_tok}\n</passage>' for i, doc in enumerate(docs)) + "\n"
    prompt += f"<query>\n{query}{query_tok}\n</query>"
    return _V35_PREFIX + prompt + _V35_SUFFIX


def strip_special_tokens(text: str) -> tuple[str, bool]:
    cleaned = SPECIAL_TOKEN_RE.sub("", text)
    return cleaned, cleaned != text


@dataclass
class TokenCounter:
    """``tokenizers.Tokenizer`` from a ``tokenizer.json`` with its sha256 (checked against a pin if given)."""

    path: Path
    sha256: str
    _tok: Any

    @classmethod
    def from_file(cls, path: str | Path, expected_sha256: str | None = None) -> "TokenCounter":
        from tokenizers import Tokenizer

        p = Path(path)
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if expected_sha256 and digest != expected_sha256:
            raise ValueError(f"tokenizer.json sha256 mismatch: {digest} != {expected_sha256}")
        return cls(path=p, sha256=digest, _tok=Tokenizer.from_file(str(p)))

    def count(self, text: str) -> int:
        return len(self._tok.encode(text, add_special_tokens=False).ids)

    def truncate(self, text: str, max_tokens: int) -> tuple[str, int, bool]:
        """Cut ``text`` after ``max_tokens`` tokens at a character offset: ``(text[:end], n_tokens, truncated)``."""
        enc = self._tok.encode(text, add_special_tokens=False)
        if len(enc.ids) <= max_tokens:
            return text, len(enc.ids), False
        end = enc.offsets[max_tokens - 1][1]
        return text[:end], max_tokens, True

    def v35_prompt_tokens(self, query: str, docs: list[str]) -> int:
        return self.count(v35_listwise_prompt(query, docs))
