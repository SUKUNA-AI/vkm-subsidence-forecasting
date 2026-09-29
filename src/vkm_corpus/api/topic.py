"""Topic dossier «от А до Я» (``reconstruct_topic``): one call → a compact, character-budgeted, fully cited map of what
the corpus and the PUBLIC evidence catalogues contain about a question.

Parts (each optional part degrades to a warning when its dependency is missing):

a. retrieval — the injected :class:`TopicRetrieval` (default :class:`HybridTopicRetrieval`: BM25 + dense + late
   interaction over pages) runs up to 5 formulations of the topic (the query, the caller's paraphrases, the query in the
   other language from the NAV term dictionary, the concept's synonyms — SAME_AS and narrower terms — and the query
   widened by its strongest concept neighbours) over two tiers of
   sources — the VKM core (sources of the evidence catalogues and sources whose register scope is VKM/SKRU/regional)
   and the whole corpus — and fuses them by RRF into a ranked page list per tier (TOPIC_BENCHMARK_V1: several
   formulations and the core tier are the measured gains); pages are grouped by source and NAV section
   (``section_of_page``);
b. NAV sections whose titles match the formulations (stems, idf-weighted; plus the lemma-based
   ``NavStore.search_sections``), checked on their pages (the query words meeting on one page); the concept's own
   mentions only strengthen sections found otherwise (neighbours widen the query, they are not sections); one section
   per branch and a few per source, shown in the two tiers, each with its pages, best units and a ≤ 200-character
   snippet of canonical text (runtime only);
c. formulas on the chosen pages, by the meaning of their symbols (``find_formulas(concept=…)``) and with matching
   «где…» definitions near the hit pages: number, section, symbols, parameter candidates (AUTO_EXTRACTED_UNREVIEWED);
   figures and tables near the hit pages whose captions or headers carry the query words (visual evidence);
d. the concept graph (``explore_concept``) and topics (``find_topics`` / ``topic``, when the build has them);
e. provenance of the sources found, in the two tiers (register scope, primary work: title, authors, year, type) and
   CITES among them;
f. the PUBLIC catalogues (:class:`vkm_corpus.catalogues.store.CatalogueStore`): physics processes PC-xx (the words of
   the query, the paraphrases and the synonyms; the sources found; their evidence pages among the pages found) with
   their evidence records, formula-registry models, conflicts, causal neighbours, observation operators and matching
   rows of the evidence catalogues;
g. gaps: required parameters of the matched processes without a linked evidence record (or only with records of other
   sites) and the curated data gaps — an explicit UNKNOWN list; nothing is filled.

Navigation ≠ evidence: NAV items are AUTO_EXTRACTED_UNREVIEWED, catalogue records keep their own status (FACT …
UNKNOWN), scope and scale; no value is invented. Ordering is deterministic (scores, then ids). The markdown rendering is
cut to ``budget_chars``: the lowest-priority items go first and the dossier says what was dropped and how to get it.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Protocol

LOG = logging.getLogger("vkm.api.topic")

RULE_VERSION = "topic_dossier_v2"
DEFAULT_BUDGET, MIN_BUDGET, MAX_BUDGET = 12_000, 1_000, 60_000
SNIPPET_CHARS = 200
RETRIEVAL_UNITS = 50               # pages asked per formulation and tier (the hybrid search's maximum)
CORE_PAGES, REST_PAGES = 50, 25    # fused pages kept per tier (the same budget as one query for the core)
MAX_FORMULATIONS = 5
MAX_PARAPHRASES = 4
RRF_K = 60
TRANSLATE_DEFAULT = True           # translation formulation by default (benchmarks/term_dictionary_v1)
RETRIEVAL_WORKERS = 4
PER_SOURCE_SECTIONS = 3
MAX_SCAN_PAGES = 24
FORMULA_FOCUS_PAGES = 4            # a section longer than this lends its rank only to formulas near its hit pages
CITES_WAIT_S = 8.0                 # the call that starts the CITES read waits this long (then: CITES_PENDING)
SKRU1_SCOPES = frozenset({"SKRU1", "SKRU1_SKRU2_PILLAR"})
# register scopes (normalised) of the VKM core tier: VKM_REGIONAL, OTHER_VKM_SITE, SKRU1…, BKPRU…, SOLIKAMSK…, USOLSK…
CORE_SCOPE_PREFIXES = ("VKM", "OTHER_VKM", "SKRU", "BKPRU", "SOLIKAMSK", "USOLSK")
# catalogue tables whose source ids make a source «catalogued» (the evidence catalogues were built from it)
CORE_SOURCE_TABLES = (("source_coverage_master", "source_id"), ("process_evidence_links", "source_id"),
                      ("mathematical_model_registry", "source_ids"),
                      ("physics_coverage_and_execution_matrix", "source_ids"))
TIER_LABEL = {"CORE": "ядро ВКМ", "REST": "остальной корпус"}
NAV_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
NOTE = ("Карта навигации, не evidence: разделы, формулы и понятия — AUTO_EXTRACTED_UNREVIEWED; записи каталогов — со "
        "своим статусом (FACT … UNKNOWN), scope и scale; сниппеты — текст корпуса только в ответе. Значения не "
        "подставлять: UNKNOWN остаётся UNKNOWN. Открывать: get_section, get_formula_context, get_page, get_object.")

# ============================================================================================================ text
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_RU_ENDINGS = tuple(sorted(set("""ями ами ого его ому ему ыми ими ых их ой ей ый ий ая яя ое ее ые ие ую юю ом ем ам
    ям ах ях ов ев ью ия ья ье ы и а я о е у ю ь""".split()), key=len, reverse=True))
_EN_ENDINGS = ("ings", "ing", "ies", "es", "ed", "s")
STOP = frozenset("""и в во на по для о об от до из с со к ко при за под над без через между как что это или а также
    их его ее её не но же ли бы то при про the of and in on for to an with by from as at is are be or into its their
    about""".split())


def norm(text: Any) -> str:
    return str(text or "").lower().replace("ё", "е")


def stem(word: str) -> str:
    """Crude stem for prefix matching (no morphology needed): the Russian inflection ending stripped, English
    plural/-ing/-ed stripped. Derived forms meet through :func:`variants` (к/ц/г alternations)."""
    w = norm(word)
    if re.search("[а-я]", w):
        for e in _RU_ENDINGS:
            if w.endswith(e) and len(w) - len(e) >= 3:
                return w[: -len(e)]
        return w
    for e in _EN_ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[: -len(e)]
    return w


@lru_cache(maxsize=4096)
def variants(s: str) -> tuple[str, ...]:
    """Prefixes a stem stands for: itself and the consonant alternations of Russian derivation (закладк → закладоч,
    механик → механич, границ → граничн, нагрузк → нагрузоч) — never a shorter stem (механи… would reach
    «механизация»)."""
    if len(s) >= 5 and s[-1] in "кцг" and re.search("[а-я]", s):
        alt = {"к": "ч", "ц": "ч", "г": "ж"}[s[-1]]
        return (s, s[:-1] + alt, s[:-1] + "о" + alt)
    return (s,)


def begins(word: str, s: str) -> bool:
    return word.startswith(variants(s))


def words_of(text: Any) -> frozenset[str]:
    """Lower-case words of a text (ё → е; hyphenated words also split into their parts)."""
    return frozenset(_WORD.findall(norm(text)))


def query_stems(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(norm(text)):
        if len(w) < 3 or w in STOP:
            continue
        s = stem(w)
        if len(s) >= 3 and s not in out:
            out.append(s)
    return out


def hits(stems: Iterable[str], words: Iterable[str]) -> set[str]:
    """The stems that begin some word."""
    words = tuple(words)
    return {s for s in stems if any(begins(w, s) for w in words)}


def short(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: max(1, limit - 1)].rstrip() + "…"


def split_ids(value: Any) -> list[str]:
    return [p.strip() for p in re.split(r"[;,]", str(value or "")) if p.strip()]


def snippet(text: Any, stems: list[str], limit: int = SNIPPET_CHARS) -> str | None:
    """A ≤ ``limit``-character window of ``text`` around the densest run of query-stem words (None without a hit)."""
    t = " ".join(str(text or "").split())
    if not t:
        return None
    low = norm(t)
    marks = [(m.start(), s) for m in _WORD.finditer(low) for s in stems if begins(m.group(0), s)]
    if not marks:
        return None
    best, best_n = marks[0][0], 0
    for pos, _s in marks:
        n = len({s for p, s in marks if pos - 40 <= p <= pos + limit - 60})
        if n > best_n:
            best, best_n = pos, n
    start = max(0, best - 40)
    if start:
        space = t.rfind(" ", 0, start)
        start = space + 1 if space >= 0 and start - space < 20 else start
    room = limit - (1 if start else 0)
    piece = t[start:start + room]
    if start + room < len(t):
        piece = piece[: room - 1].rstrip() + "…"
    return ("…" if start else "") + piece


def idf_weights(stems: list[str], docs: list[frozenset[str]]) -> dict[str, float]:
    n = len(docs)
    return {s: math.log((n + 1) / (sum(1 for d in docs if any(begins(w, s) for w in d)) + 1)) + 1.0 for s in stems}


# ============================================================================================================ retrieval
class TopicRetrieval(Protocol):
    """Injected retrieval of the dossier: ``search`` returns ``{"units": [...], ...}`` where a unit is a dict with
    ``page_id``, ``source_id``, ``page_index``, ``rank`` (1 = best), ``unit_id``, ``object_ids`` (blocks of the unit)
    and optional ``stages``; extra keys (``engine``, ``build_id``, ``built_from_snapshot_id``, ``late``,
    ``model_key``, ``timings_ms``, ``warnings``) describe the run. Failures raise (the dossier degrades to NAV only)."""

    def search(self, query: str, *, source_ids: list[str] | None = None,
               limit: int = RETRIEVAL_UNITS) -> dict[str, Any]: ...


class HybridTopicRetrieval:
    """Default retrieval: the API's hybrid backend (BM25 + dense + late interaction when on) over pages."""

    def __init__(self, backend: Any, *, candidates: int = 100) -> None:
        self.backend = backend
        self.candidates = candidates

    def search(self, query: str, *, source_ids: list[str] | None = None,
               limit: int = RETRIEVAL_UNITS) -> dict[str, Any]:
        request = {"query": query, "kinds": ("PAGE",), "filters": {"source_id": list(source_ids)} if source_ids else {},
                   "size": max(1, min(50, limit)), "offset": 0, "candidates": self.candidates,
                   "include_duplicates": False, "exact": False, "late": None, "late_candidates": 100,
                   # a dossier is built from text units (sections, formulas, blocks): pages the page-image leg adds
                   # have no unit to cite, so the visual route stays off here (agent VIS, MODEL_CHOICE)
                   "visual_route": False}
        response = self.backend.search(request)
        units = []
        for hit in response.get("hits") or []:
            trace = hit.get("trace") or {}
            late_unit, dense_unit = trace.get("late_unit") or {}, trace.get("dense_unit") or {}
            blocks = [b.get("id") for b in (hit.get("best_blocks") or []) if b.get("id")]
            units.append({"page_id": hit.get("page_id") or hit.get("id"), "source_id": hit.get("source_id"),
                          "page_index": hit.get("page_index"), "rank": hit.get("rank"),
                          "unit_id": late_unit.get("unit_id") or dense_unit.get("unit_id"),
                          "object_ids": blocks or list(dense_unit.get("object_ids") or [])[:20],
                          "stages": {k: trace.get(k) for k in ("bm25_rank", "dense_rank", "fused_rank", "late_rank")}})
        dense = (response.get("stages") or {}).get("dense") or {}
        return {"engine": "opensearch-hybrid", "units": units, "late": response.get("late"),
                "build_id": dense.get("build_id"), "built_from_snapshot_id": dense.get("built_from_snapshot_id"),
                "model_key": dense.get("model_key"), "timings_ms": response.get("timings_ms"),
                "warnings": [str(w)[:200] for w in response.get("warnings") or []]}


# ============================================================================================================ output
@dataclass
class TopicRequest:
    query: str
    budget_chars: int = DEFAULT_BUDGET
    source_ids: tuple[str, ...] = ()
    max_sources: int = 10
    max_sections: int = 12
    max_formulas: int = 10
    max_processes: int = 6
    paraphrases: tuple[str, ...] = ()        # other wordings of the topic from the caller (fused with the query)
    translate: bool = TRANSLATE_DEFAULT      # + the query in the other language (NAV term dictionary, agent TR)


@dataclass
class Entry:
    """One rendered item: markdown lines, its JSON record and a priority for the budget (higher stays longer)."""

    category: str
    key: str
    priority: float
    lines: list[str]
    data: Any = None


@dataclass
class Dossier:
    record: dict[str, Any]
    warnings: list[dict[str, Any]]
    projection: dict[str, Any] | None
    markdown: str


CATEGORIES: tuple[tuple[str, str, str, str], ...] = (
    # key, heading, short name, how to get more / open
    ("sections_core", "Разделы — ядро ВКМ (источники каталогов и области ВКМ/СКРУ)", "разделы ядра",
     "search_sections(query) · get_outline(source_id) · get_section(section_id)"),
    ("sections_rest", "Разделы — остальной корпус", "разделы корпуса",
     "search_sections(query) · reconstruct_topic(source_ids=[…])"),
    ("formulas", "Формулы", "формулы", "find_formulas(concept=…) · get_formula_context(formula_id)"),
    ("visual", "Рисунки и таблицы у найденных страниц", "рисунки/таблицы", "get_figure · get_table · get_page"),
    ("concept", "Понятия", "понятия", "explore_concept(term)"),
    ("topics", "Темы", "темы", "find_topics / topic"),
    ("sources_core", "Источники — ядро ВКМ", "источники ядра",
     "get_source · get_work · reconstruct_topic(source_ids=[…])"),
    ("sources_rest", "Источники — остальной корпус", "источники корпуса", "get_source · get_work"),
    ("citations", "Цитирования", "цитирования", "get_citations(work_id)"),
    ("processes", "Процессы (каталог физики)", "процессы", "catalogues physics_coverage_and_execution_matrix"),
    ("gaps", "Пробелы — UNKNOWN (не заполнять)", "пробелы", "catalogues process_evidence_links · data_gaps"),
    ("models", "Модели (реестр формул)", "модели", "catalogues mathematical_model_registry"),
    ("conflicts", "Конфликты", "конфликты", "catalogues *_conflicts"),
    ("evidence", "Записи evidence-каталогов", "записи evidence", "catalogues evidence/*"),
    ("operators", "Операторы наблюдения", "операторы", "catalogues observation_operator_design"),
    ("causal", "Причинные связи", "связи", "catalogues causal_graph_edges"),
)
HEADINGS = {k: h for k, h, _s, _t in CATEGORIES}
SHORT_NAMES = {k: s for k, _h, s, _t in CATEGORIES}
HOW_TO = {k: t for k, _h, _s, t in CATEGORIES}
# budget priorities (higher stays longer): priority = base − step · rank; the gaps of a process follow the process
BASE_PRIORITY = {"sections_core": 100, "processes": 92, "concept": 88, "sections_rest": 84, "formulas": 82,
                 "conflicts": 80, "sources_core": 78, "models": 74, "visual": 72, "topics": 70, "sources_rest": 64,
                 "evidence": 58, "citations": 56, "operators": 50, "causal": 48}
STEP = {"sections_core": 5, "processes": 6, "concept": 12, "sections_rest": 6, "formulas": 5, "conflicts": 6,
        "sources_core": 4, "models": 6, "visual": 5, "topics": 6, "sources_rest": 5, "evidence": 5, "citations": 4,
        "operators": 6, "causal": 4}


def _prio(category: str, rank: int) -> float:
    return BASE_PRIORITY[category] - STEP[category] * rank


def _pages(first: Any, last: Any) -> str:
    if first is None:
        return "с. ?"
    return f"с. {first}" if last in (None, first) else f"с. {first}–{last}"


def render(head: list[str], entries: list[Entry], dropped: list[Entry], extra: dict[str, int],
           budget: int) -> str:
    lines = list(head)
    for cat, heading, _short, _how in CATEGORIES:
        mine = [e for e in entries if e.category == cat]
        if not mine:
            continue
        lines.append(f"## {heading}")
        for e in mine:
            lines.extend(e.lines)
        lines.append("")
    more: dict[str, list[Entry]] = {}
    for e in dropped:
        more.setdefault(e.category, []).append(e)
    cats = [c for c, _h, _s, _t in CATEGORIES if more.get(c) or extra.get(c)]
    if cats:
        parts = []
        for c in cats:
            ids = ", ".join(f"`{e.key}`" for e in more.get(c, [])[:2])
            parts.append(f"{SHORT_NAMES[c]} +{len(more.get(c, [])) + extra.get(c, 0)}" + (f" ({ids})" if ids else ""))
        lines += [f"## Не вошло (бюджет {budget} знаков, лимиты): ids и как получить — budget.trimmed в JSON",
                  " · ".join(parts),
                  "дальше: search_sections · get_outline · find_formulas · explore_concept · reconstruct_topic с "
                  "бо́льшим budget_chars или source_ids=[…]"]
    return "\n".join(lines).rstrip() + "\n"


def fit_budget(head: list[str], entries: list[Entry], extra: dict[str, int],
               budget: int) -> tuple[list[Entry], list[Entry], str]:
    """Drop the lowest-priority entries until the markdown fits the budget (a hard cap: cut as the last resort)."""
    kept, dropped = list(entries), []
    order = sorted(entries, key=lambda e: (e.priority, e.category, e.key))
    text = render(head, kept, dropped, extra, budget)
    i = 0
    while len(text) > budget and i < len(order):
        victim = order[i]
        i += 1
        kept.remove(victim)
        dropped.append(victim)
        text = render(head, kept, dropped, extra, budget)
    if len(text) > budget:
        text = text[: budget - 2].rstrip() + "…\n"
    return kept, dropped, text


# ========================================================================================================= catalogues
PROCESS_FIELDS = (("process", 3.0), ("group_ru", 1.5), ("domain_ru", 1.5), ("causal_role", 1.0),
                  ("causal_path_to_subsidence", 1.0), ("required_variables", 1.0), ("required_parameters", 1.0),
                  ("governing_equations", 1.0), ("data_gaps", 0.5), ("notes", 0.5))
GROUP_RU = {"INIT": "начальное состояние напряжения нагрузка", "ELAST": "упругость деформация",
            "PLAST": "пластичность прочность", "RHEO": "реология ползучесть", "DAMAGE": "повреждённость разрушение",
            "MINING": "выемка горные работы камеры целики", "OVERBURDEN": "подработанная толща",
            "BACKFILL": "закладка", "HYDRO": "гидрогеология вода рассолы", "THERMAL": "температура тепло",
            "DYN": "динамика сейсмичность", "SURF": "поверхность оседание мульда сдвижение",
            "OBS": "наблюдения мониторинг", "INTERF": "контакты прослои"}
DOMAIN_RU = {"mechanics": "механика", "rheology": "реология ползучесть", "backfill": "закладка",
             "hydro": "гидрогеология вода", "thermal": "температура тепло", "damage": "повреждённость разрушение",
             "surface": "поверхность оседание", "dynamics": "динамика", "interfaces": "контакты прослои",
             "EM": "георадар электромагнитные волны"}
CONFIDENCE_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
# parameter lexicon for the gap check: key → (Russian stems of the parameter phrase, symbols, catalogue variables /
# categories whose records evidence it, evidence-link kinds that evidence it)
PARAM_LEXICON: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "density": (("плотн", "удельн вес", "объемн вес", "density"), ("ρ", "γ"),
                ("density", "unit_weight", "density_unit_weight", "density_backfill_or_fluid"), ()),
    "modulus": (("модул", "упруг", "деформируем", "жесткост", "modul"), ("E",),
                ("youngs_modulus", "deformation_modulus", "modulus", "youngs_modulus_reduction_factor"), ()),
    "poisson": (("пуассон",), ("ν",), ("poisson_ratio",), ()),
    "strength": (("прочност", "ucs", "strength"), ("σсж", "σр"),
                 ("ucs", "tensile_strength", "strength", "triaxial_strength", "residual_strength", "ucs_cube_strength",
                  "standard_strength_normative", "triaxial", "long_term_strength"), ()),
    "long_term": (("длительн",), ("Kдл", "Кдл", "σ∞", "σдл"), ("long_term_strength", "long_term_strength_at_size"),
                  ()),
    "coulomb": (("сцеплен", "трени", "кулон"), ("φ",), ("cohesion", "friction_angle"), ()),
    "stress": (("напряж", "распор", "литостат", "давлен", "нагружен"), ("σ", "σv", "σh", "λ", "K0"),
               ("vertical_stress", "lateral_stress_ratio", "in_situ_stress_measurement", "in_situ_stress", "stress",
                "kaiser_effect_pressure", "kaiser_effect_stress_lab", "pillar_stress"), ("stress_measurement",)),
    "creep": (("ползуч", "реолог", "вязк", "релаксац", "энерги активац", "абел", "нортон"), ("η", "ε̇"),
              ("creep_coefficient", "creep_stress_exponent", "creep_threshold_stress", "steady_creep_rate",
               "creep_time_scale", "viscosity", "rheology_param", "hereditary_kernel_exponent_alpha"),
              ("rheology_law",)),
    "damage": (("поврежд", "разупрочн", "дилатан", "трещиностойк", "хрупк"), (),
               ("damage_softening", "fracture_toughness_K1c", "fracture_toughness_KIc", "specific_fracture_energy",
                "critical_energy_release_rate", "brittleness_coefficient_insitu", "fracture_brittleness_damage",
                "failure_strain"), ()),
    "contact": (("контакт", "прослой", "глинист", "межслоев"), (),
                ("contact_peak_shear_strength", "contact_shear_disp_at_peak", "contact_residual_shear_strength",
                 "contact_residual_shear_disp", "contact_shear_stiffness", "contact_softening_stiffness", "interface"),
                ()),
    "moisture": (("влажн", "увлажн"), (), ("moisture_content", "ucs_vs_moisture", "moisture_effect"), ()),
    "scale": (("масштаб",), (), ("strength_at_size", "scale_shape_effect"), ()),
    "temperature": (("температур", "тепл", "термич", "геотерм"), ("T",), (), ("thermal",)),
    "hydro": (("вод", "напор", "фильтрац", "проницаем", "порист", "рассол", "гидрогеолог"), (), (), ("hydro",)),
    "backfill": (("заклад", "заполнен", "усадк", "консолидац"), (), ("density_backfill_or_fluid",), ("backfill",)),
    "geometry": (("отметк", "рельеф", "цмр", "dem", "мощност", "глубин", "ширин", "высот", "пролет", "геометр",
                  "азимут", "ориентац", "контур", "положен"), (), (), ("chronology_event",)),
}
# where such values live in the PUBLIC catalogues (a hint for the coordinator; the dossier never copies values)
PARAM_CATALOGUES = {
    "density": "mechanics_evidence_catalog", "modulus": "mechanics_evidence_catalog",
    "poisson": "mechanics_evidence_catalog", "strength": "mechanics_evidence_catalog",
    "long_term": "mechanics_evidence_catalog", "coulomb": "mechanics_evidence_catalog",
    "stress": "mechanics_evidence_catalog", "creep": "rheology_evidence_catalog",
    "damage": "interfaces_damage_catalog", "contact": "interfaces_damage_catalog",
    "moisture": "mechanics_evidence_catalog", "scale": "mechanics_evidence_catalog",
    "temperature": "thermal_evidence_catalog", "hydro": "hydro_evidence_catalog", "backfill": "backfill_event_catalog",
    "geometry": "mining_geometry_catalog · stratigraphic_thickness_observations · borehole_catalog"}
PARAM_LINK_KINDS = frozenset({"parameter", "stress_measurement", "rheology_law", "backfill", "hydro", "thermal",
                              "formula", "engineering_rule"})
CONFLICT_TABLES: tuple[tuple[str, str, tuple[tuple[str, float], ...], str, str, str], ...] = (
    # table, label, weighted text fields, status field, handling field, linked-ids field
    ("physics_conflicts", "физика", (("variable", 3.0), ("values_kept", 0.5), ("handling", 0.5)), "status",
     "handling", "affected_processes"),
    ("formula_conflicts", "формулы", (("description_ru", 1.5), ("conflict_type", 0.5), ("form_a", 0.5),
                                      ("form_b", 0.5)), "status", "resolution_needed", "model_ids"),
    ("mech_rheo_conflicts", "механика/реология", (("variable_or_topic", 3.0), ("assessment", 0.5),
                                                  ("values_as_printed", 0.5)), "resolution_status", "assessment",
     "linked_ids"),
    ("mining_conflicts", "горные работы", (("topic", 3.0), ("assessment", 0.5), ("value_a", 0.5), ("value_b", 0.5)),
     "resolution_status", "assessment", ""),
    ("chronology_conflicts", "хронология", (("topic", 3.0), ("assessment", 0.5), ("value_a", 0.5)),
     "resolution_status", "assessment", ""),
    ("monitoring_conflicts", "мониторинг", (("topic", 3.0), ("resolution", 0.5), ("value_a", 0.5)), "status",
     "resolution", ""),
    ("stratigraphy_conflicts", "стратиграфия", (("topic", 3.0), ("recommendation", 0.5),
                                                ("competing_values_as_printed", 0.5)), "resolution_status",
     "recommendation", "unit_ids"),
)
EVIDENCE_TABLES: tuple[tuple[str, str, str, tuple[tuple[str, float], ...], str], ...] = (
    # table, label, id field, weighted text fields, table topic (Russian words that make the whole table relevant)
    ("rheology_evidence_catalog", "реология", "rheo_id", (("law_name", 2.0), ("law_family", 2.0), ("lithology", 1.0),
                                                          ("planned_role", 0.5), ("applicability_SKRU1", 0.5)),
     "ползучесть реология релаксация длительная прочность creep"),
    ("backfill_event_catalog", "закладка", "row_id", (("method_as_printed", 1.0), ("material_as_printed", 1.0),
                                                      ("purpose_as_printed", 1.0), ("target_as_printed", 1.0),
                                                      ("compaction_as_printed", 1.0),
                                                      ("convergence_relation_as_printed", 1.0)),
     "закладка закладочный заполнение камер backfill"),
    ("interfaces_damage_catalog", "контакты/повреждения", "idc_id", (("phenomenon", 2.0), ("phenomenon_class", 1.5),
                                                                     ("mechanism_causal_path", 1.0)),
     "контакты прослои повреждённость разрушение трещины"),
)


def _catalogue_words(row: dict[str, Any], fields: Iterable[tuple[str, float]]) -> list[tuple[frozenset[str], float]]:
    return [(words_of(row.get(f)), w) for f, w in fields]


def _word_score(stems: list[str], idf: dict[str, float], fields: list[tuple[frozenset[str], float]],
                top_weight: float) -> tuple[float, set[str]]:
    """idf-weighted share of the query stems found, each by its best field weight (0…1), and the stems found in
    the first (name) field."""
    total = sum(idf[s] for s in stems) * top_weight
    got = 0.0
    for s in stems:
        best = max((w for words, w in fields if any(begins(x, s) for x in words)), default=0.0)
        got += idf[s] * best
    name = hits(stems, fields[0][0]) if fields else set()
    return (got / total if total else 0.0), name


# ============================================================================================================ builder
@dataclass
class _State:
    req: TopicRequest
    stems: list[str]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    units: list[dict[str, Any]] = field(default_factory=list)
    formulations: list[dict[str, Any]] = field(default_factory=list)
    core: set[str] = field(default_factory=set)
    sections: list[dict[str, Any]] = field(default_factory=list)
    formulas: list[dict[str, Any]] = field(default_factory=list)
    visual: list[dict[str, Any]] = field(default_factory=list)
    near: dict[str, float] | None = None      # hit pages and their ±1 neighbours → order key (computed once)
    concept: dict[str, Any] | None = None
    topics: list[dict[str, Any]] = field(default_factory=list)
    source_score: dict[str, float] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    citations: dict[str, Any] = field(default_factory=dict)
    processes: list[dict[str, Any]] = field(default_factory=list)
    models: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    causal: list[dict[str, Any]] = field(default_factory=list)
    operators: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, int] = field(default_factory=dict)

    def warn(self, code: str, message: str, count: int | None = None) -> None:
        self.warnings.append({"code": code, "message": message[:300], "count": count})

    def add_source(self, source_id: str | None, score: float) -> None:
        if source_id and (not self.req.source_ids or source_id in self.req.source_ids):
            self.source_score[source_id] = self.source_score.get(source_id, 0.0) + score

    def tier(self, source_id: str | None) -> str:
        return "CORE" if source_id in self.core else "REST"


class DossierBuilder:
    """Assembles a dossier from the canon (``CanonStore``), the navigation layer (``NavStore``), the catalogue pack
    (``CatalogueStore``) and an injected retrieval; ``cache`` (a dict owned by the caller) keeps the section index
    between calls (keyed by the NAV build)."""

    def __init__(self, canon: Any, nav: Any = None, catalogues: Any = None, retrieval: TopicRetrieval | None = None,
                 *, cache: dict[str, Any] | None = None) -> None:
        self.canon, self.nav, self.catalogues, self.retrieval = canon, nav, catalogues, retrieval
        self.cache = cache if cache is not None else {}

    # ---------------------------------------------------------------------------------------------------- driver
    def build(self, req: TopicRequest) -> Dossier:
        t0 = time.perf_counter()
        st = _State(req=req, stems=query_stems(req.query))
        if not st.stems:
            st.warn("QUERY_WITHOUT_CONTENT_WORDS", "the query has no content words of 3+ letters: title and catalogue "
                                                   "matching are off")
        nav_ok = self._nav_ready(st)
        cat_ok = self._catalogues_ready(st)
        try:
            st.core = self._core_sources(st, cat_ok)
        except Exception as exc:  # noqa: BLE001 - without tiers everything is shown as the rest of the corpus
            st.warn("CORE_TIER_UNAVAILABLE", f"core tier: {type(exc).__name__}: {str(exc)[:120]}")
        for name, fn, needs in (("concept", self._concept, nav_ok), ("retrieval", self._retrieve, True),
                                ("sections", self._sections, nav_ok), ("formulas", self._formulas, nav_ok),
                                ("visual", self._visual, True), ("topics", self._topics, nav_ok),
                                ("sources", self._sources, True), ("catalogues", self._catalogue_part, cat_ok)):
            if not needs:
                continue
            t1 = time.perf_counter()
            try:
                fn(st)
            except Exception as exc:  # noqa: BLE001 - one failing part never sinks the dossier
                LOG.warning("topic dossier part failed", extra={"vkm": {"stage": f"topic_{name}",
                                                                         "error": type(exc).__name__}})
                st.warn("PART_FAILED", f"{name}: {type(exc).__name__}: {str(exc)[:160]}")
            st.timings[name] = round((time.perf_counter() - t1) * 1000, 1)
        if not (st.units or nav_ok or cat_ok):
            from vkm_corpus.api.errors import ApiFailure

            raise ApiFailure("DEPENDENCY_UNAVAILABLE", "neither the navigation layer, the search nor the catalogue "
                                                       "pack is available: no dossier can be built", stage="topic",
                             tool="topic", hint="vkm-corpus nav … / catalogues publish; hybrid search on CORE")
        entries = self._entries(st)
        head = self._head(st)
        kept, dropped, text = fit_budget(head, entries, st.extra, req.budget_chars)
        record = self._record(st, kept, dropped, text)
        record["timings_ms"] = {**st.timings, "total": round((time.perf_counter() - t0) * 1000, 1)}
        return Dossier(record=record, warnings=st.warnings, projection=self._projection(st), markdown=text)

    def _nav_ready(self, st: _State) -> bool:
        if self.nav is None:
            st.warn("NAV_UNAVAILABLE", "the navigation layer is not configured: no sections, formulas or concepts")
            return False
        try:
            st.inputs["nav_snapshot_id"] = self.nav.snapshot_id()
        except Exception as exc:  # noqa: BLE001 - NavUnavailable or an unreadable build
            st.warn("NAV_UNAVAILABLE", f"the navigation layer is not available: {str(exc)[:160]}")
            return False
        canon_snapshot = self.canon.snapshot_id()
        st.inputs["canonical_snapshot_id"] = canon_snapshot
        if st.inputs["nav_snapshot_id"] != canon_snapshot:
            st.warn("NAV_SNAPSHOT_BEHIND", "the navigation layer was built from another canonical snapshot; ids are "
                                           "stable, counts may differ")
        return True

    def _catalogues_ready(self, st: _State) -> bool:
        if self.catalogues is None:
            st.warn("CATALOGUES_UNAVAILABLE", "no catalogue pack configured: the dossier has no processes, models, "
                                              "conflicts, causal links or gaps (vkm-corpus catalogues pack/publish)")
            return False
        try:
            meta = self.catalogues.meta()
        except Exception as exc:  # noqa: BLE001 - CataloguesUnavailable
            st.warn("CATALOGUES_UNAVAILABLE", f"no catalogue pack is published ({str(exc)[:120]}): the dossier has no "
                                              "processes, models, conflicts, causal links or gaps")
            return False
        st.inputs["catalogues"] = {k: meta.get(k) for k in ("pack_id", "git_commit", "content_sha256", "n_files",
                                                             "working_tree")}
        return True

    # ---------------------------------------------------------------------------------------------------- (a)
    def _core_sources(self, st: _State, cat_ok: bool) -> set[str]:
        """The VKM core tier: sources the evidence catalogues were built from and sources whose register scope is
        VKM/SKRU/regional — the mapped scope or the verbatim register value, since an unmapped value (a list of mines)
        maps to no scope (cached per canonical snapshot and catalogue pack)."""
        pack = (st.inputs.get("catalogues") or {}).get("pack_id") if cat_ok else None
        key = ("core", self.canon.snapshot_id(), pack)
        cached = self.cache.get("core")
        if cached and cached[0] == key:
            info = self.cache.get("core_info")
            st.inputs["core_tier"] = info[1] if info and info[0] == key else {"sources": len(cached[1])}
            return cached[1]
        core: set[str] = set()
        if cat_ok:
            for table, column in CORE_SOURCE_TABLES:
                for r in self.catalogues.rows(table, [column]):
                    core.update(re.findall(r"VKM-SRC-\d{3}", str(r.get(column) or "")))
        n_catalogued = len(core)
        for r in self._canon_rows("SELECT source_id, site_scope, site_scope_raw FROM sources", []):
            scopes = [*(r.get("site_scope") or []), r.get("site_scope_raw") or ""]
            if any(str(v).upper().startswith(CORE_SCOPE_PREFIXES) for v in scopes):
                core.add(r["source_id"])
        st.inputs["core_tier"] = {"sources": len(core), "catalogued": n_catalogued,
                                  "rule": "evidence-catalogue sources + register scope VKM/SKRU/regional (mapped or "
                                          "verbatim)"}
        self.cache["core"] = (key, core)
        self.cache["core_info"] = (key, st.inputs["core_tier"])
        return core

    def _formulations(self, st: _State) -> list[dict[str, Any]]:
        """≤ 5 formulations of the topic: the query, the caller's paraphrases (≤ 4; they come before the automatic
        ones), the query in the other language (term dictionary), the concept's synonyms (its lemma, SAME_AS, a
        narrower term) and the query widened by its two strongest concept neighbours."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(kind: str, text: str) -> None:
            key = " ".join(norm(text).split())
            if key and key not in seen and len(out) < MAX_FORMULATIONS:
                seen.add(key)
                out.append({"kind": kind, "text": " ".join(text.split())[:512]})

        add("query", st.req.query)
        for p in st.req.paraphrases[:MAX_PARAPHRASES]:
            add("paraphrase", p)
        translation = self._translation(st)
        if translation:
            add("translation", translation)
        c = st.concept or {}
        if c.get("match"):
            qwords = words_of(st.req.query)
            syn = [x.get("lemma") for x in [c["match"], *(c.get("same_as") or [])[:2], *(c.get("narrower") or [])[:1]]]
            syn = [s for s in dict.fromkeys(s for s in syn if s) if not words_of(s) <= qwords]
            if syn:
                add("synonyms", ", ".join(syn))
            near = [n.get("lemma") for n in (c.get("neighbours") or []) if n.get("lemma")
                    and not words_of(n["lemma"]) <= qwords][:2]
            if near:
                add("neighbours", f"{st.req.query} {' '.join(near)}")
        return out

    # ---------------------------------------------------------------- term dictionary (agent TR)
    def _translation(self, st: _State) -> str | None:
        """The query in the other language (RU ↔ EN) from the NAV term dictionary (``translate_query``: its terms by
        their trusted translations), when the request asks for it and the build has the dictionary; what happened is
        in ``inputs.translation`` (a build without it: ``not in this build``, no warning)."""
        if not st.req.translate or self.nav is None or not st.req.query.strip():
            return None
        from vkm_corpus.navigation import store as nav_store

        functions = getattr(self.nav, "_functions", {}) or {}
        if "translate_query" not in set(nav_store.QUERY_FUNCTIONS) | set(functions):
            st.inputs["translation"] = "not in this build"
            return None
        try:
            data = self.nav.run("translate_query", st.req.query)
        except Exception as exc:  # noqa: BLE001 - a NAV build without term_translations, no morphology …
            st.inputs["translation"] = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
            return None
        data = data if isinstance(data, dict) else {}
        text = data.get("translation")
        st.inputs["translation"] = {
            "status": "APPLIED" if text else "NOT_COVERED", "source_language": data.get("source_language"),
            "target_language": data.get("target_language"), "coverage": data.get("coverage"),
            "terms": [{k: t.get(k) for k in ("span", "translation", "score", "pair_id")}
                      for t in (data.get("terms") or [])[:8]]}
        return text
    # ---------------------------------------------------------------- end term dictionary (agent TR)

    def _retrieve(self, st: _State) -> None:
        """Every formulation over the core tier and over the corpus (or over the caller's sources), fused by RRF per
        tier: the ranked page lists ``CORE`` (≤ CORE_PAGES) and ``REST`` (≤ REST_PAGES)."""
        st.formulations = self._formulations(st)
        if self.retrieval is None:
            st.inputs["retrieval"] = {"mode": "NAV_ONLY", "reason": "no retrieval configured"}
            st.warn("RETRIEVAL_UNAVAILABLE", "hybrid search is not configured: sections come from titles only "
                                             "(NAV_ONLY)")
            return
        if st.req.source_ids:
            passes = [("FILTER", sorted(st.req.source_ids))]
        else:
            passes = ([("CORE", sorted(st.core))] if st.core else []) + [("ALL", None)]
        calls = [(p, ids, i, f) for i, f in enumerate(st.formulations) for p, ids in passes]
        results: dict[tuple[str, int], dict[str, Any]] = {}
        failures: list[str] = []
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, min(RETRIEVAL_WORKERS, len(calls)))) as pool:
            futures = {(p, i): pool.submit(self.retrieval.search, f["text"], source_ids=ids, limit=RETRIEVAL_UNITS)
                       for p, ids, i, f in calls}
            for key, fut in futures.items():
                try:
                    results[key] = fut.result() or {}
                except Exception as exc:  # noqa: BLE001 - ApiFailure (DEPENDENCY_UNAVAILABLE …) or a client error
                    failures.append(str(getattr(exc, "code", type(exc).__name__)))
        if not results:
            st.inputs["retrieval"] = {"mode": "NAV_ONLY", "reason": failures[0] if failures else "no answer"}
            st.warn("RETRIEVAL_UNAVAILABLE", f"hybrid search failed ({', '.join(sorted(set(failures)))}): NAV_ONLY "
                                             "dossier")
            return
        if failures:
            st.warn("RETRIEVAL_PARTIAL", f"{len(failures)} of {len(calls)} searches failed "
                                         f"({', '.join(sorted(set(failures)))}); the others are fused", len(failures))
        fused: dict[str, dict[str, Any]] = {}
        for (p, i), res in sorted(results.items()):
            ranked, seen_pages = [], set()
            for u in sorted((u for u in res.get("units") or [] if u.get("page_id")),
                            key=lambda u: (u.get("rank") or 10**6, u["page_id"])):
                if u["page_id"] not in seen_pages:        # a page counts once per ranked list (its best unit)
                    seen_pages.add(u["page_id"])
                    ranked.append(u)
            for rank, u in enumerate(ranked, 1):
                e = fused.setdefault(u["page_id"], {"rrf": 0.0, "best": None, "best_rank": 10**9, "hits": set()})
                e["rrf"] += 1.0 / (RRF_K + rank)
                e["hits"].add(st.formulations[i]["kind"])
                if rank < e["best_rank"]:
                    e["best"], e["best_rank"] = u, rank
        pages = {r["page_id"]: r for r in self._canon_rows(
            "SELECT page_id, source_id, page_index FROM pages WHERE page_id IN (SELECT unnest(?::VARCHAR[]))",
            [sorted(fused)])} if fused else {}
        stale = [p for p in fused if p not in pages]
        if stale:
            st.warn("STALE_PROJECTION", "retrieval pages not in the canonical snapshot were dropped", len(stale))
        by_tier: dict[str, list[tuple[str, dict[str, Any]]]] = {"CORE": [], "REST": []}
        for pid, e in fused.items():
            if pid in pages and (not st.req.source_ids or pages[pid]["source_id"] in st.req.source_ids):
                by_tier[st.tier(pages[pid]["source_id"])].append((pid, e))
        kept = []
        for tier, cap in (("CORE", CORE_PAGES), ("REST", REST_PAGES)):
            ordered = sorted(by_tier[tier], key=lambda x: (-x[1]["rrf"], x[0]))[:cap]
            for rank, (pid, e) in enumerate(ordered, 1):
                u = e["best"]
                kept.append({"page_id": pid, "source_id": pages[pid]["source_id"],
                             "page_index": pages[pid]["page_index"], "tier": tier, "rank": rank,
                             "unit_id": u.get("unit_id"), "object_ids": list(u.get("object_ids") or [])[:20],
                             "weight": round(e["rrf"], 6), "formulations": sorted(e["hits"])})
        st.units = kept
        first = next(iter(results.values()))
        st.inputs["retrieval"] = {
            "mode": "FULL", **{k: first.get(k) for k in ("engine", "build_id", "built_from_snapshot_id", "late",
                                                         "model_key")},
            "fusion": f"RRF k={RRF_K} over formulations × tiers", "searches": len(calls), "failed": len(failures),
            "passes": [p for p, _ids in passes], "pages": {t: sum(1 for u in kept if u["tier"] == t)
                                                           for t in ("CORE", "REST")}}
        for w in sorted({str(w) for res in results.values() for w in res.get("warnings") or []}):
            st.warn("SEARCH_WARNING", w)
        for u in kept:
            st.add_source(u["source_id"], u["weight"])

    # ---------------------------------------------------------------------------------------------------- (b)
    def _section_index(self) -> dict[str, Any]:
        meta = {}
        try:
            meta = self.nav.meta()
        except Exception:  # noqa: BLE001 - nav_meta is optional
            pass
        key = ("sections", self.nav.snapshot_id(), meta.get("packed_at"), meta.get("built_at"))
        cached = self.cache.get("sections")
        if cached and cached[0] == key:
            return cached[1]
        cols = {r["column_name"] for r in self.nav.query(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'sections'")}
        want = [c for c in ("section_id", "source_id", "work_id", "parent_section_id", "level", "ordinal", "numbering",
                            "title", "title_path", "page_start_id", "page_end_id", "page_start_index",
                            "page_end_index", "method", "key_terms") if c in cols]
        rows = self.nav.query(f"SELECT {', '.join(want)} FROM sections ORDER BY source_id, ordinal, section_id")
        index = {"rows": {r["section_id"]: r for r in rows},
                 "title_words": {r["section_id"]: words_of(" ".join(
                     [str(r.get("title") or "")] + list(r.get("key_terms") or []))) for r in rows},
                 "path_words": {r["section_id"]: words_of(r.get("title_path")) for r in rows}}
        self.cache["sections"] = (key, index)
        return index

    def _ancestors(self, index: dict[str, Any], sid: str) -> list[str]:
        out, seen = [], {sid}
        row = index["rows"].get(sid)
        while row and row.get("parent_section_id") and row["parent_section_id"] not in seen:
            seen.add(row["parent_section_id"])
            out.append(row["parent_section_id"])
            row = index["rows"].get(row["parent_section_id"])
        return out

    def _sections(self, st: _State) -> None:
        index = self._section_index()
        rows = index["rows"]
        cand: dict[str, dict[str, Any]] = {}

        def slot(sid: str) -> dict[str, Any]:
            return cand.setdefault(sid, {"retrieval": 0.0, "title": 0.0, "concept": 0.0, "units": []})

        # a: retrieval units → the deepest section of their page
        for u in st.units:
            try:
                found = self.nav.run("section_of_page", u["page_id"])
            except Exception:  # noqa: BLE001 - a page outside every section stays a bare unit
                found = None
            secs = (found or {}).get("sections") or []
            if secs and secs[0].get("section_id") in rows:
                c = slot(secs[0]["section_id"])
                c["retrieval"] += u["weight"]
                c["units"].append(u)
        # b: titles — each formulation but the neighbour-widened one (stems, idf over all titles; a partial match
        #    counts less: share ** 1.5), and the lemma-based NavStore.search_sections
        idf = idf_weights(st.stems, list(index["title_words"].values())) if st.stems else {}
        for stems, weight in self._phrasings(st):
            if not stems:
                continue
            f_idf = idf if stems == st.stems else idf_weights(stems, list(index["title_words"].values()))
            total = sum(f_idf.values())
            for sid, words in index["title_words"].items():
                found = hits(stems, words)
                if not found:
                    continue
                row = rows[sid]
                if st.req.source_ids and row["source_id"] not in st.req.source_ids:
                    continue
                path_only = hits(stems, index["path_words"][sid]) - found
                share = (sum(f_idf[s] for s in found) + 0.4 * sum(f_idf[s] for s in path_only)) / total
                score = weight * (min(1.0, share) ** 1.5 + 0.05 * len(found) / max(1, len(words)))
                slot(sid)["title"] = max(slot(sid)["title"], min(1.0, score))
        try:                                   # lemma matches of NavStore.search_sections (key terms included)
            nav_hits = self.nav.search_sections(st.req.query, source_id=(st.req.source_ids[0] if len(
                st.req.source_ids) == 1 else None), limit=30)
            for r in nav_hits:
                sid = r.get("section_id")
                if sid in rows and (not st.req.source_ids or r.get("source_id") in st.req.source_ids):
                    raw = r.get("score") or 0
                    score = raw / max(1, len(st.stems)) if isinstance(raw, int) else float(raw)  # int: word count
                    slot(sid)["title"] = max(slot(sid)["title"], 0.9 * min(1.0, score) ** 1.5)
        except Exception as exc:  # noqa: BLE001
            st.warn("SEARCH_SECTIONS_FAILED", f"search_sections: {type(exc).__name__}")
        if not cand:
            return
        # c: the query concept's own mentions (N3 term_mentions) strengthen sections found above — never add new
        #    ones (TOPIC_BENCHMARK_V1: concept-derived sections are noise; neighbours widen the query instead)
        for sid, weight in self._concept_sections(st).items():
            if sid in cand:
                cand[sid]["concept"] = max(cand[sid]["concept"], weight)
        reviewed = self._reviewed_sources()
        r_max = max(c["retrieval"] for c in cand.values()) or 1.0
        for sid, c in cand.items():
            signals = [k for k in ("retrieval", "title", "concept") if c[k] > 0]
            c["signals"] = signals
            c["score"] = (1.0 * c["retrieval"] / r_max + 0.9 * c["title"] + 0.3 * c["concept"]
                          + (0.1 if len(signals) > 1 else 0.0) + 0.01 * min(int(rows[sid].get("level") or 1), 4)
                          + (0.05 if rows[sid]["source_id"] in reviewed else 0.0))
        # d: the text itself — do the query words meet on one page of the section? (the best candidates without
        #    retrieval units; also gives their best page and snippet)
        pool = sorted(cand, key=lambda s: (-cand[s]["score"], s))[: 4 * st.req.max_sections + 8]
        texts = self._section_texts(st, [rows[s] for s in pool if not cand[s]["units"]], idf)
        for sid in cand:
            share = texts[sid]["share"] if sid in texts else (1.0 if cand[sid]["units"] else 0.0)
            cand[sid]["content"] = share
            cand[sid]["score"] = round(cand[sid]["score"] + 0.4 * share, 6)
        order = sorted(cand, key=lambda s: (-cand[s]["score"], s))
        caps = {"CORE": st.req.max_sections, "REST": max(3, st.req.max_sections // 2)}
        chosen: list[str] = []
        per_tier: dict[str, int] = {"CORE": 0, "REST": 0}
        per_source: dict[str, int] = {}
        skipped: dict[str, int] = {"CORE": 0, "REST": 0}
        for sid in order:
            fam = {sid, *self._ancestors(index, sid)}
            owner = next((c for c in chosen if c in fam or sid in self._ancestors(index, c)), None)
            if owner is not None:                     # one section per branch: the best-scored, the rest «related»
                cand[owner].setdefault("related", []).append(sid)
                continue
            src = rows[sid]["source_id"]
            tier = st.tier(src)
            if per_source.get(src, 0) >= PER_SOURCE_SECTIONS or per_tier[tier] >= caps[tier]:
                skipped[tier] += 1
                continue
            per_source[src] = per_source.get(src, 0) + 1
            per_tier[tier] += 1
            chosen.append(sid)
        st.extra["sections_core"], st.extra["sections_rest"] = skipped["CORE"], skipped["REST"]
        rest = [rows[s] for s in chosen if not cand[s]["units"] and s not in texts]
        if rest:
            texts.update(self._section_texts(st, rest, idf))
        unit_texts = self._unit_texts([u for s in chosen for u in cand[s]["units"][:2]])
        rank_in = {"CORE": 0, "REST": 0}
        for sid in chosen:
            row, c = rows[sid], cand[sid]
            tier = st.tier(row["source_id"])
            rank_in[tier] += 1
            units_out = []
            for u in sorted(c["units"], key=lambda u: (u["tier"] != tier, u["rank"]))[:2]:
                units_out.append({"page_id": u["page_id"], "page_index": u.get("page_index"),
                                  "unit_id": u.get("unit_id"), "object_ids": list(u.get("object_ids") or [])[:5],
                                  "retrieval_rank": u["rank"], "formulations": u.get("formulations"),
                                  "snippet": self._best_snippet(u, unit_texts, st.stems, self._all_stems(st))})
            if not units_out and texts.get(sid, {}).get("page_id"):
                units_out.append({k: v for k, v in texts[sid].items() if k != "share"})
            st.sections.append({
                "rank": rank_in[tier], "tier": tier, "section_id": sid, "source_id": row["source_id"],
                "work_id": row.get("work_id"), "level": row.get("level"), "numbering": row.get("numbering"),
                "title": row.get("title"), "title_path": row.get("title_path"), "method": row.get("method"),
                "pages": {"first_id": row.get("page_start_id"), "last_id": row.get("page_end_id"),
                          "first_index": row.get("page_start_index"), "last_index": row.get("page_end_index")},
                "score": c["score"], "signals": c["signals"], "content_share": round(c.get("content", 0.0), 3),
                "units": units_out, "related_sections": sorted(c.get("related", []))[:5],
                "review_status": NAV_STATUS})
            st.add_source(row["source_id"], c["score"])

    def _concept_sections(self, st: _State) -> dict[str, float]:
        """Section → salience (0…1) of the matched concept: all its N3 mentions by tf-idf when the build has the
        ``term_mentions`` table, else explore_concept's top units."""
        c = st.concept or {}
        term_id = (c.get("match") or {}).get("term_id")
        rows: list[dict[str, Any]] = []
        if term_id:
            try:
                rows = self.nav.query("SELECT section_id, tfidf FROM term_mentions WHERE term_id = ? AND section_id "
                                      "IS NOT NULL ORDER BY tfidf DESC, section_id LIMIT 80", [term_id])
            except Exception:  # noqa: BLE001 - a build without the concept tables, or a stubbed concept
                rows = []
        if not rows:
            rows = [u for u in c.get("top_units") or [] if u.get("section_id")]
        best = max((float(r.get("tfidf") or 0) for r in rows), default=0.0)
        out: dict[str, float] = {}
        for r in rows:
            if best > 0:
                out[r["section_id"]] = max(out.get(r["section_id"], 0.0), float(r.get("tfidf") or 0) / best)
        return out

    @staticmethod
    def _phrasings(st: _State) -> list[tuple[list[str], float]]:
        """(stems, weight) of the query (1.0), each paraphrase and the translation (0.9) and the synonyms (0.8) — the
        formulations whose words may match titles and catalogue rows on their own (the neighbour-widened one only
        feeds retrieval)."""
        weights = {"query": 1.0, "paraphrase": 0.9, "translation": 0.9, "synonyms": 0.8}
        out = [(query_stems(f["text"]), weights[f["kind"]]) for f in st.formulations if f["kind"] in weights]
        return [(stems, w) for stems, w in out if stems] or [(st.stems, 1.0)]

    @staticmethod
    def _all_stems(st: _State) -> list[str]:
        """Stems of the query, the paraphrases, the translation and the synonyms (not the neighbour-widened
        formulation)."""
        out = list(st.stems)
        for f in st.formulations:
            if f["kind"] in ("paraphrase", "translation", "synonyms"):
                out += [s for s in query_stems(f["text"]) if s not in out]
        return out

    def _near_pages(self, st: _State) -> dict[str, float]:
        """Hit pages (the best fused pages of each tier and the pages shown with the sections) and their ±1
        neighbours → an order key (lower = nearer the top)."""
        if st.near is not None:
            return st.near
        order: dict[str, float] = {}
        hits_ = [u["page_id"] for u in st.units if u["tier"] == "CORE"][:20] + \
            [u["page_id"] for u in st.units if u["tier"] == "REST"][:10] + \
            [u["page_id"] for s in st.sections for u in s["units"]]
        for i, pid in enumerate(hits_):
            order[pid] = min(order.get(pid, 1e9), float(i))
            m = re.match(r"^(.*:[prs])(\d{4})$", pid)
            if m:
                for d in (-1, 1):
                    n = int(m.group(2)) + d
                    if n >= 0:
                        near = f"{m.group(1)}{n:04d}"
                        order[near] = min(order.get(near, 1e9), i + 0.5)
        st.near = order
        return order

    def _visual(self, st: _State) -> None:
        """Figures and tables on or next to the hit pages whose label, caption or table header carries the query
        words (TOPIC_BENCHMARK_V1: 27 % of the missed evidence pages hold it in a figure, a table or a formula)."""
        near = self._near_pages(st)
        stems = self._all_stems(st)
        if not near or not stems:
            return
        ids = [sorted(near)]
        rows = [("FIGURE", r) for r in self._canon_rows(
            "SELECT object_id, page_id, source_id, figure_label AS label, caption_normalized AS caption, NULL AS head "
            "FROM figures WHERE page_id IN (SELECT unnest(?::VARCHAR[]))", ids)]
        rows += [("TABLE", r) for r in self._canon_rows(
            "SELECT object_id, page_id, source_id, table_label AS label, caption_normalized AS caption, "
            "left(normalized_text, 300) AS head FROM tables WHERE page_id IN (SELECT unnest(?::VARCHAR[]))", ids)]
        scored = []
        for kind, r in rows:
            if st.req.source_ids and r["source_id"] not in st.req.source_ids:
                continue
            found = hits(stems, words_of(" ".join(str(x) for x in (r["label"], r["caption"], r["head"]) if x)))
            if not found:
                continue
            tier = st.tier(r["source_id"])
            score = len(found) / len(stems) + (0.1 if tier == "CORE" else 0.0) - 0.002 * near[r["page_id"]]
            scored.append((round(score, 6), {
                "object_id": r["object_id"], "kind": kind, "page_id": r["page_id"], "source_id": r["source_id"],
                "tier": tier, "label": short(r["label"], 30) or None,
                "caption": short(r["caption"] or r["head"], 110) or None, "matched": len(found),
                "review_status": NAV_STATUS}))
        scored.sort(key=lambda x: (-x[0], x[1]["object_id"]))
        st.extra["visual"] = max(0, len(scored) - 8)
        st.visual = [{"rank": i, **v} for i, (_s, v) in enumerate(scored[:8], 1)]

    def _reviewed_sources(self) -> set[str]:
        """Sources the evidence catalogues were built from (SOURCE_COVERAGE_MASTER): a mild ranking prior."""
        if self.catalogues is None:
            return set()
        try:
            return {r["source_id"] for r in self.catalogues.rows("source_coverage_master", ["source_id"])
                    if r.get("source_id")}
        except Exception:  # noqa: BLE001 - the pack is optional
            return set()

    def _unit_texts(self, units: list[dict[str, Any]]) -> dict[str, str]:
        ids = sorted({o for u in units for o in (u.get("object_ids") or [])[:5]})
        pages = sorted({u["page_id"] for u in units})
        out: dict[str, str] = {}
        if ids:
            out.update({r["object_id"]: r["normalized_text"] or "" for r in self._canon_rows(
                "SELECT object_id, normalized_text FROM blocks WHERE object_id IN (SELECT unnest(?::VARCHAR[]))",
                [ids])})
        if pages:
            out.update({r["page_id"]: r["normalized_text"] or "" for r in self._canon_rows(
                "SELECT page_id, normalized_text FROM pages WHERE page_id IN (SELECT unnest(?::VARCHAR[]))", [pages])})
        return out

    @staticmethod
    def _best_snippet(unit: dict[str, Any], texts: dict[str, str], stems: list[str],
                      more: list[str] | None = None) -> str | None:
        """A snippet around the query words in the unit's blocks, then its page; the paraphrase and synonym words
        when the query's own words are not there (a unit found by another formulation)."""
        for words in (stems, more or []):
            for oid in list(unit.get("object_ids") or [])[:5] + [unit["page_id"]]:
                s = snippet(texts.get(oid), words) if words else None
                if s:
                    return s
        return None

    def _section_texts(self, st: _State, secs: list[dict[str, Any]],
                       idf: dict[str, float]) -> dict[str, dict[str, Any]]:
        """For each section: the page (among its first ``MAX_SCAN_PAGES``) where most of the query words meet
        (idf-weighted ``share`` 0…1, then the earliest page) and a snippet there — the best unit without retrieval."""
        if not secs or not st.stems:
            return {}
        spans: dict[str, tuple[str, int, int]] = {}
        for s in secs:
            first = s.get("page_start_index")
            if first is not None:
                spans[s["section_id"]] = (s["source_id"], int(first),
                                          min(int(s.get("page_end_index") or first), int(first) + MAX_SCAN_PAGES - 1))
        if not spans:
            return {}
        by_source: dict[str, list[tuple[int, int]]] = {}
        for src, a, b in spans.values():
            by_source.setdefault(src, []).append((a, b))
        clauses, params = [], []
        for src, ranges in sorted(by_source.items()):
            for a, b in sorted(set(ranges)):
                clauses.append("(source_id = ? AND page_index BETWEEN ? AND ?)")
                params += [src, a, b]
        pages: dict[tuple[str, int], dict[str, Any]] = {}
        for i in range(0, len(clauses), 200):
            for p in self._canon_rows(f"SELECT page_id, source_id, page_index, normalized_text FROM pages WHERE "
                                      f"{' OR '.join(clauses[i:i + 200])}", params[3 * i:3 * (i + 200)]):
                pages[(p["source_id"], p["page_index"])] = p
        total = sum(idf.get(s, 1.0) for s in st.stems) or 1.0
        found_cache: dict[str, set[str]] = {}
        out: dict[str, dict[str, Any]] = {}
        for sid, (src, a, b) in spans.items():
            best = None
            for idx in range(a, b + 1):
                p = pages.get((src, idx))
                if p is None:
                    continue
                if p["page_id"] not in found_cache:
                    found_cache[p["page_id"]] = hits(st.stems, words_of(p["normalized_text"]))
                found = found_cache[p["page_id"]]
                share = sum(idf.get(s, 1.0) for s in found) / total
                if found and (best is None or share > best[0]):
                    best = (share, p)
            if best:
                share, p = best
                out[sid] = {"page_id": p["page_id"], "page_index": p["page_index"], "unit_id": None, "object_ids": [],
                            "retrieval_rank": None, "snippet": snippet(p["normalized_text"], st.stems),
                            "share": round(share, 4)}
        return out

    # ---------------------------------------------------------------------------------------------------- (d)
    def _concept(self, st: _State) -> None:
        if not st.req.query.strip():
            return
        try:
            data = self.nav.run("explore_concept", st.req.query, limit=12)
        except Exception as exc:  # noqa: BLE001 - NavUnavailable, no concept tables, no morphology
            st.warn("CONCEPTS_UNAVAILABLE", f"explore_concept: {type(exc).__name__}: {str(exc)[:120]}")
            return
        if isinstance(data, dict) and data.get("match"):
            st.concept = data
            for i, s in enumerate(data.get("top_sources") or []):
                st.add_source(s.get("source_id"), 0.3 / (1 + i))

    def _topics(self, st: _State) -> None:
        """Topics of agent T's layer (``find_topics``; the detail through ``topic`` / ``get_topic``) when the build
        has them: ids, label terms, sizes, central and member sections, sources — no text."""
        from vkm_corpus.navigation import store as nav_store

        functions = getattr(self.nav, "_functions", {}) or {}
        known = set(nav_store.QUERY_FUNCTIONS) | set(functions)
        if "find_topics" not in known:
            st.inputs["topics"] = "not in this build"
            return
        try:
            data = self.nav.run("find_topics", st.req.query, limit=5)
        except Exception as exc:  # noqa: BLE001 - agent T's layer not built / not published
            st.warn("TOPICS_UNAVAILABLE", f"find_topics: {type(exc).__name__}: {str(exc)[:120]}")
            return
        items = data.get("topics") if isinstance(data, dict) else data
        for t in list(items or [])[:5]:
            if isinstance(t, dict):
                st.topics.append({k: t.get(k) for k in ("topic_id", "id", "level", "label", "label_terms", "terms",
                                                        "n_sections", "n_sources", "central_section_ids", "score",
                                                        "coherence") if t.get(k) is not None})
        detail_fn = next((n for n in ("topic", "get_topic") if n in known), None)
        for t in st.topics[:2] if detail_fn else []:            # the two best topics in detail (ids, not text)
            tid = t.get("topic_id") or t.get("id")
            try:
                detail = self.nav.run(detail_fn, tid) if tid else None
            except Exception as exc:  # noqa: BLE001
                st.warn("TOPICS_UNAVAILABLE", f"{detail_fn}: {type(exc).__name__}: {str(exc)[:120]}")
                break
            if isinstance(detail, dict):
                sections = [s.get("section_id") if isinstance(s, dict) else s
                            for s in [*(detail.get("central_sections") or []), *(detail.get("members") or []),
                                      *(detail.get("sections") or [])]]
                terms = detail.get("label_terms") or detail.get("terms") or []
                t["detail"] = {"section_ids": list(dict.fromkeys(s for s in sections if isinstance(s, str)))[:8],
                               "source_ids": [s.get("source_id") if isinstance(s, dict) else s
                                              for s in detail.get("sources") or []][:8],
                               "terms": [x.get("lemma") if isinstance(x, dict) else x for x in terms][:10]}

    # ---------------------------------------------------------------------------------------------------- (c)
    def _formulas(self, st: _State) -> None:
        req = st.req
        if req.max_formulas <= 0:
            return
        score: dict[str, float] = {}
        why: dict[str, list[str]] = {}
        defs: dict[str, list[dict[str, Any]]] = {}
        n_core = sum(1 for s in st.sections if s["tier"] == "CORE")      # the core tier's sections come first
        ranges = [(s["source_id"], s["pages"]["first_index"], s["pages"]["last_index"],
                   s["rank"] + (n_core if s["tier"] == "REST" else 0))
                  for s in st.sections if s["pages"]["first_index"] is not None]
        ctx: dict[str, dict[str, Any]] = {}
        cols = ("formula_id, source_id, page_id, page_index, kind, equation_number, section_id, n_defined_symbols, "
                "n_refs_in, n_parameters")
        near = self._near_pages(st)
        if ranges:
            clause = " OR ".join("(source_id = ? AND page_index BETWEEN ? AND ?)" for _ in ranges)
            params = [x for s, a, b, _r in ranges for x in (s, a, b if b is not None else a)]
            for r in self.nav.query(f"SELECT {cols} FROM formula_context WHERE kind = 'DISPLAY' AND "
                                    f"(equation_number IS NOT NULL OR n_defined_symbols > 0 OR n_parameters > 0) AND "
                                    f"({clause})", params):
                rank, span = min((rk, (b if b is not None else a) - a + 1) for s, a, b, rk in ranges
                                 if s == r["source_id"] and a <= r["page_index"] <= (b if b is not None else a))
                # a long section (a whole chapter) lends its rank only to the formulas near its hit pages
                local = r["page_id"] in near
                focus = 1.0 if local or span <= FORMULA_FOCUS_PAGES else FORMULA_FOCUS_PAGES / span
                ctx[r["formula_id"]] = r
                score[r["formula_id"]] = focus * (0.8 / rank + 0.3 * min(int(r["n_defined_symbols"] or 0), 3) / 3
                                                  + 0.2 * (r["equation_number"] is not None)
                                                  + 0.1 * min(int(r["n_refs_in"] or 0), 3) / 3
                                                  + 0.1 * (int(r["n_parameters"] or 0) > 0)) + 0.2 * local
                why.setdefault(r["formula_id"], []).append(f"section#{rank}" + (":near" if local else ""))
        phrases = [(req.query, 1.2)]
        if len(st.stems) > 1:                   # single words only help when the whole phrase is rare
            phrases += [(w, 0.4) for w in _WORD.findall(norm(req.query)) if len(w) >= 6 and w not in STOP]
        by_phrase = 0
        for n, (phrase, weight) in enumerate(phrases):
            if n and by_phrase >= req.max_formulas:
                break
            try:
                found = self.nav.run("find_formulas", concept=phrase, limit=80)
            except Exception as exc:  # noqa: BLE001
                st.warn("FIND_FORMULAS_FAILED", f"find_formulas: {type(exc).__name__}")
                break
            stems = query_stems(phrase)
            for r in found or []:
                fid = r.get("formula_id")
                if not fid or (req.source_ids and r.get("source_id") not in req.source_ids):
                    continue
                if f"def:{phrase}" not in why.get(fid, []):
                    words = _WORD.findall(norm(r.get("definition")))
                    adjacent = len(stems) > 1 and _adjacent(stems, words)
                    w = weight * (1.5 if adjacent else 1.0) * (0.7 if len(words) > 10 else 1.0)
                    score[fid] = score.get(fid, 0.0) + w
                    why.setdefault(fid, []).append(f"def:{phrase}")
                    by_phrase += n == 0
                defs.setdefault(fid, []).append(r)
        # «где…» definitions carrying the query words on or next to the hit pages (visual evidence)
        if near and st.stems:
            for r in self.nav.query(
                    "SELECT c.formula_id, c.source_id, c.page_id, c.page_index, c.kind, c.equation_number, "
                    "c.section_id, c.n_defined_symbols, c.n_refs_in, c.n_parameters, s.symbol, s.definition, s.unit "
                    "FROM formula_context c JOIN formula_symbols s ON s.formula_id = c.formula_id "
                    "WHERE c.page_id IN (SELECT unnest(?::VARCHAR[])) AND s.definition IS NOT NULL "
                    "ORDER BY c.formula_id, s.symbol", [sorted(near)]):
                if req.source_ids and r["source_id"] not in req.source_ids:
                    continue
                found = hits(st.stems, words_of(r["definition"]))
                if not found:
                    continue
                fid = r["formula_id"]
                ctx.setdefault(fid, {k: r[k] for k in ("formula_id", "source_id", "page_id", "page_index", "kind",
                                                       "equation_number", "section_id", "n_defined_symbols",
                                                       "n_refs_in", "n_parameters")})
                if "near-hit" not in why.get(fid, []):
                    score[fid] = score.get(fid, 0.0) + 0.6 * len(found) / len(st.stems)
                    why.setdefault(fid, []).append("near-hit")
                defs.setdefault(fid, []).append({"symbol": r["symbol"], "definition": r["definition"],
                                                 "unit": r["unit"]})
        if not score:
            return
        order = sorted(score, key=lambda f: (-score[f], f))
        st.extra["formulas"] = max(0, len(order) - req.max_formulas)
        chosen = order[: req.max_formulas]
        missing = [f for f in chosen if f not in ctx]
        if missing:
            for r in self.nav.query(f"SELECT {cols} FROM formula_context WHERE formula_id IN "
                                    f"(SELECT unnest(?::VARCHAR[]))", [missing]):
                ctx[r["formula_id"]] = r
        symbols: dict[str, list[dict[str, Any]]] = {}
        for r in self.nav.query("SELECT formula_id, symbol, definition, unit FROM formula_symbols WHERE formula_id IN "
                                "(SELECT unnest(?::VARCHAR[])) AND definition IS NOT NULL ORDER BY formula_id, "
                                "in_formula DESC, symbol", [chosen]):
            symbols.setdefault(r["formula_id"], []).append(r)
        params_of: dict[str, list[dict[str, Any]]] = {}
        for r in self.nav.query("SELECT formula_id, symbol, value_text, unit, context_kind FROM formula_parameters "
                                "WHERE formula_id IN (SELECT unnest(?::VARCHAR[])) ORDER BY formula_id, context_kind, "
                                "symbol", [chosen]):
            params_of.setdefault(r["formula_id"], []).append(r)
        latex = {r["object_id"]: r["latex"] for r in self._canon_rows(
            "SELECT object_id, coalesce(normalized_latex, CASE WHEN raw_format <> 'IMAGE_ONLY' THEN raw_output END) "
            "AS latex FROM formulas WHERE object_id IN (SELECT unnest(?::VARCHAR[]))", [chosen])}
        for rank, fid in enumerate(chosen, 1):
            c = ctx.get(fid, {})
            syms = symbols.get(fid) or [{"symbol": d.get("symbol"), "definition": d.get("definition"),
                                         "unit": d.get("unit")} for d in defs.get(fid, [])]
            seen, sym_out = set(), []
            for s in syms:
                if s.get("symbol") in seen:
                    continue
                seen.add(s.get("symbol"))
                sym_out.append({"symbol": s.get("symbol"), "definition": short(s.get("definition"), 80),
                                "unit": s.get("unit")})
            st.formulas.append({
                "rank": rank, "formula_id": fid, "source_id": c.get("source_id") or fid.split(":")[0],
                "page_id": c.get("page_id"), "page_index": c.get("page_index"), "section_id": c.get("section_id"),
                "equation_number": c.get("equation_number"), "latex": short(latex.get(fid), 100) or None,
                "symbols": sym_out[:5], "parameters": [{"symbol": p["symbol"], "value_text": p["value_text"],
                                                        "unit": p["unit"]} for p in params_of.get(fid, [])[:3]],
                "n_refs_in": c.get("n_refs_in"), "match": why.get(fid, []), "score": round(score[fid], 4),
                "review_status": NAV_STATUS})
            st.add_source(c.get("source_id") or fid.split(":")[0], 0.2 * score[fid])

    # ---------------------------------------------------------------------------------------------------- (e)
    def _sources(self, st: _State) -> None:
        req = st.req
        for sid in req.source_ids:
            st.source_score.setdefault(sid, 0.0)
        if not st.source_score:
            return
        order = sorted(st.source_score, key=lambda s: (-st.source_score[s], s))
        caps = {"CORE": req.max_sources, "REST": max(3, req.max_sources // 2)}
        chosen: list[str] = []
        for tier in ("CORE", "REST"):
            mine = [s for s in order if st.tier(s) == tier]
            st.extra[f"sources_{tier.lower()}"] = max(0, len(mine) - caps[tier])
            chosen += mine[: caps[tier]]
        rows = self._canon_rows(
            "SELECT s.source_id, s.source_class_raw, s.site_scope_raw, s.site_scope, s.lifecycle_status, ws.work_id, "
            "w.title, w.authors_display, w.publication_year, w.work_type FROM sources s LEFT JOIN work_sources ws ON "
            "ws.source_id = s.source_id AND ws.is_primary LEFT JOIN works_all w ON w.work_id = ws.work_id WHERE "
            "s.source_id IN (SELECT unnest(?::VARCHAR[])) ORDER BY s.source_id, ws.work_id", [chosen])
        by_source: dict[str, dict[str, Any]] = {}
        for r in rows:
            by_source.setdefault(r["source_id"], r)
        coverage = {}
        if self.catalogues is not None:
            try:
                coverage = {r["source_id"]: r for r in self.catalogues.rows(
                    "source_coverage_master", ["source_id", "document_type", "geographic_scope", "mine_attribution"])}
            except Exception:  # noqa: BLE001 - the pack is optional here
                coverage = {}
        cites = self._cites()
        if cites is None:
            st.warn("CITES_PENDING", f"CITES is still being read (older DuckDB file without materialised "
                                     f"bibliography links: over {CITES_WAIT_S:g} s); repeat the call for citations")
        cited_by: dict[str, set[str]] = {}
        for r in cites or []:
            cited_by.setdefault(r["cited_work_id"], set()).add(r["citing_work_id"])
        n_sec: dict[str, int] = {}
        n_for: dict[str, int] = {}
        for s in st.sections:
            n_sec[s["source_id"]] = n_sec.get(s["source_id"], 0) + 1
        for f in st.formulas:
            n_for[f["source_id"]] = n_for.get(f["source_id"], 0) + 1
        rank_in = {"CORE": 0, "REST": 0}
        for sid in chosen:
            r = by_source.get(sid)
            if r is None:
                st.warn("SOURCE_NOT_IN_CANON", f"{sid} is not a registered source of the snapshot")
                continue
            cov = coverage.get(sid) or {}
            tier = st.tier(sid)
            rank_in[tier] += 1
            st.sources.append({
                "rank": rank_in[tier], "tier": tier, "source_id": sid, "work_id": r.get("work_id"),
                "title": short(r.get("title"), 140),
                "authors": short(r.get("authors_display"), 120), "year": r.get("publication_year"),
                "work_type": r.get("work_type"), "source_class": r.get("source_class_raw"),
                "site_scope_raw": r.get("site_scope_raw"), "site_scope": list(r.get("site_scope") or []),
                "scope_basis": "SOURCE_REGISTER (the area of the source, not established for its objects)",
                "lifecycle_status": r.get("lifecycle_status"),
                "evidence_catalogue": {k: cov.get(k) for k in ("document_type", "geographic_scope", "mine_attribution")
                                       if cov.get(k)} or None,
                "n_sections": n_sec.get(sid, 0), "n_formulas": n_for.get(sid, 0),
                "cited_by_works": len(cited_by.get(r.get("work_id"), ())) if r.get("work_id") and cites is not None
                else None, "score": round(st.source_score[sid], 4)})
        if cites is not None:
            self._citations(st, [s["work_id"] for s in st.sources if s.get("work_id")], cites)

    def _cites(self) -> list[dict[str, Any]] | None:
        """All CITES rows of the snapshot (a few hundred), read once per canonical snapshot in a background thread:
        the view aggregates the bibliography links, and a DuckDB file built before the links were materialised
        computes them per query (tens of seconds). The call that starts the read waits ``CITES_WAIT_S`` at most, the
        calls during the read do not wait; None = still reading (a later call has them)."""
        key = ("cites", self.canon.snapshot_id())
        cached = self.cache.get("cites")
        if cached and cached[0] == key:
            return cached[1]
        pending = self.cache.get("cites_pending")
        started = False
        if pending is None or pending[0] != key or pending[1].is_set():     # none, another snapshot, or failed
            started = True
            done = threading.Event()

            def read() -> None:
                try:
                    rows = self._canon_rows("SELECT citing_work_id, cited_work_id, n_citing_entries FROM cites "
                                            "ORDER BY citing_work_id, cited_work_id", [])
                    self.cache["cites"] = (key, rows)
                except Exception as exc:  # noqa: BLE001 - reported by the next call's CITES_PENDING
                    LOG.warning("CITES read failed", extra={"vkm": {"stage": "topic_cites",
                                                                     "error": type(exc).__name__}})
                finally:
                    done.set()

            pending = (key, done)
            self.cache["cites_pending"] = pending
            threading.Thread(target=read, name="vkm-topic-cites", daemon=True).start()
        pending[1].wait(CITES_WAIT_S if started else 0)
        cached = self.cache.get("cites")
        return cached[1] if cached and cached[0] == key else None

    def _citations(self, st: _State, works: list[str], cites: list[dict[str, Any]]) -> None:
        if not works:
            return
        in_set = set(works)
        rows = [r for r in cites if r["citing_work_id"] in in_set]
        within = [{"citing_work_id": r["citing_work_id"], "cited_work_id": r["cited_work_id"],
                   "n_entries": int(r["n_citing_entries"] or 0)} for r in rows if r["cited_work_id"] in in_set]
        counts: dict[str, set[str]] = {}
        for r in rows:
            counts.setdefault(r["cited_work_id"], set()).add(r["citing_work_id"])
        top = sorted(counts, key=lambda w: (-len(counts[w]), w))[:5]
        titles = {r["work_id"]: r for r in self._canon_rows(
            "SELECT work_id, title, authors_display, publication_year FROM works_all WHERE work_id IN "
            "(SELECT unnest(?::VARCHAR[]))", [top])} if top else {}
        st.citations = {
            "within_set": within[:12],
            "most_cited": [{"work_id": w, "cited_by_set_works": len(counts[w]), "in_set": w in in_set,
                            "title": short((titles.get(w) or {}).get("title"), 90),
                            "authors": short((titles.get(w) or {}).get("authors_display"), 60),
                            "year": (titles.get(w) or {}).get("publication_year")} for w in top],
            "note": "a citation is not agreement; CITES = resolved bibliography links (canon)"}

    # ---------------------------------------------------------------------------------------------------- (f, g)
    def _catalogue_part(self, st: _State) -> None:
        cat = self.catalogues
        P = [dict(p, group_ru=GROUP_RU.get(p.get("group") or "", ""),
                  domain_ru=DOMAIN_RU.get(p.get("domain") or "", ""))
             for p in cat.rows("physics_coverage_and_execution_matrix")]
        L = cat.rows("process_evidence_links")
        found_sources = {s["source_id"] for s in st.sources} | {s["source_id"] for s in st.sections[:5]}
        links_by: dict[str, list[dict[str, Any]]] = {}
        for link in L:
            links_by.setdefault(link.get("process_id") or "", []).append(link)
        docs = [_catalogue_words(p, PROCESS_FIELDS) for p in P]
        # every phrasing (query, paraphrases, synonyms) scores a process on its own words; the best weighted score
        # counts, and the name bonus stays with the query's own words
        phrasings = [(stems, w) for stems, w in self._phrasings(st) if stems]
        all_stems = sorted({s for stems, _w in phrasings for s in stems})
        idf = idf_weights(all_stems, [frozenset().union(*(w for w, _x in d)) for d in docs]) if all_stems else {}
        hit_pages = set(self._near_pages(st))          # the pages the dossier found (and their ±1 neighbours)
        scored = []
        for p, fields in zip(P, docs):
            ws, name = _word_score(st.stems, idf, fields, 3.0) if st.stems else (0.0, set())
            for stems, weight in phrasings:
                if stems != st.stems:
                    ws = max(ws, weight * _word_score(stems, idf, fields, 3.0)[0])
            plinks = links_by.get(p["process_id"], [])
            linked_sources = set(split_ids(p.get("source_ids"))) | {x.get("source_id") for x in plinks}
            overlap = len(linked_sources & found_sources)
            # the process's evidence pages among the pages found: the search and the catalogue point at one place
            page_hits = len({_evidence_page(x) for x in plinks} & hit_pages)
            if not (ws >= 0.25 or name or ((overlap >= 3 or page_hits >= 2) and ws > 0)):
                continue
            scored.append((round(ws + 0.08 * min(overlap, 5) + 0.12 * min(page_hits, 5), 6), p, overlap,
                           sorted(name), page_hits))
        scored.sort(key=lambda x: (-x[0], x[1]["process_id"]))
        st.extra["processes"] = max(0, len(scored) - st.req.max_processes)
        mech = cat.rows("mechanics_evidence_catalog", ["vn_ids", "variable", "category"])
        rheo = cat.rows("rheology_evidence_catalog", ["vn_ids"])
        vn_vars: dict[str, set[str]] = {}
        for m in mech:
            for vn in split_ids(m.get("vn_ids")):
                vn_vars.setdefault(vn, set()).update(x for x in (m.get("variable"), m.get("category")) if x)
        for m in rheo:
            for vn in split_ids(m.get("vn_ids")):
                vn_vars.setdefault(vn, set()).add("rheology_param")
        for rank, (score, p, overlap, name, page_hits) in enumerate(scored[: st.req.max_processes], 1):
            links = sorted(links_by.get(p["process_id"], []), key=lambda x: (
                (x.get("role") or "") != "KEY", (x.get("scope") or "") not in SKRU1_SCOPES,
                CONFIDENCE_ORDER.get(x.get("confidence") or "", 3), x.get("vn_id") or ""))
            if st.req.source_ids:
                links = [x for x in links if x.get("source_id") in st.req.source_ids] + \
                        [x for x in links if x.get("source_id") not in st.req.source_ids]
            st.processes.append({
                "rank": rank, "process_id": p["process_id"], "process": p.get("process"), "group": p.get("group"),
                "domain": p.get("domain"), "readiness": p.get("readiness"), "status": p.get("status"),
                "scope": p.get("scope"), "confidence": p.get("confidence"),
                "best_evidence_scope": p.get("best_evidence_scope"),
                "governing_equations": short(p.get("governing_equations"), 220),
                "required_parameters": short(p.get("required_parameters"), 220),
                "data_gaps": short(p.get("data_gaps"), 220) or None,
                "model_ids": split_ids(p.get("math_model_ids_FORMULAS_draft")),
                "evidence": [{k: x.get(k) for k in ("vn_id", "kind", "source_id", "pdf_page", "status", "evidence_type",
                                                    "scope", "scale", "confidence", "role")} for x in links[:8]],
                "n_evidence": len(links), "evidence_by_status": _count(links, "status"),
                "evidence_by_scope": _count(links, "scope"), "matched_words": name, "linked_sources_found": overlap,
                "evidence_pages_found": page_hits, "score": score})
            self._gaps(st, p, links, vn_vars)
        top_ids = {p["process_id"] for p in st.processes}
        self._models(st, cat, top_ids)
        self._conflicts(st, cat, top_ids)
        self._causal(st, cat, top_ids)
        self._operators(st, cat, top_ids)
        self._evidence_rows(st, cat, {x.get("vn_id") for p in st.processes for x in links_by.get(p["process_id"], [])})

    def _gaps(self, st: _State, p: dict[str, Any], links: list[dict[str, Any]], vn_vars: dict[str, set[str]]) -> None:
        """Gaps of one process, all UNKNOWN and shown by name (never with the values printed in the catalogue cell):

        * ``LINKED_OTHER_SCOPE`` — the parameter's evidence records linked to the process are all of other sites
          (a transfer to SKRU-1 needs an explicit Transfer);
        * ``NO_LINKED_RECORD`` — a recognised parameter (lexicon) with no linked record of its kind;
        * ``CURATED_GAP`` — the catalogue's own ``data_gaps``;
        * ``NOT_MATCHED`` — one line with the parameters the lexicon does not recognise: their coverage was not
          checked (reported as such, not as missing)."""
        pid = p["process_id"]
        param_links = [x for x in links if (x.get("kind") or "") in PARAM_LINK_KINDS]
        has_skru1 = any((x.get("scope") or "") in SKRU1_SCOPES for x in param_links)
        link_keys = []
        for x in param_links:
            keys = {k for k, (_st, _sy, vars_, kinds) in PARAM_LEXICON.items() if (x.get("kind") or "") in kinds}
            for var in vn_vars.get(x.get("vn_id") or "", set()):
                keys |= {k for k, (_st, _sy, vars_, _k) in PARAM_LEXICON.items() if var in vars_}
            link_keys.append((x, keys))
        base = {"process_id": pid, "status": "UNKNOWN", "readiness": p.get("readiness"),
                "process_has_skru1_parameter_records": has_skru1}
        unmatched: list[str] = []
        for item in _split_parameters(p.get("required_parameters")):
            words = words_of(item)
            tokens = set(_WORD.findall(item)) | set(item.split())
            keys = {k for k, (stems_, symbols, _v, _k) in PARAM_LEXICON.items()
                    if any(all(any(w.startswith(s) for w in words) for s in phrase.split()) for phrase in stems_)
                    or any(sym in tokens for sym in symbols)}
            if not keys:
                unmatched.append(_param_name(item))
                continue
            linked = [x for x, k in link_keys if keys & k]
            if any((x.get("scope") or "") in SKRU1_SCOPES for x in linked):
                continue                                           # a SKRU-1 record is linked: not a gap here
            st.gaps.append({**base, "parameter": _param_name(item), "parameter_as_catalogued": short(item, 140),
                            "coverage": "LINKED_OTHER_SCOPE" if linked else "NO_LINKED_RECORD",
                            "scopes": sorted({x.get("scope") or "?" for x in linked}),
                            "evidence_vn_ids": [x.get("vn_id") for x in linked][:4],
                            "where_to_look": sorted({PARAM_CATALOGUES[k] for k in keys if k in PARAM_CATALOGUES})})
        gaps_text = str(p.get("data_gaps") or "").strip()
        if _WORD.search(gaps_text) and norm(gaps_text) not in ("нет", "none", "n/a", "na"):
            st.gaps.append({**base, "parameter": short(gaps_text, 160),
                            "parameter_as_catalogued": short(gaps_text, 160), "coverage": "CURATED_GAP", "scopes": [],
                            "evidence_vn_ids": [], "where_to_look": []})
        if unmatched:
            st.gaps.append({**base, "status": "NOT_CHECKED", "parameter": "; ".join(unmatched)[:200],
                            "parameter_as_catalogued": short(p.get("required_parameters"), 200),
                            "coverage": "NOT_MATCHED", "scopes": [], "evidence_vn_ids": [], "where_to_look": []})

    def _models(self, st: _State, cat: Any, top_ids: set[str]) -> None:
        M = cat.rows("mathematical_model_registry")
        refs: dict[str, list[str]] = {}
        for p in st.processes:
            for mid in p["model_ids"]:
                refs.setdefault(mid, []).append(p["process_id"])
        fields_ = (("name_ru", 3.0), ("physical_meaning_ru", 1.5), ("group", 1.0), ("intended_role", 0.5))
        docs = [_catalogue_words(m, fields_) for m in M]
        idf = idf_weights(st.stems, [frozenset().union(*(w for w, _x in d)) for d in docs]) if st.stems else {}
        scored = []
        for m, f in zip(M, docs):
            ws, name = _word_score(st.stems, idf, f, 3.0) if st.stems else (0.0, set())
            ref = refs.get(m.get("model_id") or "", [])
            if not (ref or (name and ws >= 0.34)):
                continue
            scored.append((round((1.0 + 0.2 * (len(ref) - 1) if ref else 0.0) + ws, 6), m, ref))
        scored.sort(key=lambda x: (-x[0], x[1].get("model_id") or ""))
        st.extra["models"] = max(0, len(scored) - 6)
        for rank, (score, m, ref) in enumerate(scored[:6], 1):
            st.models.append({"rank": rank, "model_id": m.get("model_id"), "name": short(m.get("name_ru"), 110),
                              "math_class": m.get("math_class"), "equation": short(m.get("equation_plain"), 140),
                              "status": m.get("status"), "confidence": m.get("confidence"), "scale": m.get("scale"),
                              "site_applicability": short(m.get("site_applicability"), 60),
                              "source_ids": split_ids(m.get("source_ids"))[:4],
                              "locator": short(m.get("locator"), 90), "processes": ref, "score": score})

    def _conflicts(self, st: _State, cat: Any, top_ids: set[str]) -> None:
        model_ids = {m["model_id"] for m in st.models}
        prank = {p["process_id"]: p["rank"] for p in st.processes}

        def link_weight(ids: list[str]) -> float:     # a conflict of a top process outranks one of a model
            return max([1.2 / (1 + 0.2 * (prank[i] - 1)) for i in ids if i in prank] +
                       [0.8 for i in ids if i in model_ids] + [0.0])

        scored = []
        for table, label, fields_, status_f, handling_f, linked_f in CONFLICT_TABLES:
            rows = cat.rows(table)
            if not rows:
                continue
            docs = [_catalogue_words(r, fields_) for r in rows]
            idf = idf_weights(st.stems, [frozenset().union(*(w for w, _x in d)) for d in docs]) if st.stems else {}
            for r, f in zip(rows, docs):
                ws, name = _word_score(st.stems, idf, f, fields_[0][1]) if st.stems else (0.0, set())
                linked = set(split_ids(r.get(linked_f))) if linked_f else set()
                link = sorted(linked & (top_ids | model_ids))
                if not (link or (name and ws >= 0.34)):
                    continue
                vn = split_ids(r.get("vn_ids")) + split_ids(r.get("vn_ids_a")) + split_ids(r.get("vn_ids_b"))
                sources = split_ids(r.get("source_ids")) or sorted(set(re.findall(r"VKM-SRC-\d{3}", " ".join(
                    str(r.get(k) or "") for k in ("sources_locators", "locators", "locator")))))
                scored.append((round(link_weight(link) + ws, 6), {
                    "conflict_id": r.get("conflict_id"), "catalogue": table, "label": label,
                    "topic": short(r.get(fields_[0][0]), 110), "status": r.get(status_f),
                    "handling": short(r.get(handling_f), 140), "linked": link, "n_vn": len(vn),
                    "source_ids": sources[:5]}))
        scored.sort(key=lambda x: (-x[0], x[1]["conflict_id"] or ""))
        st.extra["conflicts"] = max(0, len(scored) - 6)
        st.conflicts = [{"rank": i, **c, "score": s} for i, (s, c) in enumerate(scored[:6], 1)]

    def _causal(self, st: _State, cat: Any, top_ids: set[str]) -> None:
        nodes = {n.get("node_id"): n for n in cat.rows("causal_graph_nodes")}
        edges = cat.rows("causal_graph_edges")
        matched = {nid for nid, n in nodes.items() if set(split_ids(n.get("process_ids"))) & top_ids}
        if st.stems:
            docs = {nid: [(words_of(n.get("label_ru")), 3.0)] for nid, n in nodes.items()}
            idf = idf_weights(st.stems, [d[0][0] for d in docs.values()])
            matched |= {nid for nid, d in docs.items() if _word_score(st.stems, idf, d, 3.0)[0] >= 0.5}
        scored = []
        for e in edges:
            a, b = e.get("from_node"), e.get("to_node")
            ends = int(a in matched) + int(b in matched)
            if not ends:
                continue
            procs = sorted(set(split_ids(e.get("process_ids"))) & top_ids)
            score = ends + {"STRONG": 0.3, "MODERATE": 0.15}.get(e.get("strength") or "", 0.0) + 0.3 * bool(procs)
            scored.append((round(score, 6), {
                "edge_id": e.get("edge_id"), "from_node": a,
                "from_label": short((nodes.get(a) or {}).get("label_ru"), 60),
                "to_node": b, "to_label": short((nodes.get(b) or {}).get("label_ru"), 60),
                "edge_type": e.get("edge_type"), "mechanism": short(e.get("mechanism"), 100),
                "strength": e.get("strength"), "status": e.get("status"), "scope": e.get("scope"),
                "processes": procs}))
        scored.sort(key=lambda x: (-x[0], x[1]["edge_id"] or ""))
        st.extra["causal"] = max(0, len(scored) - 8)
        st.causal = [{"rank": i, **c} for i, (_s, c) in enumerate(scored[:8], 1)]

    def _operators(self, st: _State, cat: Any, top_ids: set[str]) -> None:
        rows = cat.rows("observation_operator_design")
        fields_ = (("observable", 3.0), ("modality", 2.0), ("predicted_output", 1.0))
        docs = [_catalogue_words(r, fields_) for r in rows]
        idf = idf_weights(st.stems, [frozenset().union(*(w for w, _x in d)) for d in docs]) if st.stems else {}
        scored = []
        for r, f in zip(rows, docs):
            ws, name = _word_score(st.stems, idf, f, 3.0) if st.stems else (0.0, set())
            link = sorted(set(split_ids(r.get("linked_processes"))) & top_ids)
            if not (link or (name and ws >= 0.34)):
                continue
            scored.append((round((1.0 if link else 0.0) + ws, 6), {
                "operator_id": r.get("operator_id"), "modality": r.get("modality"),
                "observable": short(r.get("observable"), 90), "operator_status": r.get("operator_status"),
                "skru1_data_availability": short(r.get("skru1_data_availability"), 120), "status": r.get("status"),
                "scope": r.get("scope"), "processes": link}))
        scored.sort(key=lambda x: (-x[0], x[1]["operator_id"] or ""))
        st.extra["operators"] = max(0, len(scored) - 3)
        st.operators = [{"rank": i, **c} for i, (_s, c) in enumerate(scored[:3], 1)]

    def _evidence_rows(self, st: _State, cat: Any, process_vns: set[str]) -> None:
        scored = []
        for table, label, id_field, fields_, topic in EVIDENCE_TABLES:
            rows = cat.rows(table)
            if not rows or not st.stems:
                continue
            topic_hit = bool(hits(st.stems, words_of(topic)))
            docs = [_catalogue_words(r, fields_) for r in rows]
            idf = idf_weights(st.stems, [frozenset().union(*(w for w, _x in d)) for d in docs])
            for r, f in zip(rows, docs):
                ws, _name = _word_score(st.stems, idf, f, max(w for _f, w in fields_))
                vn_link = bool(set(split_ids(r.get("vn_ids"))) & process_vns)
                if not (vn_link or ws >= 0.34 or topic_hit):
                    continue
                scope = r.get("site_scope") or r.get("scope") or r.get("mine_scope")
                skru1 = (r.get("skru1_relevance") or "").startswith("SKRU1") or norm(scope).startswith("skru1")
                score = ws + (0.6 if vn_link else 0.0) + (0.3 if topic_hit else 0.0) + (0.2 if skru1 else 0.0)
                scored.append((round(score, 6), {
                    "row_id": r.get(id_field), "catalogue": table, "label": label,
                    "what": short(" · ".join(str(r.get(k)) for k, _w in fields_[:2] if r.get(k)), 120),
                    "status": r.get("status"), "scope": scope, "scale": r.get("scale"),
                    "source_ids": (split_ids(r.get("source_ids")) or split_ids(r.get("source_id")))[:3],
                    "locator": short(r.get("locator") or r.get("locators"), 70), "linked_to_processes": vn_link}))
        scored.sort(key=lambda x: (-x[0], x[1]["catalogue"], x[1]["row_id"] or ""))
        st.extra["evidence"] = max(0, len(scored) - 6)
        st.evidence = [{"rank": i, **c} for i, (_s, c) in enumerate(scored[:6], 1)]

    # ---------------------------------------------------------------------------------------------------- helpers
    def _canon_rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        return self.canon.query(sql, params)

    def _page_list(self, st: _State) -> dict[str, list[dict[str, Any]]]:
        """The ranked pages of the dossier per tier (JSON only, ids): the fused retrieval pages, or without retrieval
        the best pages of the chosen sections followed by their other pages (≤ CORE_PAGES / REST_PAGES)."""
        out: dict[str, list[dict[str, Any]]] = {"core": [], "rest": []}
        if st.units:
            for u in st.units:
                out[u["tier"].lower()].append({"page_id": u["page_id"], "source_id": u["source_id"],
                                               "page_index": u["page_index"], "rank": u["rank"],
                                               "rrf": u["weight"], "formulations": u["formulations"]})
            return out
        spans = [(s["source_id"], s["pages"]["first_index"], min(int(s["pages"]["last_index"] or 0),
                                                                 int(s["pages"]["first_index"] or 0) + 49))
                 for s in st.sections if s["pages"]["first_index"] is not None]
        by_source: dict[str, list[tuple[int, str]]] = {}
        if spans:
            clause = " OR ".join("(source_id = ? AND page_index BETWEEN ? AND ?)" for _ in spans)
            for r in self._canon_rows(f"SELECT page_id, source_id, page_index FROM pages WHERE {clause} "
                                      f"ORDER BY source_id, page_index", [x for sp in spans for x in sp]):
                by_source.setdefault(r["source_id"], []).append((r["page_index"], r["page_id"]))
        for tier, cap in (("CORE", CORE_PAGES), ("REST", REST_PAGES)):
            secs = [s for s in st.sections if s["tier"] == tier]
            lst, seen = out[tier.lower()], set()

            def add(pid: str, sid: str, index: Any, lst=lst, seen=seen, cap=cap) -> None:
                if pid not in seen and len(lst) < cap:
                    seen.add(pid)
                    lst.append({"page_id": pid, "source_id": sid, "page_index": index, "rank": len(lst) + 1})

            for s in secs:
                for u in s["units"]:
                    add(u["page_id"], s["source_id"], u.get("page_index"))
            for s in secs:
                a, b = s["pages"]["first_index"], s["pages"]["last_index"]
                for index, pid in by_source.get(s["source_id"], []):
                    if a is not None and a <= index <= (b if b is not None else a):
                        add(pid, s["source_id"], index)
        return out

    def _projection(self, st: _State) -> dict[str, Any] | None:
        nav_snap = st.inputs.get("nav_snapshot_id")
        if nav_snap:
            return {"engine": "navigation", "index_or_graph": "nav.duckdb (+ catalogues, search)",
                    "build_id": nav_snap, "built_from_snapshot_id": nav_snap}
        retrieval = st.inputs.get("retrieval") or {}
        if retrieval.get("mode") == "FULL":
            return {"engine": "opensearch", "index_or_graph": "hybrid (BM25 + dense)",
                    "build_id": retrieval.get("build_id"),
                    "built_from_snapshot_id": retrieval.get("built_from_snapshot_id")}
        cats = st.inputs.get("catalogues") or {}
        if cats.get("pack_id"):
            return {"engine": "catalogues", "index_or_graph": "catalogues.duckdb", "build_id": cats["pack_id"],
                    "built_from_snapshot_id": None}
        return None

    # ---------------------------------------------------------------------------------------------------- render
    def _head(self, st: _State) -> list[str]:
        req = st.req
        mode = (st.inputs.get("retrieval") or {}).get("mode", "NAV_ONLY")
        cats = (st.inputs.get("catalogues") or {}).get("pack_id") or "нет"
        src = f" · источники: {', '.join(req.source_ids)}" if req.source_ids else ""
        codes = sorted({w["code"] for w in st.warnings})
        kinds = ", ".join(f["kind"] for f in st.formulations) or "query"
        core = (st.inputs.get("core_tier") or {}).get("sources")
        return [f"# Досье темы «{short(req.query, 120)}»", f"_{NOTE}_",
                f"поиск: {mode} ({kinds}; RRF) · ядро ВКМ: {core if core is not None else '?'} источников · NAV: "
                f"{st.inputs.get('nav_snapshot_id') or 'нет'} · каталоги: {cats}{src}"
                + (f" · предупреждения: {', '.join(codes)}" if codes else ""), ""]

    def _entries(self, st: _State) -> list[Entry]:
        out: list[Entry] = []
        for s in st.sections:
            cat = "sections_core" if s["tier"] == "CORE" else "sections_rest"
            via = ", ".join({"retrieval": "поиск", "title": "заголовок", "concept": "понятие"}[x] for x in s["signals"])
            lines = [f"{s['rank']}. `{s['section_id']}` {s['source_id']} · "
                     f"{_pages(s['pages']['first_index'], s['pages']['last_index'])} · "
                     f"«{short(s['title_path'] or s['title'], 150)}» [{via}]"]
            for u in s["units"][:2]:
                label = f"`{u['unit_id']}` " if u.get("unit_id") else ""
                snip = f" «{u['snippet']}»" if u.get("snippet") else ""
                lines.append(f"   ↳ {label}`{u['page_id']}`{snip}")
            if s["related_sections"]:
                lines.append(f"   ещё в этой ветке: {', '.join('`' + x + '`' for x in s['related_sections'][:3])}")
            out.append(Entry(cat, s["section_id"], _prio(cat, s["rank"] - 1), lines, s))
        for v in st.visual:
            what = {"FIGURE": "рисунок", "TABLE": "таблица"}[v["kind"]]
            text = f"- `{v['object_id']}` [{what}] " + (f"{v['label']} " if v.get("label") else "") + \
                (f"«{v['caption']}» " if v.get("caption") else "") + f"· `{v['page_id']}`"
            out.append(Entry("visual", v["object_id"], _prio("visual", v["rank"] - 1), [text], v))
        for f in st.formulas:
            parts = [f"- `{f['formula_id']}`" + (f" ({f['equation_number']})" if f["equation_number"] else ""),
                     f"{f['source_id']} {_pages(f['page_index'], None)}"]
            if f["section_id"]:
                parts.append(f"`{f['section_id']}`")
            if f["latex"]:
                parts.append(f"`{f['latex']}`")
            if f["symbols"]:
                parts.append("; ".join(f"{s['symbol']} — {s['definition']}" + (f", {s['unit']}" if s.get("unit")
                                                                                 else "") for s in f["symbols"][:4]))
            if f["parameters"]:
                parts.append("параметры-кандидаты: " + "; ".join(
                    f"{p['symbol']} = {p['value_text']}" + (f" {p['unit']}" if p.get("unit") else "")
                    for p in f["parameters"]))
            if f["n_refs_in"]:
                parts.append(f"ссылок на неё {f['n_refs_in']}")
            out.append(Entry("formulas", f["formula_id"], _prio("formulas", f["rank"] - 1), [" · ".join(parts)], f))
        out += self._concept_entries(st)
        for i, t in enumerate(st.topics):
            tid = t.get("topic_id") or t.get("id") or f"topic-{i + 1}"
            label = t.get("label") or ", ".join(str(x) for x in (t.get("label_terms") or [])[:4])
            terms = t.get("terms")
            terms_s = ", ".join(str(x) for x in terms[:6]) if isinstance(terms, list) else ""
            size = t.get("n_sections") or t.get("size")
            secs = ((t.get("detail") or {}).get("section_ids") or t.get("central_section_ids") or [])[:3]
            out.append(Entry("topics", str(tid), _prio("topics", i), [
                f"- `{tid}` {short(label, 80)}" + (f" · разделов {size}" if size else "")
                + (f" · {terms_s}" if terms_s else "")
                + (f" · разделы: {', '.join('`' + s + '`' for s in secs)}" if secs else "")], t))
        for s in st.sources:
            cat = "sources_core" if s["tier"] == "CORE" else "sources_rest"
            who = s["authors"].split(";")[0].strip() if s.get("authors") else "авторы ?"
            if s.get("authors") and ";" in s["authors"]:
                who += " и др."
            scope = s.get("site_scope_raw") or "?"
            ev = s.get("evidence_catalogue") or {}
            extra = f" · по каталогу: {short(ev.get('mine_attribution'), 60)}" if ev.get("mine_attribution") else ""
            counts = ", ".join(x for x in (f"разделов {s['n_sections']}" if s["n_sections"] else "",
                                           f"формул {s['n_formulas']}" if s["n_formulas"] else "",
                                           f"цитируют {s['cited_by_works']}" if s.get("cited_by_works") else "") if x)
            out.append(Entry(cat, s["source_id"], _prio(cat, s["rank"] - 1), [
                f"- {s['source_id']} → `{s['work_id'] or '?'}` · {who} ({s.get('year') or '?'}) · "
                f"{s.get('work_type') or s.get('source_class') or '?'} · «{short(s.get('title'), 90)}» · "
                f"область (реестр): {scope}{extra}" + (f" · {counts}" if counts else "")], s))
        if st.citations.get("within_set"):
            text = "; ".join(f"`{c['citing_work_id']}` → `{c['cited_work_id']}`"
                             for c in st.citations["within_set"][:8])
            out.append(Entry("citations", "within_set", _prio("citations", 0),
                             [f"- внутри набора: {text}"], {"within_set": st.citations["within_set"]}))
        top = [c for c in st.citations.get("most_cited") or []]
        if top:
            text = "; ".join(f"`{c['work_id']}` {short(c.get('authors'), 30)} ({c.get('year') or '?'}) "
                             f"«{short(c.get('title'), 50)}» — {c['cited_by_set_works']}" for c in top[:5])
            out.append(Entry("citations", "most_cited", _prio("citations", 1),
                             [f"- чаще всего цитируются набором: {text}"], {"most_cited": top}))
        for p in st.processes:
            ev = "; ".join(f"`{e['vn_id']}` {e['source_id']} с.{e.get('pdf_page') or '?'} {e.get('status')}/"
                           f"{e.get('scope')}/{e.get('scale')}/{e.get('confidence')}" for e in p["evidence"][:3])
            more = f" (+{p['n_evidence'] - 3})" if p["n_evidence"] > 3 else ""
            lines = [f"- **{p['process_id']}** {short(p['process'], 100)} · {p['group']}/{p['domain']} · "
                     f"готовность {p['readiness']} · {p['status']} · scope {p['scope']} · {p['confidence']}",
                     f"  уравнения: {short(p['governing_equations'], 150)}",
                     f"  параметры: {short(p['required_parameters'], 150)}"]
            if ev:
                lines.append(f"  evidence ({p['n_evidence']}): {ev}{more}")
            out.append(Entry("processes", p["process_id"], _prio("processes", p["rank"] - 1), lines, p))
        prank = {p["process_id"]: p["rank"] for p in st.processes}
        within: dict[str, int] = {}
        for i, g in enumerate(st.gaps):
            j = within[g["process_id"]] = within.get(g["process_id"], -1) + 1
            prio = _prio("processes", prank.get(g["process_id"], 99) - 1) - 0.5 - 0.2 * j
            if g["coverage"] == "CURATED_GAP":
                text = f"- {g['process_id']} · кураторский пробел: {g['parameter']}"
            elif g["coverage"] == "LINKED_OTHER_SCOPE":
                text = (f"- {g['process_id']} · «{g['parameter']}» — UNKNOWN для СКРУ-1: связаны только записи "
                        f"{', '.join(g['scopes'])} ({', '.join('`' + v + '`' for v in g['evidence_vn_ids'][:2])}); "
                        f"перенос только через Transfer")
            elif g["coverage"] == "NOT_MATCHED":
                text = (f"- {g['process_id']} · не сверено с записями evidence (параметр не распознан): "
                        f"{short(g['parameter'], 150)}")
                prio -= 3
            else:
                why = "связанной с процессом записи evidence не найдено"
                if g.get("process_has_skru1_parameter_records"):
                    why += "; у процесса есть записи СКРУ-1 — сверить"
                text = f"- {g['process_id']} · «{g['parameter']}» — UNKNOWN: {why}"
                if g.get("where_to_look"):
                    text += f" (искать: {' · '.join(g['where_to_look'])})"
            out.append(Entry("gaps", f"{g['process_id']}#{i + 1}", prio, [text], g))
        for m in st.models:
            out.append(Entry("models", m["model_id"], _prio("models", m["rank"] - 1), [
                f"- `{m['model_id']}` {m['name']} · {m['math_class']} · {m['status']}/{m['confidence']} · "
                f"{m['scale']} · {m['locator'] or ', '.join(m['source_ids'])}"
                + (f" · {', '.join(m['processes'])}" if m["processes"] else "")], m))
        for c in st.conflicts:
            out.append(Entry("conflicts", c["conflict_id"] or "?", _prio("conflicts", c["rank"] - 1), [
                f"- `{c['conflict_id']}` [{c['label']}] {c['topic']} · {c['status']}"
                + (f" · {', '.join(c['linked'])}" if c["linked"] else "") + f" · {c['n_vn']} vn · "
                f"{', '.join(c['source_ids'][:3])}" + (f" · {short(c['handling'], 90)}" if c["handling"] else "")], c))
        for e in st.evidence:
            out.append(Entry("evidence", e["row_id"] or "?", _prio("evidence", e["rank"] - 1), [
                f"- `{e['row_id']}` [{e['label']}] {e['what']} · {e.get('status')} · {e.get('scope')} · "
                f"{e.get('scale')} · {', '.join(e['source_ids'])}" + (f" {e['locator']}" if e["locator"] else "")], e))
        for o in st.operators:
            out.append(Entry("operators", o["operator_id"] or "?", _prio("operators", o["rank"] - 1), [
                f"- `{o['operator_id']}` {o['modality']}: {o['observable']} · {o['operator_status']} · "
                f"СКРУ-1: {o['skru1_data_availability']}"], o))
        for c in st.causal:
            out.append(Entry("causal", c["edge_id"] or "?", _prio("causal", c["rank"] - 1), [
                f"- `{c['from_node']}` {c['from_label']} → `{c['to_node']}` {c['to_label']} · {c['edge_type']}/"
                f"{c['strength']} · {c['status']}" + (f" · {', '.join(c['processes'])}" if c["processes"] else "")],
                c))
        return out

    def _concept_entries(self, st: _State) -> list[Entry]:
        c = st.concept
        if not c:
            return []
        m = c["match"]
        focus = c.get("focus")
        defs = c.get("definitions") or []
        head = (f"- «{m.get('lemma')}» `{m.get('term_id')}` · разделов {m.get('df_units')} · источников "
                f"{m.get('df_sources')}" + (f" · фокус: «{focus.get('lemma')}»" if focus else ""))
        if defs:
            head += " · определения: " + ", ".join(f"`{d.get('block_id')}`" for d in defs[:3])
        out = [Entry("concept", m.get("term_id") or "match", _prio("concept", 0), [head], {
            "match": {k: m.get(k) for k in ("term_id", "lemma", "language", "df_units", "df_sources", "match")},
            "focus": focus, "definitions": defs[:3],
            "alternatives": (c.get("alternatives") or [])[:3], "note": c.get("note")})]
        near = c.get("neighbours") or []
        if near:
            text = ", ".join(f"{n.get('lemma')} ({n.get('n_units')}/{n.get('n_sources')})" for n in near[:10])
            out.append(Entry("concept", "neighbours", _prio("concept", 1), [f"- обсуждаются рядом: {text}"],
                             {"neighbours": near[:10]}))
        rel = [("шире", c.get("broader") or []), ("уже", c.get("narrower") or []), ("синонимы", c.get("same_as") or [])]
        parts = [f"{name}: " + ", ".join(str(x.get("lemma")) for x in xs[:4]) for name, xs in rel if xs]
        if parts:
            out.append(Entry("concept", "relations", _prio("concept", 3), ["- " + " · ".join(parts)],
                             {name: xs[:4] for name, xs in rel if xs}))
        return out

    def _record(self, st: _State, kept: list[Entry], dropped: list[Entry], text: str) -> dict[str, Any]:
        req = st.req
        by: dict[str, list[Entry]] = {}
        for e in kept:
            by.setdefault(e.category, []).append(e)
        concept: dict[str, Any] | None = None
        for e in by.get("concept", []):
            concept = {**(concept or {}), **e.data}
        citations: dict[str, Any] = {}
        for e in by.get("citations", []):
            citations.update(e.data)
        if citations:
            citations["note"] = st.citations.get("note")
        trimmed: dict[str, Any] = {}
        for e in dropped:
            t = trimmed.setdefault(e.category, {"dropped_by_budget": 0, "ids": [], "how_to_get": HOW_TO[e.category]})
            t["dropped_by_budget"] += 1
            if len(t["ids"]) < 10:
                t["ids"].append(e.key)
        for cat, n in st.extra.items():
            if n:
                trimmed.setdefault(cat, {"dropped_by_budget": 0, "ids": [], "how_to_get": HOW_TO[cat]})[
                    "beyond_limits"] = n
        return {
            "query": req.query, "query_stems": st.stems, "rule_version": RULE_VERSION,
            "mode": (st.inputs.get("retrieval") or {}).get("mode", "NAV_ONLY"), "note": NOTE,
            "source_filter": list(req.source_ids),
            "inputs": st.inputs,
            "formulations": st.formulations,
            "pages": self._page_list(st),
            "sections": [e.data for e in by.get("sections_core", []) + by.get("sections_rest", [])],
            "formulas": [e.data for e in by.get("formulas", [])],
            "visual": [e.data for e in by.get("visual", [])],
            "concept": concept, "topics": [e.data for e in by.get("topics", [])],
            "sources": [e.data for e in by.get("sources_core", []) + by.get("sources_rest", [])],
            "citations": citations or None,
            "catalogue": {"processes": [e.data for e in by.get("processes", [])],
                          "models": [e.data for e in by.get("models", [])],
                          "conflicts": [e.data for e in by.get("conflicts", [])],
                          "evidence_rows": [e.data for e in by.get("evidence", [])],
                          "operators": [e.data for e in by.get("operators", [])],
                          "causal": [e.data for e in by.get("causal", [])]},
            "gaps": [e.data for e in by.get("gaps", [])],
            "budget": {"budget_chars": req.budget_chars, "markdown_chars": len(text), "trimmed": trimmed},
            "markdown": text}


def _evidence_page(link: dict[str, Any]) -> str | None:
    """The canonical page of a catalogue evidence link (``pdf_page`` is the 1-based PDF page, as the page ids);
    None without a page number."""
    m = re.match(r"\s*(\d+)", str(link.get("pdf_page") or ""))
    return f"{link.get('source_id')}:p{int(m.group(1)):04d}" if m and link.get("source_id") else None


def _param_name(item: str) -> str:
    """The name of a required parameter without the values printed with it in the catalogue cell
    («UCS гидрозакладки 3,5–4,0 МПа» → «UCS гидрозакладки», «Δb=5 м (…)» → «Δb», «k_зап (учебные 0,73…)» → «k_зап»)."""
    s = re.sub(r"\([^()]*\d[^()]*\)", " ", item)                     # parenthesised groups with numbers
    m = re.search(r"\s*(?:=|:\s*[\d≈~<>±]|\s[\d≈~<>±])", s)
    if m and m.start() >= 1:
        s = s[: m.start()]
    s = re.sub(r"\s+,", ",", " ".join(s.split())).strip(" ,;:–—-|")
    return short(s or item, 80)


def _adjacent(stems: list[str], words: list[str]) -> bool:
    """The stems begin consecutive words (in this order or reversed): «начальное напряжение», not «напряжение …
    в начальной точке»."""
    for seq in (stems, stems[::-1]):
        for i in range(len(words) - len(seq) + 1):
            if all(begins(words[i + k], s) for k, s in enumerate(seq)):
                return True
    return False


def _count(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = r.get(key) or "?"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def _split_parameters(text: Any) -> list[str]:
    """Items of a «required_parameters» cell: split on ';' outside parentheses; 1–2 letter items (g) and value
    fragments (an item that starts with a number) skipped."""
    out, depth, cur = [], 0, []
    for ch in str(text or ""):
        depth += ch in "([" and 1 or (-1 if ch in ")]" else 0)
        if ch == ";" and depth <= 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [x for x in out if sum(len(w) for w in _WORD.findall(x)) >= 3 and not re.match(r"[\s(≈~<>±+−-]*\d", x)]


def dossier_id(query: str, source_ids: Iterable[str]) -> str:
    key = norm(" ".join(query.split())) + "|" + ",".join(sorted(source_ids))
    return "topic-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def make_retrieval(injected: Any, hybrid: Any) -> TopicRetrieval | None:
    """The injected retrieval, else the hybrid backend's adapter, else None (NAV only)."""
    if injected is not None:
        return injected
    return HybridTopicRetrieval(hybrid) if hybrid is not None else None


__all__ = ["DossierBuilder", "Dossier", "TopicRequest", "TopicRetrieval", "HybridTopicRetrieval", "make_retrieval",
           "dossier_id", "query_stems", "stem", "snippet", "fit_budget", "render", "Entry", "DEFAULT_BUDGET",
           "MIN_BUDGET", "MAX_BUDGET", "RULE_VERSION"]
