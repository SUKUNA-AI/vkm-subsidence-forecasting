"""Long questions in natural language in the BM25 legs (agent M, 13.10; opt-in ``long_query``, default ``strict``).

Review 12.10 (live): «Какие максимальные оседания земной поверхности наблюдались на СКРУ-1 и в каком году?» — BM25
finds 24 pages, the keywords of the same question 664. Two causes: ``minimum_should_match = "3<70%"`` over the 9–13
terms of a question, and the question's own words that the ``_russian_`` stop list keeps («какие», «каком»,
«наблюдались», «году», «влияет» …) — a page must hold most of them to match at all. Dense and late partly rescue the
answer; the translation is not built for such a query (``NOT_COVERED``).

Modes (``long_query``; every value is a MODEL_CHOICE of retrieval set a priori — by meaning and on a few questions of
agent M — never fitted to the truth of a benchmark):

* ``strict`` (default) — today's path, byte for byte: nothing here is consulted;
* ``relax`` — a *long* query's BM25 legs use ``minimum_should_match`` :data:`RELAXED_MSM` instead of ``"3<70%"``;
* ``strip`` — a long query's BM25 legs search the question without its frame (:func:`strip_frame`: interrogative and
  request words, «в каком году», «что известно», «наблюдались» … — :data:`FRAME_PATTERNS`; domain terms and numbered
  objects stay); the dense leg, the late stage, the routes (bibliographic, visual, author, entity, label) and the graph
  legs keep the original query; the API builds the translation from the stripped question;
* ``relax_strip`` — both.

A query is *long* when it has at least :data:`LONG_QUERY_MIN_TERMS` content terms (:func:`content_terms`: tokens as
the analyzer cuts them, minus the ``_russian_`` and ``_english_`` stop words, without the numbered objects of
``vkm_corpus.search.object_refs``). A short query takes the old path in every mode. ``plan`` decides; its record is
``stages.long_query`` of the answer (``mode``, ``long``, ``terms``, ``msm``, ``stripped_text`` …).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vkm_corpus.retrieval_lab.textproc import EN_STOP, RU_STOP, raw_tokens
from vkm_corpus.search.object_refs import parse_refs, strip_refs

STRICT, RELAX, STRIP, RELAX_STRIP = "strict", "relax", "strip", "relax_strip"
LONG_QUERY_MODES: tuple[str, ...] = (STRICT, RELAX, STRIP, RELAX_STRIP)
LONG_QUERY_DEFAULT = STRICT
# MODEL_CHOICE (a priori): a keyword query of the corpus has 2–5 content terms («оседание земной поверхности»,
# «ползучесть каменной соли», «мульда сдвижения СКРУ-1»); a question in words has 6–13, of which 2–4 are its frame.
# From 6 terms on, "3<70%" lets a page miss only 2–4 terms — about the frame alone.
LONG_QUERY_MIN_TERMS = 6
STRICT_MSM = "3<70%"                      # SearchRequest's default
# MODEL_CHOICE (a priori): up to 3 terms all, 4–5 terms 70 % (as strict), 6 terms and more 50 %. The conditional
# spec changes exactly the queries the gate calls long (OpenSearch counts the analysed terms itself, per field; the
# ``.exact`` fields keep stop words and so count more); half of a question's terms may be missing — its frame and as
# many topical terms; BM25 still ranks by the matched terms' weights, so the wider set mostly adds candidates.
RELAXED_MSM = "3<70% 5<50%"

_W = r"0-9A-Za-zА-Яа-яЁё_"
_KAKOJ = r"как(?:ой|ая|ое|ие|ого|ую|им|ом|их|ими|ому)"            # «какой» in every form (not «как»)
_KOTORYJ = r"котор(?:ый|ая|ое|ые|ого|ую|ым|ом|ых|ыми|ому|ой)"
_TIME = r"(?:год|года|году|годы|годах|годов|период|периода|периоде|периоды|время|времени|срок|сроки|момент|моменте)"
_FRAME_SOURCES: tuple[str, ...] = (
    # phrases first (whole phrase out): time questions, «каким образом», definitions, «как влияет», English frames
    rf"(?:в|во|с|со|до|за|к|на|по)\s+(?:{_KAKOJ}|{_KOTORYJ})\s+{_TIME}",
    r"в\s+течение\s+какого\s+времени",
    r"каким\s+образом",
    r"что\s+такое",
    r"представля(?:ет|ют)\s+(?:собой|из\s+себя)|из\s+себя\s+представля(?:ет|ют)",
    r"как\s+влия(?:ет|ют|л|ла|ло|ли)|влия(?:ет|ют|л|ла|ло|ли)\s+ли",
    r"what\s+is\s+known\s+about|tell\s+me(?:\s+about)?|in\s+(?:which|what)\s+years?|how\s+(?:many|much)",
    # interrogative words
    _KAKOJ, r"каков(?:а|о|ы)?", _KOTORYJ,
    r"почему|зачем|отчего|сколько|насколько|когда|где|куда|откуда|кто|кого|кому|кем|что|чего|чему|чем|как|ли|"
    r"разве|неужели",
    # requests to the answerer
    r"(?:расскажи|опиши|объясни|перечисли|назови|покажи|приведи|найди|укажи|подскажи|дай)(?:те)?|пожалуйста|хочу|"
    r"узнать|интересует|интересно",
    # «что известно о …» (short forms only: «известняк», «известные модели» stay), existence and being
    r"известн(?:о|ы|а|ен)|существу(?:ет|ют)|име(?:ется|ются)|явля(?:ется|ются|лся|лась|лось|лись)",
    # observation frame of a question («наблюдались», «отмечались», «происходит»); nouns and participles
    # («наблюдения», «наблюдаемые») stay
    r"наблюда(?:ется|ются|лся|лась|лось|лись)|отмеча(?:ется|ются|лся|лась|лось|лись)|"
    r"происход(?:ит|ят|ил|ила|ило|или)",
    # English question words
    r"what|which|how|why|when|where|who|whom|whose|do|does|did|were|please|describe|explain",
)
# MODEL_CHOICE (a priori, by meaning): the question frame removed by ``strip``; ё is folded before matching
FRAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(rf"(?<![{_W}])(?:{p})(?![{_W}])", re.IGNORECASE)
                                                     for p in _FRAME_SOURCES)
_EDGE_PUNCT = " \t,.;:!?–—-«»\"'()"


def content_terms(text: str) -> list[str]:
    """The terms of ``text`` the BM25 match counts: tokens as the analyzer cuts them (``vkm_text`` mirror, lower case,
    ё folded), minus the ``_russian_`` and ``_english_`` stop words, without numbered objects («скв. 75», «рис. 3.1» …)."""
    refs = parse_refs(text)
    topic = strip_refs(text, refs) if refs else text
    return [t for t in raw_tokens(topic) if t not in RU_STOP and t not in EN_STOP]


def is_long(text: str) -> bool:
    return len(content_terms(text)) >= LONG_QUERY_MIN_TERMS


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?)»])", r"\1", text)
    text = re.sub(r"([,;:!?])(?:\s*[,;:!?])+", r"\1", text)
    words = text.strip(_EDGE_PUNCT).split(" ")
    # stop words left at the edges («… на СКРУ-1 и», «о ползучести …» after «что известно») say nothing to BM25
    while words and words[-1].strip(_EDGE_PUNCT).lower().replace("ё", "е") in RU_STOP | EN_STOP:
        words.pop()
    while words and words[0].strip(_EDGE_PUNCT).lower().replace("ё", "е") in RU_STOP | EN_STOP:
        words.pop(0)
    return " ".join(words).strip(_EDGE_PUNCT)


def strip_frame(text: str) -> tuple[str, list[str]]:
    """``text`` without its question frame (:data:`FRAME_PATTERNS`) → (stripped text, removed phrases in order).
    Numbered objects (``object_refs``) are never touched; ё is folded to е in the parts that are matched."""
    refs = parse_refs(text)
    removed: list[str] = []

    def cut(m: re.Match[str]) -> str:
        removed.append(" ".join(m.group(0).split()).lower())
        return " "

    out, pos = [], 0
    for r in [*refs, None]:
        seg = text[pos:r.start if r else len(text)].replace("ё", "е").replace("Ё", "Е")
        for pattern in FRAME_PATTERNS:
            seg = pattern.sub(cut, seg)
        out.append(seg)
        if r:
            out.append(text[r.start:r.end])
            pos = r.end
    return _tidy(" ".join(out)), removed


@dataclass(frozen=True)
class LongQueryPlan:
    """What a mode does to one wording: the BM25 legs run ``bm25_text`` with ``msm`` (``changed`` = one of them
    differs from the wording and the request's ``minimum_should_match``). ``status``: ``STRICT`` (the default mode:
    never consulted on the request path), ``SHORT`` (fewer than :data:`LONG_QUERY_MIN_TERMS` terms: unchanged),
    ``APPLIED``, ``UNCHANGED`` (long, but ``strip`` found nothing to change). ``strip``: ``STRIPPED``, ``NO_FRAME``
    (no frame word), ``NOTHING_LEFT`` (the frame was all of it: the wording stays) or ``None`` (no strip)."""
    mode: str
    long: bool
    terms: int
    msm: str
    bm25_text: str
    status: str
    strip: str | None = None
    stripped_text: str | None = None
    terms_after_strip: int | None = None
    removed: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.status == "APPLIED"

    def record(self) -> dict[str, Any]:
        return {"mode": self.mode, "status": self.status, "long": self.long, "terms": self.terms,
                "min_terms": LONG_QUERY_MIN_TERMS, "msm": self.msm, "strip": self.strip,
                "stripped_text": self.stripped_text, "terms_after_strip": self.terms_after_strip,
                "removed": list(self.removed)}


def plan(text: str, mode: str, msm: str = STRICT_MSM) -> LongQueryPlan:
    """The plan of ``mode`` for one wording ``text`` (``msm`` = the request's own ``minimum_should_match``)."""
    if mode not in LONG_QUERY_MODES:
        raise ValueError(f"long_query is one of {LONG_QUERY_MODES}")
    n = len(content_terms(text))
    long = n >= LONG_QUERY_MIN_TERMS
    if mode == STRICT or not long:
        return LongQueryPlan(mode, long, n, msm, text, "STRICT" if mode == STRICT else "SHORT")
    new_msm = RELAXED_MSM if mode in (RELAX, RELAX_STRIP) else msm
    bm25_text, strip, stripped, n_after, removed = text, None, None, None, ()
    if mode in (STRIP, RELAX_STRIP):
        candidate, cut = strip_frame(text)
        n_after = len(content_terms(candidate))
        if not cut:
            strip = "NO_FRAME"
        elif n_after == 0:
            strip = "NOTHING_LEFT"
        else:
            bm25_text, strip, stripped, removed = candidate, "STRIPPED", candidate, tuple(cut)
    status = "APPLIED" if bm25_text != text or new_msm != msm else "UNCHANGED"
    return LongQueryPlan(mode, True, n, new_msm, bm25_text, status, strip, stripped, n_after, removed)