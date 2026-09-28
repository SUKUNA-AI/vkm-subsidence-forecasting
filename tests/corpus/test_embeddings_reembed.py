"""Re-embed policy (§46): same text hash + same signature → no inference; only changed objects are re-embedded."""
from __future__ import annotations

import pytest

from vkm_corpus.embeddings.reembed import CanonObject, batches, plan
from vkm_corpus.embeddings.signature import text_hash

SIG = "f" * 64


def _objs(texts: dict[str, str]) -> list[CanonObject]:
    return [CanonObject(oid, text_hash(t)) for oid, t in texts.items()]


def test_first_run_embeds_everything():
    p = plan(SIG, _objs({"a": "x", "b": "y"}), {})
    assert p.inference_count == 2 and p.new == ["a", "b"] and not p.unchanged


def test_same_hash_and_signature_means_zero_inference():
    canon = _objs({"a": "x", "b": "y"})
    existing = {o.object_id: {o.text_hash} for o in canon}
    p = plan(SIG, canon, existing)
    assert p.inference_count == 0 and p.unchanged == ["a", "b"]


def test_only_changed_and_new_objects_are_embedded():
    existing = {"a": {text_hash("x")}, "b": {text_hash("y")}, "gone": {text_hash("z")}}
    p = plan(SIG, _objs({"a": "x", "b": "y2", "c": "w"}), existing)
    assert [o.object_id for o in p.to_embed] == ["b", "c"]
    assert p.changed == ["b"] and p.new == ["c"] and p.unchanged == ["a"] and p.orphaned == ["gone"]
    assert p.summary()["to_embed"] == 2


def test_history_rows_do_not_block_reverting_text():
    # an object whose text changed back to an earlier version is not re-embedded (that vector already exists)
    existing = {"a": {text_hash("v1"), text_hash("v2")}}
    assert plan(SIG, _objs({"a": "v1"}), existing).inference_count == 0


def test_duplicate_canonical_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        plan(SIG, [CanonObject("a", "1"), CanonObject("a", "2")], {})


def test_batches():
    objs = _objs({str(i): str(i) for i in range(5)})
    assert [len(b) for b in batches(objs, 2)] == [2, 2, 1]
    with pytest.raises(ValueError):
        batches(objs, 0)
