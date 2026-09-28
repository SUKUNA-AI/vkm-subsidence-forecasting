"""Topic-gated object numbers (agent L, smoke Q5): «рис. N.N + topic» ranks figures by the topic and moves the ones
labelled N.N among the topic's top hits to the front; an off-topic «рис. N.N» is never pulled up; the smoke Q5 check
holds on a fake index."""
from __future__ import annotations

from vkm_corpus.search import query as Q
from vkm_corpus.search.smoke import run_smoke


class _Figures:
    """A fake OpenSearch answering figure queries: hits whose caption words overlap the query, by overlap."""

    def __init__(self, figures):
        self.figures = figures              # id → (caption words, object_label)
        self.bodies = []

    def _hits(self, body):
        text = body["query"]["bool"]["must"][0]["multi_match"]["query"].lower().replace(".", " ").split()
        scored = []
        for fid, (words, label) in self.figures.items():
            s = len(set(text) & set(words.split()))
            if s:
                scored.append((-s, fid, label))
        scored.sort()
        size = body["size"]
        return [{"_id": fid, "_index": "vkm-figures-m1-b1", "_score": float(-s),
                 "_source": {"id": fid, "object_type": "FIGURE", "object_label": label, "page_id": fid[:17]}}
                for s, fid, label in scored[body.get("from", 0):body.get("from", 0) + size]]

    def search(self, index, body):
        self.bodies.append(body)
        hits = self._hits(body)
        return {"hits": {"hits": hits, "total": {"value": len(hits)}}, "aggregations": {}}

    def msearch(self, body):
        return {"responses": [self.search(h["index"], b) for h, b in zip(body[0::2], body[1::2])]}


FIGS = {"VKM-SRC-001:p0010:f1": ("мульда сдвижения профиль", "2.4"),
        "VKM-SRC-002:p0020:f2": ("мульда сдвижения оседания", "3.1"),     # on topic, labelled 3.1
        "VKM-SRC-003:p0030:f3": ("мульда", "1.1"),
        "VKM-SRC-004:p0040:f4": ("рис 3 1 схема ствола", "3.1")}             # labelled 3.1, off topic


def test_topic_and_promotion():
    assert Q.topic_of("рис. 3.1 мульда сдвижения") == "мульда сдвижения"
    assert Q.topic_of("мульда сдвижения, рисунок 2.10") == "мульда сдвижения"
    assert Q.topic_of("формула (3.12) ползучести") == "формула ползучести" and Q.topic_of("рис. 3.1") == ""
    fake = _Figures(FIGS)
    resp = Q.search(fake, Q.SearchRequest("рис. 3.1 мульда сдвижения", kinds=("FIGURE",), size=3), "vkm")
    assert [h.id for h in resp.hits] == ["VKM-SRC-002:p0020:f2", "VKM-SRC-001:p0010:f1", "VKM-SRC-003:p0030:f3"]
    assert resp.hits[0].fields["label_promoted"] and resp.hits[0].fields["topic_rank"] == 2
    assert not resp.hits[1].fields["label_promoted"] and [h.rank for h in resp.hits] == [1, 2, 3]
    topic_body = fake.bodies[-1]
    assert topic_body["query"]["bool"]["must"][0]["multi_match"]["query"] == "мульда сдвижения"
    assert "object_label" not in str(topic_body["query"]["bool"]["should"])     # no global label boost
    assert topic_body["size"] == Q.LABEL_TOPIC_K


def test_off_topic_label_is_not_pulled_up_and_bare_number_keeps_the_boost():
    fake = _Figures(FIGS)
    resp = Q.search(fake, Q.SearchRequest("рис. 3.1 мульда сдвижения", kinds=("FIGURE",), size=10), "vkm")
    assert "VKM-SRC-004:p0040:f4" not in [h.id for h in resp.hits]
    bare = Q.search(fake, Q.SearchRequest("рис. 3.1", kinds=("FIGURE",), size=5), "vkm")
    assert {"term": {"object_label": {"value": "3.1", "boost": 5.0}}} in fake.bodies[-1]["query"]["bool"]["should"]
    assert all("label_promoted" not in h.fields for h in bare.hits)
    multi = Q.search(fake, Q.SearchRequest("рис. 3.1 мульда сдвижения", kinds=("FIGURE", "TABLE"), size=3), "vkm")
    assert multi.hits[0].id == "VKM-SRC-002:p0020:f2" and multi.fusion == "RRF"


def test_smoke_q5_holds_and_catches_an_ungated_boost(monkeypatch):
    [ok] = run_smoke(_Figures(FIGS), "vkm", only=("Q5",))
    assert ok.check_id == "Q5" and ok.status == "PASS", ok
    # an ungated boost (the off-topic «рис. 3.1» first) must fail the check
    real = Q.promote_labels

    def ungated(hits, label, field_name, k=Q.LABEL_TOPIC_K):
        out = real(hits, label, field_name, k)
        return list(reversed(out))

    monkeypatch.setattr(Q, "promote_labels", ungated)
    [bad] = run_smoke(_Figures(FIGS), "vkm", only=("Q5",))
    assert bad.status == "FAIL"


def test_search_smoke_cli_runs_only_the_named_checks(monkeypatch, capsys):
    import json

    from vkm_corpus.search import cli

    monkeypatch.setattr(cli, "_client", lambda settings: _Figures(FIGS))
    rc = cli._cmd_smoke(cli.argparse.Namespace(t0="2009-12-31", min_total=1, prefix=None, only="q5"))
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and [r["check_id"] for r in out] == ["Q5"] and out[0]["status"] == "PASS"
