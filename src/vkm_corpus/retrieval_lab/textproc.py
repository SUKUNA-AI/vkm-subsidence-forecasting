"""Text helpers of the lab: an analyzer that mirrors OpenSearch ``vkm_text`` (agent E) for the local BM25, sentence
splitting for over-long blocks, and references to figure/table numbers.

The analyzer follows ``vkm_corpus.search.analysis`` (``vkm-analysis/1``): invisible characters stripped, super/sub-script
digits and ё→е folded; tokens roughly as the Lucene standard tokenizer (letters+digits stay together, «3.1» is one
token); possessive ’s removed; lowercase; Russian and English stop words; protected mineral names kept (lemmas as
keywords, case forms mapped to the lemma — E's ``stemmer_override``); Snowball-Russian, then Porter-English. The local
BM25 is a deterministic stand-in for OpenSearch BM25 and is checked against it (design §16).
"""
from __future__ import annotations

import re
from functools import lru_cache

from vkm_corpus.search.analysis import PROTECTED_LEMMAS, SUPSUB_FOLD, protected_forms

ANALYZER_ID = "vkm_text-mirror/1 (vkm-analysis/1)"

# Lucene EnglishAnalyzer default stop set (_english_)
EN_STOP: frozenset[str] = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such that the their then there these they "
    "this to was will with".split())
# Snowball Russian stop list (_russian_ in Lucene), with ё already folded to е
RU_STOP: frozenset[str] = frozenset(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня "
    "еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там "
    "потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже "
    "себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее "
    "сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про "
    "всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им "
    "более всегда конечно всю между".split())
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)
_FOLD = {ord(a.strip()): b.strip() for a, _, b in (m.partition("=>") for m in SUPSUB_FOLD)}
_FOLD.update({ord("ё"): "е", ord("Ё"): "Е"})
# letters/digits runs; digits with inner '.' or ',' stay one token («3.1», «0,5»)
_TOKEN = re.compile(r"[0-9]+(?:[.,][0-9]+)+|[^\W_]+(?:['’][^\W_]+)*")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9«(\[])")
_NUMBER = re.compile(r"(\d+(?:[.\-–]\d+)*[a-zа-я]?)", re.IGNORECASE)


@lru_cache(maxsize=1)
def _stemmers():
    import snowballstemmer

    return snowballstemmer.stemmer("russian"), snowballstemmer.stemmer("porter")


@lru_cache(maxsize=1)
def _protected() -> tuple[frozenset[str], dict[str, str]]:
    return frozenset(PROTECTED_LEMMAS), protected_forms()


def fold(text: str) -> str:
    return text.translate(_INVISIBLE).translate(_FOLD)


def raw_tokens(text: str) -> list[str]:
    """Lower-cased tokens before stop words and stemming (≈ ``vkm_exact``)."""
    out = []
    for tok in _TOKEN.findall(fold(text)):
        if tok.endswith(("'s", "’s")):
            tok = tok[:-2]
        if tok:
            out.append(tok.lower())
    return out


@lru_cache(maxsize=200_000)
def stem(token: str) -> str:
    lemmas, forms = _protected()
    if token in lemmas:
        return token
    if token in forms:
        return forms[token]
    ru, en = _stemmers()
    return en.stemWord(ru.stemWord(token))


def analyze(text: str) -> list[str]:
    """Tokens as ``vkm_text`` would index them."""
    return [stem(t) for t in raw_tokens(text) if t not in RU_STOP and t not in EN_STOP]


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def pack_sentences(text: str, max_chars: int) -> list[str]:
    """Split an over-long text into parts ≤ ``max_chars`` at sentence boundaries (a single over-long sentence is cut
    at the last space before the limit)."""
    parts: list[str] = []
    cur = ""
    for sent in split_sentences(text):
        while len(sent) > max_chars:
            cut = sent.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            head, sent = sent[:cut].strip(), sent[cut:].strip()
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(head)
        if not cur:
            cur = sent
        elif len(cur) + 1 + len(sent) <= max_chars:
            cur = f"{cur} {sent}"
        else:
            parts.append(cur)
            cur = sent
    if cur:
        parts.append(cur)
    return parts


def trim_to_sentence(text: str, max_chars: int, *, from_end: bool = False) -> str:
    """At most ``max_chars`` of ``text`` ending (or starting) at a sentence boundary when one is available."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    sents = split_sentences(text)
    if from_end:
        out = ""
        for s in reversed(sents):
            if len(out) + len(s) + 1 > max_chars:
                break
            out = f"{s} {out}".strip()
        return out or text[-max_chars:].lstrip()
    out = ""
    for s in sents:
        if len(out) + len(s) + 1 > max_chars:
            break
        out = f"{out} {s}".strip()
    return out or text[:max_chars].rstrip()


def label_number(label: str | None) -> str | None:
    """«Рис. 3.1» → ``3.1``; «(3.12)» → ``3.12``; «Таблица 2а» → ``2а``."""
    if not label:
        return None
    m = _NUMBER.search(label)
    return m.group(1).replace("–", "-").lower() if m else None


def reference_pattern(kind: str, number: str) -> re.Pattern[str]:
    """Regex for a textual reference to figure/table ``number`` («рис. 3.1», «рисунке 3.1», «fig. 3.1», «табл. 2»)."""
    num = re.escape(number).replace(r"\.", r"\s?\.\s?")
    if kind == "FIGURE":
        head = r"(?:рис(?:унок|унке|унка|унки|унков|унках|\.)?|fig(?:ure|s|\.)?)"
    else:
        head = r"(?:табл(?:ица|ице|ицы|иц|ицах|\.)?|table|tab\.)"
    return re.compile(head + r"\s*" + num + r"(?![\d.]\d)", re.IGNORECASE)
