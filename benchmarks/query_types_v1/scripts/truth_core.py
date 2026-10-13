"""QUERY_TYPES_V1 — automatic truth from the canon, the registry and NAV, BEFORE any system runs (stdlib only; runs
INSIDE the VKM API container on CORE with read-only API calls, like ``benchmarks/topic_v1/scripts/harness_core.py``).

``build_set.py bundle`` prepends the input as ``INPUT_JSON = r'''…'''``; ``build_set.py assemble`` turns the output
into the frozen set. The token is read from ``VKM_API_TOKEN_FILE`` in the container and never printed. Rules
(PREREGISTRATION_EXPERIMENTS_2026-10-13.md §E10):

* **objects** (type C, figures and tables with a printed label): the labelled FIGURE / TABLE objects of the sweep
  sources (``/v1/objects/query``, canon); candidates in sha256 order of ``query_types_v1|c|<object_id>``; query words =
  the two longest content words of the caption (≥ 5 letters, not a function word), in caption order; truth = the pages
  of every object of that kind in the corpus whose label number equals the candidate's and whose normalised caption
  contains both 6-letter stems (``caption_query`` is a canon ILIKE, exhaustive). A candidate is taken when the truth
  has 1–5 pages, its source is not used yet and neither stem matches more than ``MAX_STEM_OBJECTS`` objects.
* **boreholes** (type C): for every page the host sends, does the borehole number occur in the canonical page text
  (``normalized_text``, regex with digit boundaries; a printed thousands space allowed) — the page-basis check.
* **authors** (type D): every source → its work links → the work's authors (registry, ``/v1/work``); authors with ≥
  ``min_works`` works in sha256 order of ``query_types_v1|d|<author_id>``; truth = every ACTIVE source that is an
  instance of a work the author is listed on.
* **parameters** (type E): the NAV ``find_parameters`` candidates (``/v1/nav/parameters``) of a property × material:
  candidate pages and the pages of their copies (other instances of the same work).

Output (stdout): one JSON document with ids, labels, numbers, booleans and the chosen query words — no page text.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("VKM_BENCH_API", "http://127.0.0.1:8000")
PAGE_RE = re.compile(r"VKM-SRC-\d{3}:[a-z]\d{4}")
WORD_RE = re.compile(r"[а-яё]{5,}")
LABEL_RE = re.compile(r"^\s*(рис|табл|fig|table)\w*\.?\s*(\d+(?:[.,]\d+)*)\s*$", re.I)
CAPTION_LABEL_RE = re.compile(r"^\s*(рис|табл|fig|table)\w*\.?\s*\d+(?:[.,]\d+)*\.?\s*", re.I)
FUNCTION_WORDS = frozenset("""которые которых который которая которое между после через перед около более менее
также тогда когда здесь всего может могут будет быть было были этого этому этими своих своей своего такие такой
такая таких такого против внутри вблизи вдоль среди после""".split())
KIND_PREFIX = {"FIGURE": "рис.", "TABLE": "табл."}
MAX_STEM_OBJECTS = 3000
STATS = {"calls": 0, "errors": 0}


class Api:
    def __init__(self, base: str = BASE) -> None:
        self.base = base
        path = os.environ.get("VKM_API_TOKEN_FILE")
        self._auth = {}
        if path:
            with open(path, encoding="utf-8") as f:
                self._auth = {"Authorization": "Bearer " + f.read().strip()}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 120.0) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in (0, 1):
            req = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={**self._auth, "Content-Type": "application/json"})
            STATS["calls"] += 1
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt:
                    STATS["errors"] += 1
                    return e.code, {}
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt:
                    STATS["errors"] += 1
                    return 0, {}
            time.sleep(2.0)
        return 0, {}


def sha(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def label_number(label: str | None) -> tuple[str, str] | None:
    """('рис' | 'табл', '3.1') of a printed object label; None when it is not 'kind number'."""
    m = LABEL_RE.match(label or "")
    if not m:
        return None
    kind = m.group(1).lower()
    kind = {"fig": "рис", "table": "табл"}.get(kind, kind)
    return kind, m.group(2).replace(",", ".")


def content_words(caption: str | None) -> list[str]:
    """The two longest content words of a caption (label prefix removed), in caption order."""
    text = CAPTION_LABEL_RE.sub("", (caption or "")).lower()
    seen: list[str] = []
    for w in WORD_RE.findall(text):
        if w not in FUNCTION_WORDS and w not in seen:
            seen.append(w)
    if len(seen) < 2:
        return []
    top = sorted(seen, key=lambda w: (-len(w), seen.index(w)))[:2]
    return sorted(top, key=seen.index)


def stem(word: str) -> str:
    return word[:6]


def objects_of(api: Api, body: dict, cap: int | None = None) -> list[dict] | None:
    """Every object of an objects query (brief view, paged); None when more than ``cap`` objects match."""
    out, cursor = [], None
    while True:
        b = {**body, "limit": 50, "view": "brief"}
        if cursor:
            b["cursor"] = cursor
        code, r = api.call("POST", "/v1/objects/query", b)
        if code != 200:
            raise SystemExit(f"objects query failed: HTTP {code}")
        out += [{"id": it.get("id"), "label": it.get("object_label"), "caption": it.get("title_or_caption")}
                for it in r.get("items") or []]
        if cap is not None and len(out) > cap:
            return None
        cursor = r.get("next_cursor")
        if not cursor:
            return out


def page_of(object_id: str) -> str | None:
    m = PAGE_RE.match(object_id or "")
    return m.group(0) if m else None


def pick_objects(api: Api, sources: list[str], want: dict[str, int]) -> dict:
    listed = {k: [] for k in want}
    for kind in want:
        for s in sources:
            listed[kind] += [o for o in objects_of(api, {"kinds": [kind], "source_ids": [s]}) if o["label"]]
    picked, skipped = [], {"no_label_number": 0, "few_words": 0, "stem_too_common": 0, "truth_size": 0,
                           "source_used": 0}
    for kind, n in want.items():
        cands = sorted(listed[kind], key=lambda o: sha("query_types_v1|c|" + o["id"]))
        used: set[str] = set()
        got = 0
        for o in cands:
            if got >= n:
                break
            ln = label_number(o["label"])
            if not ln:
                skipped["no_label_number"] += 1
                continue
            words = content_words(o["caption"])
            if not words:
                skipped["few_words"] += 1
                continue
            src = o["id"].split(":")[0]
            if src in used:
                skipped["source_used"] += 1
                continue
            sets = []
            for w in words:
                objs = objects_of(api, {"kinds": [kind], "caption_query": stem(w)}, cap=MAX_STEM_OBJECTS)
                if objs is None:
                    break
                sets.append({x["id"]: x for x in objs})
            if len(sets) < 2:
                skipped["stem_too_common"] += 1
                continue
            both = [sets[0][i] for i in sets[0] if i in sets[1]]
            truth = sorted({x["id"] for x in both if label_number(x["label"]) == ln})
            pages = sorted({page_of(i) for i in truth if page_of(i)})
            if not 1 <= len(pages) <= 5 or o["id"] not in truth:
                skipped["truth_size"] += 1
                continue
            used.add(src)
            got += 1
            picked.append({"kind": kind, "object_id": o["id"], "label": o["label"], "number": ln[1], "words": words,
                           "stems": [stem(w) for w in words], "truth_objects": truth, "truth_pages": pages,
                           "query": f"{KIND_PREFIX[kind]} {ln[1]} {words[0]} {words[1]}"})
    return {"listed": {k: len(v) for k, v in listed.items()}, "picked": picked, "skipped": skipped}


def number_regex(number: str) -> re.Pattern:
    digits = re.escape(number)
    if len(number) == 4 and number.isdigit():                    # a printed thousands space: «1 036»
        digits = re.escape(number[0]) + r"[\s ]?" + re.escape(number[1:])
    return re.compile(r"(?<!\d)(?<!\d[.,])" + digits + r"(?!\d)(?![.,]\d)")


def check_pages(api: Api, checks: list[dict]) -> dict:
    """{page_id: true / false (number in the canonical text) or null (no such page)} per number."""
    out: dict[str, dict[str, bool | None]] = {}
    for c in checks:
        rx = number_regex(c["number"])
        res: dict[str, bool | None] = {}
        for pid in c["pages"]:
            code, r = api.call("GET", "/v1/page/" + urllib.parse.quote(pid) + "?include=text&max_chars=60000")
            if code != 200:
                res[pid] = None
                continue
            text = ((r.get("item") or {}).get("record") or {}).get("normalized_text") or ""
            res[pid] = bool(rx.search(text))
        out[c["number"]] = res
    return out


def surname_first(name: str) -> str:
    """'В.А. Асанов' → 'Асанов В.А.'; names already 'Фамилия И.О.' stay."""
    m = re.match(r"^((?:[A-ZА-ЯЁ]\.\s?){1,3})\s*(\S.+)$", name.strip())
    return f"{m.group(2).strip()} {m.group(1).replace(' ', '')}" if m else name.strip()


def pick_authors(api: Api, n: int, min_works: int, max_source: int) -> dict:
    authors: dict[str, dict] = {}
    active: set[str] = set()
    for i in range(1, max_source + 1):
        sid = f"VKM-SRC-{i:03d}"
        code, s = api.call("GET", f"/v1/source/{sid}")
        if code != 200:
            continue
        rec = (s.get("item") or {}).get("record") or {}
        if rec.get("lifecycle_status") != "ACTIVE":
            continue
        active.add(sid)
        for link in rec.get("work_links") or []:
            wid = link.get("work_id")
            if not wid:
                continue
            c2, w = api.call("GET", f"/v1/work/{urllib.parse.quote(wid)}")
            wr = (w.get("item") or {}).get("record") or {}
            for a in wr.get("authors") or []:
                aid = a.get("author_id")
                if not aid:
                    continue
                d = authors.setdefault(aid, {"works": set(), "sources": set(), "names": {}, "roles": set()})
                d["works"].add(wid)
                d["sources"].add(sid)
                nm = a.get("name_as_listed") or a.get("name_display") or ""
                d["names"][nm] = d["names"].get(nm, 0) + 1
                d["roles"].add(a.get("role"))
    cands = sorted((aid for aid, d in authors.items() if len(d["works"]) >= min_works),
                   key=lambda aid: sha("query_types_v1|d|" + aid))
    picked = []
    for aid in cands[:n]:
        d = authors[aid]
        name = sorted(d["names"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        picked.append({"author_id": aid, "name": surname_first(name), "names_as_listed": sorted(d["names"]),
                       "roles": sorted(r for r in d["roles"] if r), "works": sorted(d["works"]),
                       "sources": sorted(d["sources"] & active), "query": f"работы {surname_first(name)}"})
    return {"n_authors": len(authors), "n_candidates": len(cands), "n_active_sources": len(active), "picked": picked}


def parameters(api: Api, specs: list[dict]) -> list[dict]:
    out = []
    for p in specs:
        q = urllib.parse.urlencode({"property": p["property"], "material": p["material"], "limit": 200})
        code, r = api.call("GET", "/v1/nav/parameters?" + q)
        rec = (r.get("item") or {}).get("record") or {}
        cands = []
        for c in rec.get("candidates") or []:
            copies = [x.get("page_id") for x in c.get("copies") or [] if isinstance(x, dict) and x.get("page_id")]
            cands.append({"candidate_id": c.get("candidate_id"), "page_id": c.get("page_id"),
                          "source_id": c.get("source_id"), "copies": copies})
        out.append({"query_id": p["query_id"], "http": code, "total": rec.get("total"),
                    "property_keys": (rec.get("query") or {}).get("property_keys"),
                    "materials": (rec.get("query") or {}).get("materials"), "unresolved": rec.get("unresolved"),
                    "candidates": cands})
    return out


def main() -> None:
    raw = globals().get("INPUT_JSON")
    if not raw:
        raise SystemExit("INPUT_JSON is not embedded (use build_set.py bundle)")
    spec = json.loads(raw)
    api = Api()
    code, st = api.call("GET", "/v1/status")
    can = (((st.get("item") or {}).get("record") or st.get("status") or {}).get("canonical") or {})
    out = {"kind": "query_types_v1 truth", "canonical_snapshot_id": can.get("snapshot_id"),
           "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    out["objects"] = pick_objects(api, spec["sweep_sources"], spec["object_pick"])
    sys.stderr.write(f"objects picked {len(out['objects']['picked'])}\n")
    out["boreholes"] = check_pages(api, spec["borehole_checks"])
    sys.stderr.write("borehole pages checked\n")
    out["authors"] = pick_authors(api, spec["authors"]["n"], spec["authors"]["min_works"], spec["authors"]["max_source"])
    sys.stderr.write(f"authors picked {len(out['authors']['picked'])}\n")
    out["parameters"] = parameters(api, spec["parameters"])
    out["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["stats"] = dict(STATS)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
