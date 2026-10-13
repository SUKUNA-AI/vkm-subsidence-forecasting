"""Brief view of the search answers (``view = "brief"``, schema ``vkm.brief/1``) for agents whose context is the budget.

The full view (``view = "full"``, the API default) is the ``ApiResponse``: every hit carries a ``vkm.envelope/1`` and,
in hybrid search, the whole stage trace — about 4 KB a hit. The brief view is a projection of that same answer: it is
built from the serialized ``ApiResponse`` (so the two views never disagree) and keeps what an agent needs to choose
what to open with the get_* tools:

* per hit: ``id`` (its source and page are prefixes of it, ``VKM-SRC-NNN:pNNNN[:object]``; ``source_id`` /
  ``page_id`` are written only when they are not), ``rank``, ``object_type``, ``page_label``, the key ranks and scores
  (BM25 / dense / RRF / late / visual / bibliographic), one index snippet (≤ :data:`SNIPPET_CHARS`, highlight tags
  removed), the printed label and caption of a figure / table / formula, duplicates and copies (ids only), the API's
  interpretation flags;
* per source (``sources``): a short name (stem of the register's ``canonical_path``, ≤ :data:`SOURCE_NAME_CHARS`),
  its work, the area of the source (``scope``: inherited by its objects, never established for them) and the number of
  registered copies of the work when several;
* once: ``defaults`` (the ``review_status`` / ``origin`` of most hits; a hit with another one says its own), the
  warning codes with counts, the hybrid run (route, translation, stage statuses, late on/off, totals, total time)
  and ``next_cursor``.

Envelopes, provenance, geometry, projections, quality flags, the full trace and ``rerank_candidate`` stay in the full
view and in the get_* answers. Nothing is invented: a value absent from the full answer is absent here. Errors are the
same ``ApiResponse`` in both views.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable

BRIEF_SCHEMA = "vkm.brief/1"
SNIPPET_CHARS = 140
TITLE_CHARS = 120
SOURCE_NAME_CHARS = 60
NOTE = ("AUTO_EXTRACTED_UNREVIEWED candidates of rebuildable projections: scores are retrieval signals, not evidence; "
        "snippets are index text, not the object's; scope is the source's, inherited by its objects. Read with get_*; "
        "view=full adds envelopes, provenance and traces.")
_DEFAULTED = ("review_status", "origin")
_SAID_ONCE_FLAGS = frozenset({"SCOPE_INHERITED_FROM_SOURCE"})        # every document object has it: the note says it
_HYBRID_RANKS = ("bm25_rank", "dense_rank", "fused_rank")
_ROUTE_RANKS = ("vis_rank", "bib_rank", "author_rank")
_LEAD_CODE = re.compile(r"^(?:f\d+:\s*)?([A-Z][A-Z0-9_]{2,})")
_TAG = re.compile(r"</?em>")
_STATUS_HEAD = re.compile(r"^[A-Z][A-Z_]*")

SourceNames = Callable[[list[str]], dict[str, str]]


def brief_response(body: dict[str, Any], *, kind: str, source_names: SourceNames | None = None) -> dict[str, Any]:
    """The brief view of a serialized ``ApiResponse`` of ``/v1/search`` (``kind = "search"``), ``/v1/search/hybrid``
    (``"hybrid"``) or ``/v1/objects/query`` (``"objects"``). ``source_names`` maps source ids to short names (the
    API looks them up in the register); without it the source cards have no name. An error body is returned as is."""
    if kind not in ("search", "hybrid", "objects"):
        raise ValueError(f"no brief view for {kind!r}")
    if not body.get("ok"):
        return body
    items = [it for it in body.get("items") or [] if isinstance(it, dict)]
    defaults = _defaults(items)
    seen: dict[str, dict[str, Any]] = {}
    entries = [_object(it, defaults, seen) if kind == "objects" else _hit(it, kind, defaults, seen) for it in items]
    out: dict[str, Any] = {"ok": True, "view": "brief", "schema": BRIEF_SCHEMA,
                           "meta": brief_meta(body.get("meta") or {}), "note": NOTE, "defaults": defaults}
    if kind == "hybrid":
        out["run"] = _run(((body.get("item") or {}).get("record")) or {})
    elif kind == "search":                    # BM25: a run only with a long-question mode (long_query not strict)
        lq = _long_query((((body.get("item") or {}).get("record") or {}).get("stages") or {}).get("long_query"))
        if lq is not None:
            rec = (body.get("item") or {}).get("record") or {}
            out["run"] = {"stages": {"long_query": lq}, "totals": rec.get("totals")}
    out["sources"] = _sources(seen, source_names)
    out["items"] = entries
    out["next_cursor"] = body.get("next_cursor")
    return out


def source_names(canon: Any, source_ids: Iterable[str]) -> dict[str, str]:
    """Short names of sources: the stem of the register's ``canonical_path`` (the file name without its folder and
    extension), else of ``original_filename``, cut to :data:`SOURCE_NAME_CHARS`. A source the snapshot does not know
    has no name; a failing lookup (no snapshot) gives no names — a name is a convenience, never a reason to fail."""
    from vkm_corpus.api.errors import ApiFailure

    ids = sorted({s for s in source_ids if s})
    if not ids:
        return {}
    try:
        rows = canon.rows("SOURCE", ids)
    except ApiFailure:
        return {}
    names: dict[str, str] = {}
    for sid, row in rows.items():
        raw = row.get("canonical_path") or row.get("original_filename")
        stem = PurePosixPath(str(raw).replace("\\", "/")).stem if raw else ""
        if stem:
            names[sid] = cut(stem, SOURCE_NAME_CHARS)
    return names


# ---------------------------------------------------------------------------------------------------- pieces
def cut(text: str, limit: int) -> str:
    """``text`` on one line, at most ``limit`` characters (an ellipsis marks the cut)."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def snippet(highlight: str, limit: int = SNIPPET_CHARS) -> str:
    """One index highlight without its ``<em>`` tags, at most ``limit`` characters: a window that keeps the first match
    near its start, cut at word boundaries where one is near (ellipses mark the cuts)."""
    text = " ".join(str(highlight).split())
    first = text.find("<em>")
    at = len(_TAG.sub("", text[:first])) if first >= 0 else 0
    plain = _TAG.sub("", text)
    if len(plain) <= limit:
        return plain
    start = max(0, min(at - limit // 5, len(plain) - limit))
    end = start + limit
    if start > 0:
        space = plain.find(" ", start, max(start, at))          # begin at a word, never after the match
        start = space + 1 if 0 <= space - start < 20 else start
    if end < len(plain):
        space = plain.rfind(" ", start, end)
        end = space if 0 <= end - space < 20 else end
    head, tail = ("…" if start > 0 else ""), ("…" if end < len(plain) else "")
    return head + plain[start:end].strip()[:limit - len(head) - len(tail)].rstrip() + tail


def _defaults(items: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``review_status`` / ``origin`` most hits have (the first seen wins a tie): said once; a hit with another
    value (or none) writes its own."""
    out: dict[str, Any] = {}
    for key in _DEFAULTED:
        counts: dict[Any, int] = {}
        for it in items:
            value = (it.get("envelope") or {}).get(key)
            if value is not None:
                counts[value] = counts.get(value, 0) + 1
        if counts:
            out[key] = max(counts, key=lambda v: counts[v])
    return out


def _place(out: dict[str, Any], env: dict[str, Any], oid: str) -> None:
    for key in ("source_id", "page_id"):
        value = env.get(key)
        if value and oid != value and not oid.startswith(value + ":"):
            out[key] = value
    if env.get("page_label"):
        out["page_label"] = env["page_label"]


def _scientific(out: dict[str, Any], env: dict[str, Any], defaults: dict[str, Any]) -> None:
    flags = [f for f in env.get("flags") or [] if f not in _SAID_ONCE_FLAGS]
    if flags:
        out["flags"] = flags
    for key in _DEFAULTED:
        if env.get(key) != defaults.get(key):
            out[key] = env.get(key)


def _note_source(seen: dict[str, dict[str, Any]], out: dict[str, Any], env: dict[str, Any],
                 rec: dict[str, Any]) -> None:
    """The source card of a hit (first hit of the source wins); a hit whose work differs says its own."""
    sid = env.get("source_id")
    if not sid:
        return
    work_id = env.get("work_id") or rec.get("work_id")
    card = seen.get(sid)
    if card is None:
        scope = env.get("source_scope") or {}
        card = seen[sid] = {"work_id": work_id, "scope": list(scope.get("values") or []) or None,
                            "scope_raw": scope.get("raw") if scope.get("raw") and (
                                not scope.get("values") or scope.get("mapping") not in ("EXACT", "CASE")) else None,
                            "work_copies": rec.get("work_copy_count") if (rec.get("work_copy_count") or 0) > 1
                            else None}
    elif work_id and work_id != card["work_id"]:
        out["work_id"] = work_id


def _sources(seen: dict[str, dict[str, Any]], names: SourceNames | None) -> dict[str, dict[str, Any]]:
    found = names(sorted(seen)) if names and seen else {}
    return {sid: {k: v for k, v in {"name": found.get(sid), **card}.items() if v is not None}
            for sid, card in seen.items()}


def _hit(item: dict[str, Any], kind: str, defaults: dict[str, Any], seen: dict[str, dict[str, Any]]
         ) -> dict[str, Any]:
    env, rec = item.get("envelope") or {}, item.get("record") or {}
    oid = str(env.get("object_id"))
    out: dict[str, Any] = {"id": oid}
    if rec.get("rank") is not None:
        out["rank"] = rec["rank"]
    out["object_type"] = rec.get("object_type") or env.get("object_kind")
    _place(out, env, oid)
    trace = rec.get("trace") if isinstance(rec.get("trace"), dict) else {}
    if kind == "hybrid":
        out.update({k: trace[k] for k in _HYBRID_RANKS if trace.get(k) is not None})
        if trace.get("late_score") is not None:
            out["late_score"] = round(float(trace["late_score"]), 4)
        if trace.get("late_fusion_rank") is not None:                  # experiment flags of the late stage
            out["late_fusion_rank"] = trace["late_fusion_rank"]
        if isinstance(trace.get("late_query"), dict):
            out["late_query"] = trace["late_query"].get("best")
        out.update({k: trace[k] for k in _ROUTE_RANKS if trace.get(k) is not None})
    elif rec.get("bm25_score") is not None:
        out["bm25_score"] = round(float(rec["bm25_score"]), 3)
    text = next((h for h in rec.get("highlights") or [] if h), None)
    if text:
        out["snippet"] = snippet(text)
    if rec.get("object_label"):
        out["object_label"] = rec["object_label"]
    if out["object_type"] in ("FIGURE", "TABLE") and rec.get("title_or_caption"):
        out["title_or_caption"] = cut(_caption(rec["title_or_caption"], rec.get("object_label_raw")), TITLE_CHARS)
    if rec.get("duplicates"):
        out["duplicates"] = [str(d) for d in rec["duplicates"]]
    copies = rec.get("copies")
    if isinstance(copies, list) and copies:
        out["copies"] = [c if isinstance(c, str) else
                         str((c or {}).get("page_id") or (c or {}).get("object_id") or (c or {}).get("id"))
                         for c in copies]
    elif isinstance(trace.get("graph"), dict) and trace["graph"].get("copies"):
        out["copies_count"] = trace["graph"]["copies"]
    if rec.get("fragments"):                                  # other regions of the figure (one page, one caption)
        out["fragments"] = [str(f) for f in rec["fragments"]]
    _scientific(out, env, defaults)
    _note_source(seen, out, env, rec)
    return out


def _caption(title: str, label: str | None) -> str:
    """A search title without its label written twice («Рис. 1 Рис. 1. Схема» → «Рис. 1. Схема»)."""
    title = " ".join(str(title).split())
    if label and title.startswith(f"{label} {label}"):
        return title[len(label) + 1:]
    return title


def _object(item: dict[str, Any], defaults: dict[str, Any], seen: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """An object of ``/v1/objects/query``: id, kind, page label, printed label, caption (or the LaTeX of a formula,
    the parsed title of a reference), detected figure type and whether an image is stored."""
    env, rec = item.get("envelope") or {}, item.get("record") or {}
    oid = str(env.get("object_id"))
    kind = env.get("object_kind")
    out: dict[str, Any] = {"id": oid, "object_type": kind}
    _place(out, env, oid)
    if rec.get("object_label"):
        out["object_label"] = rec["object_label"]
    if kind == "FORMULA":
        latex = rec.get("normalized_latex") or rec.get("native_glyph_text")
        if latex:
            out["latex"] = cut(latex, TITLE_CHARS)
    else:
        title = rec.get("caption_normalized") or rec.get("caption") or rec.get("parsed_title")
        if title:
            out["title_or_caption"] = cut(title, TITLE_CHARS)
    if kind == "FIGURE" and rec.get("figure_type"):
        out["figure_type"] = rec["figure_type"]
    if kind in ("FIGURE", "TABLE", "FORMULA"):
        out["has_image"] = bool(rec.get("image_artifact_id"))
    _scientific(out, env, defaults)
    _note_source(seen, out, env, rec)
    return out


def brief_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """``meta`` in brief: request id, snapshot and the warning codes with counts (a search warning by the code its
    message leads with; the messages stay in the full view)."""
    codes: dict[str, int] = {}
    for w in meta.get("warnings") or []:
        code = str(w.get("code"))
        if code == "SEARCH_WARNING":
            m = _LEAD_CODE.match(str(w.get("message") or ""))
            code = f"SEARCH_WARNING:{m.group(1)}" if m else code
        codes[code] = codes.get(code, 0) + (w.get("count") if isinstance(w.get("count"), int) else 1)
    return {"request_id": meta.get("request_id"), "snapshot": meta.get("canonical_snapshot_id"),
            "warnings": [{"code": c, "count": n} for c, n in codes.items()]}


def _status(stage: Any) -> Any:
    """A stage's status: the ``status`` of a stage record, the leading word of a text such as ``NOT_RUN (…)``."""
    if isinstance(stage, dict):
        return stage.get("status")
    if isinstance(stage, str):
        m = _STATUS_HEAD.match(stage)
        return m.group(0) if m else cut(stage, 40)
    return stage


def _long_query(stage: Any) -> dict[str, Any] | None:
    """The long-question plan in a few fields (``vkm_corpus.search.long_query``); ``None`` without one."""
    if not isinstance(stage, dict):
        return None
    out = {k: stage.get(k) for k in ("mode", "status", "terms", "msm", "strip") if stage.get(k) is not None}
    if stage.get("stripped_text"):
        out["stripped_text"] = cut(stage["stripped_text"], TITLE_CHARS)
    return out


def _run(rec: dict[str, Any]) -> dict[str, Any]:
    """The hybrid run in a few fields: route, late on/off, translation, stage statuses, totals, total time."""
    stages = rec.get("stages") if isinstance(rec.get("stages"), dict) else {}
    out: dict[str, Any] = {"route": rec.get("route"), "late": rec.get("late")}
    tr = rec.get("translation")
    if isinstance(tr, dict):
        out["translation"] = {"status": tr.get("status"), **({"text": cut(tr["text"], 200)} if tr.get("text") else {})}
    st: dict[str, Any] = {name: _status(stages.get(name)) for name in ("bib_route", "visual_route")
                          if stages.get(name) is not None}
    if _status(stages.get("author_route")) not in (None, "NOT_DETECTED"):     # registry author route (agent Г)
        st["author_route"] = _status(stages["author_route"])
    graph = stages.get("graph")
    if isinstance(graph, dict):
        status = {k: v for k, v in (graph.get("status") or {}).items() if v != "OFF"}
        st["graph"] = status or "OFF"
    elif graph is not None:
        st["graph"] = _status(graph)
    form = stages.get("formulations")
    if isinstance(form, dict):
        st["formulations"] = {"status": form.get("status"), "answered": form.get("answered")}
    cap = stages.get("source_cap")
    if isinstance(cap, dict):
        st["source_cap"] = {"max_per_source": cap.get("max_per_source"), "moved": cap.get("moved")}
    frag = stages.get("fragments")
    if isinstance(frag, dict):
        st["fragments"] = {"status": frag.get("status"), "groups": frag.get("groups")}
    late = stages.get("late")
    if isinstance(late, dict) and (late.get("fusion") or late.get("late_query")):     # experiment flags only
        st["late"] = {k: v for k, v in (("fusion", (late.get("fusion") or {}).get("mode")),
                                        ("late_query", _status(late.get("late_query")))) if v}
    lq = _long_query(stages.get("long_query"))
    if lq is not None:                                 # long-question mode (agent M): only when asked
        st["long_query"] = lq
    out["stages"] = st
    plan = rec.get("formulations")
    if isinstance(plan, dict):
        out["formulations"] = [cut(f.get("text") or "", TITLE_CHARS) for f in plan.get("sent") or []]
        if plan.get("skipped"):
            out["formulations_skipped"] = len(plan["skipped"])
    out.update({"fused_total": rec.get("fused_total"), "candidates": rec.get("candidates"),
                "totals": rec.get("totals"), "ms": (rec.get("timings_ms") or {}).get("total")})
    return out
