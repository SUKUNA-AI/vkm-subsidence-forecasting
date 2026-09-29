"""Bibliographic intent detector (agent L): strong cues fire alone, weak cues in pairs, eponyms and mining «работы» do
not; on the V0/V1 benchmark queries it fires on exactly the 7 T-BIB questions and the DOI query."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vkm_corpus.search.intent import bibliographic_intent, surname_like

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("text,cue", [
    ("список литературы по ползучести соли", "bibliography_phrase"),
    ("список публикаций соискателя по радарной интерферометрии", "bibliography_phrase"),
    ("ГОСТ на методы испытаний в списке литературы", "bibliography_phrase"),
    ("литература о геологии Верхнекамского месторождения", "bibliography_phrase"),
    ("обзор литературы по мульде сдвижения", "bibliography_phrase"),
    ("библиография по защите рудников", "bibliography_phrase"),
    ("references to salt creep laboratory tests", "bibliography_phrase"),
    ("ссылки на «Указания по защите рудников от затопления»", "citation_phrase"),
    ("кто цитирует работу о мульде сдвижения", "citation_phrase"),
    ("Baryakh et al. salt pillars", "et_al"),
    ("Барях и др. о целиках", "et_al"),
    ("Барях А.А. ползучесть", "author_initials"),
    ("А.А. Барях оседания", "author_initials"),
    ("Baryakh A.A. creep", "author_initials"),
    ("работы Баряха и Самоделкиной о реологии", "works_of_author"),
    ("статьи Кудряшова", "works_of_author"),
    ("articles by Baryakh", "works_of_author"),
    ("Journal of Mining Science 2019", "journal_of"),
    ("doi 10.1134/S1062739119045678", "doi"),
    ("ISBN 978-5-7038-3339-3", "isbn"),
    ("найти «Методика расчёта оседаний земной поверхности»", "quoted_title"),
])
def test_strong_cues_fire(text, cue):
    it = bibliographic_intent(text)
    assert it.bibliographic and cue in it.cues, it


@pytest.mark.parametrize("text", [
    "закон Бингама для соляных пород", "ядро Абеля в наследственной теории", "критерий Кулона–Мора",
    "горные работы на Верхнекамском месторождении", "привязка планов горных работ", "работы по закладке камер",
    "труды Верхнекамского института", "книги по геомеханике", "комбайн «Урал-20КС» при выемке",
    "пласт В на руднике СКРУ-1", "журнал нивелирования реперов", "ГОСТ 21153 прочность на сжатие",
    "мульда сдвижения", "рис. 3.1 мульда сдвижения", "Березники 2006 провал",
])
def test_no_cue_or_one_weak_cue_does_not_fire(text):
    assert not bibliographic_intent(text).bibliographic


def test_two_weak_cues_fire_and_surnames_are_not_adjectives():
    it = bibliographic_intent("ГОСТ 21153 в сборнике трудов конференции")
    assert it.bibliographic and not it.cues and set(it.weak) == {"periodical", "standard"}
    assert surname_like("Баряха") and not surname_like("Верхнекамского") and not surname_like("Соликамской")
    assert bibliographic_intent("").as_dict() == {"bibliographic": False, "cues": [], "weak_cues": []}


def test_benchmark_queries_fire_on_the_bibliographic_ones_only():
    rows = [json.loads(line) for line in open(ROOT / "benchmarks" / "retrieval_v0" / "queries.jsonl", encoding="utf-8")
            if line.strip()]
    fired = {r["query_id"] for r in rows if bibliographic_intent(r["text"]).bibliographic}
    bib = {r["query_id"] for r in rows if r["category"] == "bibliography"}
    assert len(bib) == 7 and bib <= fired
    assert fired - bib == {"T-ENT-006"}                                   # the DOI query (slice "bibliography")
