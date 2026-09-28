"""In-memory stand-in for the parts of the opensearch-py client used by ``vectors`` and ``hybrid`` (tests only).

Indices hold ``_meta`` and documents; ``search`` answers k-NN queries by exact inner product over the stored vectors
(with ``terms`` filters) and delegates every other query to ``bm25`` (a callable ``(index, body) → hits``), so the BM25
path of agent E runs unchanged against canned hits.
"""
from __future__ import annotations

import copy
from typing import Any, Callable


class NotFoundError(Exception):
    status_code = 404


class _Indices:
    def __init__(self, fake: "FakeOpenSearch") -> None:
        self.f = fake

    def create(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        if index in self.f.indices_:
            raise ValueError(f"index {index} exists")
        self.f.indices_[index] = {"body": copy.deepcopy(body), "meta": dict(body["mappings"].get("_meta") or {}),
                                  "docs": {}, "settings": copy.deepcopy(body.get("settings"))}
        return {"acknowledged": True}

    def get_alias(self, name: str) -> dict[str, Any]:
        out = {i: {"aliases": {name: {}}} for i, a in self.f.aliases.items() if name in a}
        if not out:
            raise NotFoundError(name)
        return out

    def get_mapping(self, index: str, params: Any = None) -> dict[str, Any]:
        head = index.rstrip("*")
        return {n: {"mappings": {"_meta": dict(i["meta"])}} for n, i in self.f.indices_.items() if n.startswith(head)}

    def refresh(self, index: str) -> None:
        self.f.calls.append(("refresh", index))

    def put_settings(self, index: str, body: dict[str, Any]) -> None:
        self.f.calls.append(("put_settings", index))

    def forcemerge(self, index: str, params: Any = None) -> None:
        self.f.calls.append(("forcemerge", index))

    def put_mapping(self, index: str, body: dict[str, Any]) -> None:
        self.f.indices_[index]["meta"].update(body.get("_meta") or {})

    def update_aliases(self, body: dict[str, Any]) -> None:
        for action in body["actions"]:
            (op, spec), = action.items()
            names = self.f.aliases.setdefault(spec["index"], set())
            (names.add if op == "add" else names.discard)(spec["alias"])
        self.f.calls.append(("update_aliases", body))

    def delete(self, index: str) -> None:
        if "*" in index:
            raise ValueError("wildcard delete refused (destructive_requires_name)")
        if index not in self.f.indices_:
            raise NotFoundError(index)
        del self.f.indices_[index]
        self.f.aliases.pop(index, None)
        self.f.deleted.append(index)


class FakeOpenSearch:
    def __init__(self, bm25: Callable[[str, dict[str, Any]], list[dict[str, Any]]] | None = None) -> None:
        self.indices_: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, set[str]] = {}
        self.calls: list[Any] = []
        self.deleted: list[str] = []
        self.searches: list[tuple[str, dict[str, Any]]] = []
        self.bm25 = bm25
        self.indices = _Indices(self)

    def _resolve(self, name: str) -> list[str]:
        if name in self.indices_:
            return [name]
        targets = [i for i, a in self.aliases.items() if name in a]
        if not targets:
            raise NotFoundError(name)
        return targets

    def bulk(self, body: list[dict[str, Any]], params: Any = None) -> dict[str, Any]:
        items = []
        for action, doc in zip(body[0::2], body[1::2]):
            spec = action["create"]
            docs = self.indices_[spec["_index"]]["docs"]
            if spec["_id"] in docs:
                items.append({"create": {"_id": spec["_id"], "status": 409, "error": {"type": "conflict"}}})
                continue
            docs[spec["_id"]] = copy.deepcopy(doc)
            items.append({"create": {"_id": spec["_id"], "status": 201}})
        return {"errors": any(i["create"]["status"] >= 300 for i in items), "items": items}

    def count(self, index: str) -> dict[str, int]:
        return {"count": sum(len(self.indices_[i]["docs"]) for i in self._resolve(index))}

    def mget(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        docs = self.indices_[self._resolve(index)[0]]["docs"]
        return {"docs": [{"_id": i, "found": i in docs, "_source": docs.get(i)} for i in body["ids"]]}

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        self.searches.append((index, copy.deepcopy(body)))
        knn = (body.get("query") or {}).get("knn")
        if knn is None:
            hits = self.bm25(index, body) if self.bm25 else []
            return {"hits": {"hits": hits, "total": {"value": len(hits)}}, "aggregations": {}}
        (field, spec), = knn.items()
        name = self._resolve(index)[0]
        out = []
        for doc_id, doc in self.indices_[name]["docs"].items():
            if not _matches(doc, (spec.get("filter") or {}).get("bool") or {}):
                continue
            score = sum(a * b for a, b in zip(doc[field], spec["vector"]))
            out.append({"_id": doc_id, "_index": name, "_score": score,
                        "_source": {k: v for k, v in doc.items() if k != field}})
        out.sort(key=lambda h: (-h["_score"], h["_id"]))
        return {"hits": {"hits": out[:spec["k"]], "total": {"value": len(out)}}}


def _term_ok(doc: dict[str, Any], clause: dict[str, Any]) -> bool:
    (kind, spec), = clause.items()
    if kind in ("terms", "term"):
        (fld, want), = spec.items()
        want = want if isinstance(want, list) else [want]
        have = doc.get(fld)
        have = have if isinstance(have, list) else [have]
        return bool(set(map(str, have)) & set(map(str, want)))
    if kind == "range":
        (fld, cond), = spec.items()
        v = doc.get(fld)
        if v is None:
            return False
        return all({"gte": v >= b, "lte": v <= b, "gt": v > b, "lt": v < b}[op] for op, b in cond.items())
    return True


def _matches(doc: dict[str, Any], bool_q: dict[str, Any]) -> bool:
    return all(_term_ok(doc, c) for c in bool_q.get("filter", [])) and \
        not any(_term_ok(doc, c) for c in bool_q.get("must_not", []))
