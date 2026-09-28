"""Token counting of the rerank gateway (agent F; CP-18 (а)): exact v3.5 listwise prompt, truncation, sanitising."""
from __future__ import annotations

import hashlib
import json

import pytest

from vkm_corpus.retrieval.tokens import SPECIAL_TOKEN_RE, strip_special_tokens, v35_listwise_prompt

# sha256 of format_docs_prompts_func(...) of jinaai/jina-reranker-v3.5 @ e8a93f33 (modeling.py), computed with the
# original function; the same prompts gave equal token counts with transformers 4.57.3 (the service) and the
# gateway counter (178, 150) — receipt in work/corpus_platform/impl/edge_receipts/.
GOLDEN = {
    ("какой запрос?", ("документ А <|embed_token|> с токеном", "document B")):
        "c52f1e539c8e7ff6acd43f01da66298ee7f9c0afec9357164ab953212ef81b39",
    ("q", ("x",)): "dcbad72ab3c8bb94090707db01f562afc3d6dcf7baa7550f935a96a564507cba",
}


@pytest.mark.parametrize("case", list(GOLDEN))
def test_v35_prompt_is_the_original(case):
    query, docs = case
    assert hashlib.sha256(v35_listwise_prompt(query, list(docs)).encode("utf-8")).hexdigest() == GOLDEN[case]


def test_v35_prompt_sanitises_its_special_tokens():
    prompt = v35_listwise_prompt("q <|rerank_token|>", ["d <|embed_token|>"])
    assert prompt.count("<|embed_token|>") == 1 and prompt.count("<|rerank_token|>") == 1


def test_strip_special_tokens():
    assert strip_special_tokens("разрез <|box_end|> пласта <__media__>") == ("разрез  пласта ", True)
    assert strip_special_tokens("обычный запрос") == ("обычный запрос", False)
    assert SPECIAL_TOKEN_RE.search("<|im_start|>") and not SPECIAL_TOKEN_RE.search("a < b | c > d")


def _tiny_tokenizer(path):
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers import models, pre_tokenizers

    vocab = {"[UNK]": 0, "оседание": 1, "реперов": 2, "по": 3, "линии": 4, "I": 5}
    tok = tokenizers.Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(path))
    return path


def test_token_counter_counts_truncates_and_pins(tmp_path):
    from vkm_corpus.retrieval.tokens import TokenCounter

    path = _tiny_tokenizer(tmp_path / "tokenizer.json")
    counter = TokenCounter.from_file(path)
    assert counter.count("оседание реперов по линии I") == 5
    text, n, cut = counter.truncate("оседание реперов по линии I", 2)
    assert (text, n, cut) == ("оседание реперов", 2, True)
    assert counter.truncate("оседание", 5) == ("оседание", 1, False)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        TokenCounter.from_file(path, expected_sha256="0" * 64)
    assert json.loads(path.read_text(encoding="utf-8"))["model"]["type"] == "WordLevel"
