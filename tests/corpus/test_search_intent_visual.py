"""Visual intent detector (agent VIS): one picture word routes a query to the page-image channel; false friends
(«технологическая карта», «план мероприятий», «фотограмметрия», «картина», «площадь сечения») and plain text questions
do not. Synthetic phrases only — the detector's precision/recall on the benchmark queries is a measured result
(``benchmarks/retrieval_v2/visual_route_v2.json``), pinned by the last test."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vkm_corpus.search.intent import bibliographic_intent, visual_intent

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("text,cue", [
    ("рисунок мульды сдвижения", "figure"),
    ("рис. 3.1 оседания", "figure"),
    ("на рисунках показаны целики", "figure"),
    ("изображение керна каменной соли", "figure"),
    ("схема камерной системы разработки", "scheme"),
    ("схематический разрез пласта", "scheme"),
    ("чертёж крепи ствола", "scheme"),
    ("карта оседаний земной поверхности", "map"),
    ("на карте шахтного поля", "map"),
    ("выкопировка с плана горных работ", "map"),
    ("план горных работ по пласту АБ", "plan"),
    ("геологический разрез Верхнекамского месторождения", "section"),
    ("в разрезе соляной толщи", "section"),
    ("график оседаний во времени", "graph"),
    ("диаграмма деформирования образца", "graph"),
    ("эпюра напряжений в целике", "graph"),
    ("профиль оседания по профильной линии", "profile"),
    ("профильная линия наблюдательной станции", "profile"),
    ("радарограмма георадарного зондирования", "radargram"),
    ("фото обрушения кровли", "photo"),
    ("фотографии керна", "photo"),
    ("космоснимок района рудника", "photo"),
    ("таблица физико-механических свойств соли", "table"),
    ("табл. 2 прочность", "table"),
    ("изолинии оседаний", "isolines"),
    ("интерферограмма Sentinel-1", "interferogram"),
    ("стратиграфическая колонка скважины", "column"),
    ("колонка скважины 75", "column"),
    ("figure of the subsidence trough", "figure"),
    ("subsidence map of the mine field", "map"),
    ("mine plan of the potash seam", "plan"),
    ("geological cross-section of the deposit", "section"),
    ("graph of creep strain versus time", "graph"),
    ("chamber and pillar layout", "scheme"),
    ("GPR profile over the pillar", "profile"),
    ("photo of the roof collapse", "photo"),
    ("table of salt properties", "table"),
])
def test_picture_words_fire(text, cue):
    it = visual_intent(text)
    assert it.visual and cue in it.cues, it


@pytest.mark.parametrize("text", [
    "ползучесть каменной соли",
    "оседание земной поверхности над выработками ВКМ",
    "закладка выработанного пространства",
    "технологическая карта ведения закладочных работ",
    "план мероприятий по защите рудника от затопления",
    "план развития горных работ на 2025 год",
    "фотограмметрия в маркшейдерии",
    "картина сдвижения горных пород",
    "картирование трещин",
    "профилактика горных ударов",
    "планирование горных работ",
    "площадь сечения выработки",
    "модуль упругости сильвинита",
    "критерий Кулона-Мора для соли",
    "список литературы по ползучести соли",
    "business plan of the mine",
    "salt creep constitutive law",
    "рисовать",
    "планета",
    "картинг",
])
def test_text_questions_do_not_fire(text):
    it = visual_intent(text)
    assert not it.visual, it


def test_intent_lists_every_cue_once():
    it = visual_intent("схема и карта оседаний, схема 2, рис. 4")
    assert it.cues == ("figure", "map", "scheme")
    assert it.as_dict() == {"visual": True, "cues": ["figure", "map", "scheme"]}


def test_visual_and_bibliographic_are_independent():
    q = "рисунок из работы Баряха А.А."
    assert visual_intent(q).visual and bibliographic_intent(q).bibliographic
    assert not visual_intent("работы Баряха А.А.").visual


def test_measured_precision_recall_on_the_benchmark_queries_is_pinned():
    """The detector was fixed (commit 00e1b84) before its evaluation; its decisions on the 189 V0/V1 queries are the
    ones recorded in the V2 follow-up (precision 0.76, recall 0.864 against the track labels)."""
    rec = json.loads((ROOT / "benchmarks/retrieval_v2/visual_route_v2.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (ROOT / "benchmarks/retrieval_v0/queries.jsonl").read_text("utf-8")
            .splitlines() if line.strip()]
    fired = {r["query_id"] for r in rows if visual_intent(r["text"]).visual}
    assert fired == set(rec["detector"]["cues"])
    visual = {r["query_id"] for r in rows if r["track"] == "visual"}
    assert sorted(fired - visual) == rec["detector"]["false_positives"]
    assert sorted(visual - fired) == rec["detector"]["false_negatives"]
    assert rec["detector"]["all_189"]["precision"] == 0.76 and rec["detector"]["all_189"]["recall"] == 0.8636
