"""QUERY_TYPES_V1 — the frozen query set with automatic truth (PREREGISTRATION_EXPERIMENTS_2026-10-13.md §E10).

Five query types, 70 queries; the truth of every query is derived from catalogues, the canon, the registry and NAV
BEFORE any system runs (never from a system's answer):

    A_NL      15  natural-language questions rewritten from topic_v1 topics      → truth of the topic (V, P)
    B_EN      15  English formulations of Russian topic_v1 topics                → truth of the topic (V, P)
    C_OBJECT  16  4 figures + 4 tables with a printed label (canon) and 8 boreholes (borehole catalogue)
                                                                                 → pages of the object / of the locators
    D_AUTHOR  12  «работы <author>» (registry: work authors)                     → sources of the author's works
    E_PARAM   12  a property of a material (NAV find_parameters)                 → pages of the candidates (not evidence)

The topics of A and B: topic_v1 topics in sha256 order of ``query_types_v1|a|<topic_id>`` (first 15) and of
``query_types_v1|b|<topic_id>`` (first 15 not in A); the wordings are written by the benchmark agent (13.10.2026) from
the topic title, without the corpus and without any system answer. Boreholes: catalogue rows of SKRU1 / SKRU2 / SKRU3 /
SKRU1_SKRU2_PILLAR with a 3–4 digit number and ≥ 2 mentions, in sha256 order of ``query_types_v1|c-bh|<borehole_id>``,
the first 8 distinct numbers; truth = the locator pages of every catalogue row with that number (the query cannot tell
two boreholes of one number apart), on the page basis the check of ``truth_core.py`` supports.

    bundle    → <work>/truth_bundle.py (input + truth_core.py) for the API container
    assemble  container output → benchmarks/query_types_v1/query_types_v1.jsonl + truth_receipt_v1.json (ids only)

usage: python benchmarks/query_types_v1/scripts/build_set.py bundle --work <dir>
       ssh core "docker exec -i vkm-core-api-1 /opt/vkm/bin/python -" < <dir>/truth_bundle.py > <dir>/truth_raw.json
       python benchmarks/query_types_v1/scripts/build_set.py assemble --raw <dir>/truth_raw.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OUT_DIR = REPO / "benchmarks" / "query_types_v1"
SET_FILE = OUT_DIR / "query_types_v1.jsonl"
RECEIPT_FILE = OUT_DIR / "truth_receipt_v1.json"
TOPIC_SET = REPO / "benchmarks" / "topic_v1" / "topic_set_v1.jsonl"
TOPIC_SET_SHA256 = "4dbc595723cbb9ed3e59d1a71cbe84301c9554ecedc41b7f3abf654258bac2b1"          # LF-normalised
BOREHOLES = REPO / "evidence" / "boreholes" / "borehole_catalog.csv"
SWEEP_SOURCES = [f"VKM-SRC-{n:03d}" for n in range(1, 42) if n not in (13, 22)]      # topic_bench.SWEEP_SOURCES
BH_SCOPES = frozenset({"SKRU1", "SKRU2", "SKRU3", "SKRU1_SKRU2_PILLAR"})
N_BOREHOLES = 8
OFFSETS = (-2, -1, 0, 1, 2)
OBJECT_PICK = {"FIGURE": 4, "TABLE": 4}
AUTHORS = {"n": 12, "min_works": 2, "max_source": 280}
SET_VERSION = "query_types_v1"

# A_NL: natural-language questions (topic in sha256 order «a»; written from the topic title, 13.10.2026)
A_NL = {
    "PC-40": "Как рассолы гидрозакладки и шламы влияют на прочность и края междукамерных целиков?",
    "PC-61": "Как определяют горизонтальные деформации земной поверхности по измерениям между реперами профильной линии?",
    "PC-11": "Какие критерии прочности описывают переход соляных пород в предельное состояние при боковом давлении?",
    "MM-PROT": "Как рассчитать ширину предохранительного или барьерного целика?",
    "PC-38": "Как со временем уплотняется и набирает жёсткость закладочный массив?",
    "PC-53": "Какие сейсмические события вызывает разработка калийных рудников и как оценивают их энергию?",
    "PC-19": "Как происходит релаксация напряжений в соляных породах?",
    "MM-SUPPORT": "Как выбрать длину и шаг анкеров для крепления горных выработок?",
    "MM-UGS": "Как деформируется земная поверхность над подземным хранилищем газа при закачке и отборе?",
    "PC-42": "Как давление подземных вод влияет на консолидацию и осадку надсолевой толщи?",
    "PC-49": "Как температура влияет на скорость ползучести соли?",
    "PC-55": "Почему в калийных рудниках происходят внезапные выбросы соли и газа?",
    "PC-39": "Когда закладка начинает сопротивляться опусканию кровли камеры?",
    "MM-DISS": "Как растворяются междукамерные целики при затоплении рудника?",
    "PC-71": "Какие естественные тектонические движения земной поверхности происходят в районе месторождения?",
}
# B_EN: English formulations of Russian topics (topic in sha256 order «b», not in A; written from the topic title)
B_EN = {
    "MM-SUBS": "methods for calculating ground surface subsidence above potash mines",
    "MM-BLAST": "drilling and blasting parameters in salt rocks",
    "PC-34": "integrity of the water-protective strata: shear and tensile fracture zones above mined-out areas",
    "PC-24": "roof bed separation and roof falls in mining rooms",
    "PC-56": "effect of blasting on the surrounding rock mass in potash mining",
    "PC-08": "lateral heterogeneity of rock properties: folds and replacement zones of potash seams",
    "PC-46": "natural salt karst and dissolution at the salt table",
    "MM-DEF": "calculating ground surface deformation indicators from survey observations",
    "PC-26": "shear, creep and opening along clay interlayers in salt rock",
    "PC-52": "seasonal frost heave and thermal effects on benchmark and building observations",
    "MM-SCALE": "structural weakening coefficient from rock sample to rock mass",
    "MM-HYDRO": "hydrogeological models of a salt basin",
    "PC-12": "strain softening and residual strength of salt rocks beyond peak strength",
    "MM-GASDYN": "forecasting gas-dynamic phenomena in potash mines",
    "PC-66": "surface displacement noise unrelated to mining: waste piles, construction, dewatering",
}
# E_PARAM: property × material (NAV find_parameters arguments) and the query text
E_PARAM = [
    ("предел прочности на одноосное сжатие", "каменная соль", "предел прочности каменной соли на одноосное сжатие"),
    ("предел прочности на одноосное сжатие", "сильвинит", "прочность сильвинита при одноосном сжатии"),
    ("предел прочности на одноосное сжатие", "карналлит", "предел прочности карналлита на сжатие"),
    ("предел прочности на растяжение", "каменная соль", "предел прочности каменной соли на растяжение"),
    ("предел прочности на растяжение", "сильвинит", "прочность сильвинита на растяжение"),
    ("модуль упругости", "каменная соль", "модуль упругости каменной соли"),
    ("модуль деформации", "сильвинит", "модуль деформации сильвинита"),
    ("коэффициент Пуассона", "каменная соль", "коэффициент Пуассона каменной соли"),
    ("коэффициент Пуассона", "карналлит", "коэффициент Пуассона карналлита"),
    ("плотность", "каменная соль", "плотность каменной соли"),
    ("предел длительной прочности", "сильвинит", "длительная прочность сильвинита"),
    ("пористость", "песчаник", "пористость песчаника"),
]


def sha(tag: str) -> str:
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_topics() -> dict[str, dict]:
    if sha256_lf(TOPIC_SET) != TOPIC_SET_SHA256:
        raise SystemExit("topic_set_v1.jsonl does not match the pre-registered sha256")
    return {d["topic_id"]: d for d in (json.loads(x) for x in TOPIC_SET.read_text(encoding="utf-8").splitlines()
                                       if x.strip())}


def topic_order(topics: dict[str, dict]) -> tuple[list[str], list[str]]:
    a = sorted(topics, key=lambda t: sha(f"query_types_v1|a|{t}"))[:15]
    b = [t for t in sorted(topics, key=lambda t: sha(f"query_types_v1|b|{t}")) if t not in a][:15]
    return a, b


# ------------------------------------------------------------------------------------------------ boreholes
def norm_number(token: str) -> str:
    return re.sub(r"[\s ]", "", token.strip())


def numbers_of(row: dict) -> list[str]:
    return [n for n in (norm_number(x) for x in (row.get("alternate_ids") or "").split("|")) if n]


def locator_pages(locators: str) -> tuple[list[tuple[str, int]], list[str]]:
    """(pdf-page tokens «SSS:pNN» → (source, page number), canonical page ids of «разбор страниц: SSS:pNNNN»)."""
    pdf: list[tuple[str, int]] = []
    canon: list[str] = []
    for seg in (locators or "").split("|"):
        seg = seg.strip()
        if seg.startswith("разбор страниц"):
            canon += [f"VKM-SRC-{s}:{k}{n}" for s, k, n in re.findall(r"\b(\d{3}):([pr])(\d{4})\b", seg)]
        else:
            m = re.match(r"^(\d{3}):p(\d{1,4})\b", seg)
            if m:
                pdf.append((f"VKM-SRC-{m.group(1)}", int(m.group(2))))
    return list(dict.fromkeys(pdf)), list(dict.fromkeys(canon))


def page_id(source: str, index: int, prefix: str = "p") -> str:
    return f"{source}:{prefix}{index:04d}"


PREFIXES = ("p", "r")          # PDF / DjVu pages; render pages of DOCX sources (VKM-SRC-023, -269, -270: «…:r0001»)


def resolve(found: dict[str, bool | None], source: str, index: int) -> tuple[str | None, bool | None]:
    """The canonical page of a pdf-page locator: the «p» id when the canon has it, else the «r» id (DOCX render pages);
    with whether the number occurs in its text (None: no such page)."""
    if index < 1:
        return None, None
    for prefix in PREFIXES:
        pid = page_id(source, index, prefix)
        if found.get(pid) is not None:
            return pid, found[pid]
    return None, None


def pick_boreholes(rows: list[dict]) -> list[str]:
    cands = []
    for r in rows:
        nums = numbers_of(r)
        try:
            mentions = int(r.get("n_mentions") or 0)
        except ValueError:
            mentions = 0
        if (r.get("scope") in BH_SCOPES and nums and re.fullmatch(r"\d{3,4}", nums[0]) and mentions >= 2):
            cands.append((sha("query_types_v1|c-bh|" + r["borehole_id"]), nums[0]))
    out: list[str] = []
    for _h, n in sorted(cands):
        if n not in out:
            out.append(n)
        if len(out) == N_BOREHOLES:
            break
    return out


def borehole_rows(rows: list[dict], number: str) -> list[dict]:
    return [r for r in rows if number in numbers_of(r)]


def borehole_checks(rows: list[dict], numbers: list[str]) -> list[dict]:
    checks = []
    for n in numbers:
        pages: list[str] = []
        for r in borehole_rows(rows, n):
            pdf, canon = locator_pages(r.get("locators") or "")
            pages += [page_id(s, i + o, pf) for s, i in pdf for o in OFFSETS for pf in PREFIXES if i + o >= 1]
            pages += canon
        checks.append({"number": n, "pages": list(dict.fromkeys(pages))})
    return checks


def page_basis(rows: list[dict], numbers: list[str], found: dict[str, dict[str, bool | None]]) -> dict:
    """Share of pdf-page locators whose page at offset o holds the number; the adopted offset (0 unless another offset
    is better by more than 0.05) and the share of canonical locators that hold it."""
    rate = {}
    for o in OFFSETS:
        hit = tot = 0
        for n in numbers:
            for r in borehole_rows(rows, n):
                for s, i in locator_pages(r.get("locators") or "")[0]:
                    v = resolve(found.get(n, {}), s, i + o)[1]
                    if v is not None:
                        tot += 1
                        hit += int(v)
        rate[o] = {"found": hit, "checked": tot, "share": round(hit / tot, 4) if tot else None}
    best = max(OFFSETS, key=lambda o: (rate[o]["share"] or 0.0, o == 0))
    adopted = 0 if (rate[0]["share"] or 0.0) >= (rate[best]["share"] or 0.0) - 0.05 else best
    hit = tot = 0
    for n in numbers:
        for r in borehole_rows(rows, n):
            for p in locator_pages(r.get("locators") or "")[1]:
                v = found.get(n, {}).get(p)
                if v is not None:
                    tot += 1
                    hit += int(v)
    return {"pdf_locator_offsets": {str(o): v for o, v in rate.items()}, "adopted_offset": adopted,
            "canonical_locators": {"found": hit, "checked": tot, "share": round(hit / tot, 4) if tot else None}}


def borehole_truth(rows: list[dict], number: str, offset: int, found: dict[str, bool | None]) -> dict:
    pages: list[str] = []
    for r in borehole_rows(rows, number):
        pdf, canon = locator_pages(r.get("locators") or "")
        pages += [resolve(found, s, i + offset)[0] or page_id(s, i + offset) for s, i in pdf if i + offset >= 1]
        pages += canon
    pages = list(dict.fromkeys(pages))
    existing = [p for p in pages if found.get(p) is not None]          # the page exists in the canon
    rws = borehole_rows(rows, number)
    return {"pages": existing, "missing_pages": [p for p in pages if found.get(p) is None],
            "borehole_ids": sorted(r["borehole_id"] for r in rws),
            "scopes": sorted({r.get("scope") or "" for r in rws}),
            "number_in_text": sum(1 for p in existing if found.get(p))}


# ------------------------------------------------------------------------------------------------ bundle / assemble
def bundle(work: Path) -> None:
    rows = list(csv.DictReader(BOREHOLES.open(encoding="utf-8")))
    numbers = pick_boreholes(rows)
    spec = {"sweep_sources": SWEEP_SOURCES, "object_pick": OBJECT_PICK, "authors": AUTHORS,
            "borehole_checks": borehole_checks(rows, numbers),
            "parameters": [{"query_id": f"QT-E-{i:02d}", "property": p, "material": m}
                           for i, (p, m, _t) in enumerate(E_PARAM, 1)]}
    text = json.dumps(spec, ensure_ascii=False)
    if '"""' in text:
        raise SystemExit("input contains a triple quote")
    work.mkdir(parents=True, exist_ok=True)
    code = (Path(__file__).resolve().parent / "truth_core.py").read_text(encoding="utf-8")
    (work / "truth_bundle.py").write_bytes(('INPUT_JSON = r"""' + text + '"""\n' + code).encode("utf-8"))
    print(f"bundle: boreholes {numbers}, pages to check {sum(len(c['pages']) for c in spec['borehole_checks'])}")


def target(page: str, alts: list[str] | tuple = ()) -> dict:
    return {"page_id": page, "alt_page_ids": [a for a in dict.fromkeys(alts) if a != page]}


def assemble(raw_path: Path) -> None:
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    topics = load_topics()
    a_ids, b_ids = topic_order(topics)
    if list(A_NL) != a_ids or list(B_EN) != b_ids:
        raise SystemExit(f"the A / B wordings do not follow the topic order: {a_ids} / {b_ids}")
    rows = list(csv.DictReader(BOREHOLES.open(encoding="utf-8")))
    numbers = pick_boreholes(rows)
    found = raw["boreholes"]
    basis = page_basis(rows, numbers, found)
    lines: list[dict] = []
    for i, tid in enumerate(a_ids, 1):
        lines.append({"query_id": f"QT-A-{i:02d}", "type": "A_NL", "text": A_NL[tid], "topic_id": tid,
                      "truth": {"kind": "TOPIC", "topic_id": tid}, "basis": "topic_v1 truth of the topic"})
    for i, tid in enumerate(b_ids, 1):
        lines.append({"query_id": f"QT-B-{i:02d}", "type": "B_EN", "text": B_EN[tid], "topic_id": tid,
                      "truth": {"kind": "TOPIC", "topic_id": tid}, "basis": "topic_v1 truth of the topic"})
    nf = nt = 0
    for o in raw["objects"]["picked"]:
        if o["kind"] == "FIGURE":
            nf += 1
            qid = f"QT-C-F{nf}"
        else:
            nt += 1
            qid = f"QT-C-T{nt}"
        lines.append({"query_id": qid, "type": "C_OBJECT", "subtype": o["kind"], "text": o["query"],
                      "truth": {"kind": "PAGES", "targets": [target(p) for p in o["truth_pages"]]},
                      "basis": "canon objects: same label number, caption holds both stems",
                      "meta": {"object_id": o["object_id"], "label": o["label"], "stems": o["stems"],
                               "truth_objects": o["truth_objects"]}})
    for k, n in enumerate(numbers, 1):
        bt = borehole_truth(rows, n, basis["adopted_offset"], found.get(n, {}))
        lines.append({"query_id": f"QT-C-B{k}", "type": "C_OBJECT", "subtype": "BOREHOLE", "text": f"скважина {n}",
                      "truth": {"kind": "PAGES", "targets": [target(p) for p in bt["pages"]]},
                      "basis": "borehole catalogue locators (every row with this number)",
                      "meta": {"number": n, "borehole_ids": bt["borehole_ids"], "scopes": bt["scopes"],
                               "missing_pages": bt["missing_pages"], "number_in_text": bt["number_in_text"]}})
    for i, a in enumerate(raw["authors"]["picked"], 1):
        lines.append({"query_id": f"QT-D-{i:02d}", "type": "D_AUTHOR", "text": a["query"],
                      "truth": {"kind": "SOURCES", "source_ids": a["sources"]},
                      "basis": "registry: ACTIVE sources of the works the author is listed on",
                      "meta": {"author_id": a["author_id"], "names_as_listed": a["names_as_listed"],
                               "roles": a["roles"], "works": a["works"]}})
    params = {p["query_id"]: p for p in raw["parameters"]}
    for i, (prop, mat, text) in enumerate(E_PARAM, 1):
        qid = f"QT-E-{i:02d}"
        p = params[qid]
        by_page: dict[str, list[str]] = {}
        for c in p["candidates"]:
            if c.get("page_id"):
                by_page.setdefault(c["page_id"], [])
                by_page[c["page_id"]] += [x for x in c.get("copies") or [] if x]
        lines.append({"query_id": qid, "type": "E_PARAM", "text": text,
                      "truth": {"kind": "PAGES", "targets": [target(pg, al) for pg, al in sorted(by_page.items())]},
                      "basis": "NAV find_parameters candidates (AUTO_EXTRACTED_UNREVIEWED; not evidence)",
                      "meta": {"property": prop, "material": mat, "property_keys": p["property_keys"],
                               "nav_total": p["total"], "n_candidates": len(p["candidates"])}})
    empty = [x["query_id"] for x in lines if x["truth"]["kind"] != "TOPIC" and not (
        x["truth"].get("targets") or x["truth"].get("source_ids"))]
    if empty:
        raise SystemExit(f"queries without truth: {empty}")
    for x in lines:
        x["set_version"] = SET_VERSION
    text = "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in lines)
    SET_FILE.write_bytes(text.encode("utf-8"))
    receipt = {"set_version": SET_VERSION, "set_file": SET_FILE.name,
               "set_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
               "built_from": {"canonical_snapshot_id": raw.get("canonical_snapshot_id"),
                              "truth_raw_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                              "topic_set_sha256": TOPIC_SET_SHA256,
                              "borehole_catalog_sha256": sha256_lf(BOREHOLES),
                              "truth_core_sha256": sha256_lf(Path(__file__).resolve().parent / "truth_core.py"),
                              "started_at": raw.get("started_at"), "finished_at": raw.get("finished_at"),
                              "api_calls": raw.get("stats")},
               "types": dict(Counter(x["type"] for x in lines)),
               "objects": {"listed": raw["objects"]["listed"], "skipped": raw["objects"]["skipped"]},
               "boreholes": {"numbers": numbers, "page_basis": basis},
               "authors": {k: raw["authors"][k] for k in ("n_authors", "n_candidates", "n_active_sources")},
               "truth_sizes": {x["query_id"]: (len(x["truth"].get("targets") or x["truth"].get("source_ids") or [])
                                               if x["truth"]["kind"] != "TOPIC" else None) for x in lines}}
    RECEIPT_FILE.write_bytes((json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    print(json.dumps({k: receipt[k] for k in ("set_sha256", "types", "boreholes")}, ensure_ascii=False)[:1500])


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bundle")
    b.add_argument("--work", type=Path, required=True)
    a = sub.add_parser("assemble")
    a.add_argument("--raw", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "bundle":
        bundle(args.work)
    else:
        assemble(args.raw)


if __name__ == "__main__":
    main()
