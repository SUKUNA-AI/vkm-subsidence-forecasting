"""Token-id rules of the service (SpecTokenizer) against the reference-library semantics (synthetic tokenizer)."""
from __future__ import annotations

import dataclasses

import pytest

from vkm_corpus.embeddings.fakes import FakeTokenizer
from vkm_corpus.embeddings.specs import SPECS, ColbertSpec, get
from vkm_corpus.embeddings.tokenize import PUNCTUATION, SpecTokenizer


def _dense_spec(**kw):
    return dataclasses.replace(get("granite-311m-r2"), **kw)


def test_dense_prefix_and_truncation_keep_specials():
    tok = FakeTokenizer(bos="<s>", eos="</s>")
    st = SpecTokenizer(_dense_spec(query_prefix="query: ", doc_prefix="doc: "), tok)
    q = st.encode("salt creep", "query")
    assert q.ids[0] == tok.token_to_id("<s>") and q.ids[-1] == tok.token_to_id("</s>")
    assert tok.token_to_id("query") in q.ids and tok.token_to_id("doc") not in q.ids
    long = st.encode(" ".join(f"w{i}" for i in range(50)), "document", max_len=10)
    assert len(long) == 10 and long.ids[0] == tok.token_to_id("<s>") and long.ids[-1] == tok.token_to_id("</s>")
    assert long.keep is None


def test_prefix_override_is_used_for_dense_queries():
    tok = FakeTokenizer()
    st = SpecTokenizer(_dense_spec(query_prefix="Instruct: web\nQuery:"), tok)
    a = st.encode("x", "query")
    b = st.encode("x", "query", prefix="Instruct: salt mining\nQuery:")
    assert a.ids != b.ids and tok.token_to_id("mining") in b.ids


def _late_spec(style: str, **cb_kw):
    base = get("jina-colbert-v2") if style == "stanford" else get("pplx-embed-late-0.6b")
    cb = dataclasses.replace(base.colbert, **cb_kw)
    return dataclasses.replace(base, colbert=cb)


def test_pylate_marker_inserted_after_first_token_and_query_expansion():
    tok = FakeTokenizer(bos=None, eos=None, specials=("<pad>", "[Q] ", "[D] "))
    spec = _late_spec("pylate", query_maxlen=8, query_marker="[Q] ", doc_marker="[D] ")
    st = SpecTokenizer(spec, tok, pad_id=tok.token_to_id("<pad>"))
    q = st.encode("alpha beta", "query")
    assert len(q) == 8                                    # marker + text + pad expansion
    assert q.ids[1] == tok.token_to_id("[Q] ") and q.ids[0] == tok.token_to_id("alpha")
    assert q.ids[-1] == tok.token_to_id("<pad>")
    d = st.encode("alpha , beta .", "document")
    assert d.ids[1] == tok.token_to_id("[D] ")
    kept = [t for t, k in zip(d.ids, d.keep) if k]
    assert tok.token_to_id(",") not in kept and tok.token_to_id(".") not in kept
    assert tok.token_to_id("beta") in kept


def test_pylate_without_expansion_does_not_pad():
    tok = FakeTokenizer(bos="<bos>", eos="<eos>", specials=("<pad>", "[Q] ", "[D] "))
    spec = _late_spec("pylate", query_maxlen=0, attend_to_expansion=False, skip_punctuation_in_docs=False)
    st = SpecTokenizer(spec, tok, pad_id=tok.token_to_id("<pad>"))
    q = st.encode("one two", "query")
    assert q.ids == (tok.token_to_id("<bos>"), tok.token_to_id("[Q] "), tok.token_to_id("one"),
                     tok.token_to_id("two"), tok.token_to_id("<eos>"))
    d = st.encode("one , two", "document")
    assert all(d.keep)


def test_stanford_marker_overwrites_placeholder_and_expands_with_mask():
    tok = FakeTokenizer(bos="<s>", eos="</s>", specials=("<pad>", "<unk>", "<mask>", "[QueryMarker]",
                                                         "[DocumentMarker]"))
    spec = _late_spec("stanford", query_maxlen=12)
    st = SpecTokenizer(spec, tok)
    q = st.encode("creep of salt", "query")
    assert len(q) == 12
    assert q.ids[0] == tok.token_to_id("<s>") and q.ids[1] == tok.token_to_id("[QueryMarker]")
    assert q.ids[2] == tok.token_to_id("creep")           # the ". " placeholder was overwritten
    assert q.ids[-1] == tok.token_to_id("<mask>")
    d = st.encode("creep , of salt", "document", max_len=6)
    assert len(d) == 6 and d.ids[1] == tok.token_to_id("[DocumentMarker]")
    assert not dict(zip(d.ids, d.keep))[tok.token_to_id(",")]


def test_unknown_marker_is_an_error():
    tok = FakeTokenizer(specials=("<pad>",))
    with pytest.raises(ValueError, match="marker"):
        SpecTokenizer(_late_spec("stanford"), tok)


def test_bge_m3_colbert_keep_drops_cls_only():
    tok = FakeTokenizer(bos="<s>", eos="</s>")
    st = SpecTokenizer(get("bge-m3"), tok)
    e = st.encode("a b", "document")
    assert e.keep[0] is False and all(e.keep[1:])


def test_context_spans_split_on_separator():
    tok = FakeTokenizer(bos=None, eos=None, specials=("<|endoftext|>",))
    st = SpecTokenizer(get("pplx-embed-context-0.6b"), tok)
    enc, spans = st.encode_context(["a b", "c", "d e f"], sep_token="<|endoftext|>")
    assert spans == [(0, 2), (3, 4), (5, 8)] and len(enc) == 8


def test_punctuation_set_is_ascii():
    assert len(PUNCTUATION) == 32 and all(ord(c) < 128 for c in PUNCTUATION)


def test_specs_registry_is_consistent():
    assert len(SPECS) >= 12
    for key, s in SPECS.items():
        assert s.key == key and len(s.model_revision) == 40
        assert s.output_dim in s.matryoshka_dims or s.family == "late"
        if s.family == "late":
            assert s.colbert is not None and s.pooling == "none"
        assert s.license
    assert {"dense", "late", "multi", "visual"} >= {s.family for s in SPECS.values()}
    for s in SPECS.values():
        assert bool(s.query_template) == (s.family == "visual")
    assert isinstance(get("jina-colbert-v2").colbert, ColbertSpec)
