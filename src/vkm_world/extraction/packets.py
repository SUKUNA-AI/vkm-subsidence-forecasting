"""Packets of corpus pages for the page extractor: 5–10 selected pages (of one source where it has enough selected
pages, otherwise of several sources), the tables of each page, and — marked as context — headings and captions of
the neighbouring pages that are not in the packet.

The page text in a packet is the text the verifier checks against (``page_texts``), so both sides see the same
characters.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

MAX_PAGES = 8
MIN_PAGES = 5
MAX_CHARS = 32000
MAX_NUMBERS = 400                 # printed numbers per packet: dense property tables otherwise overflow one answer
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def n_numbers(text: str) -> int:
    return len(_NUMBER.findall(text or ""))

_HEADING = re.compile(r"^\s*(?:глава\s+\d|приложение\s|введение|заключение|выводы|"
                      r"\d{1,2}(?:\.\d{1,2}){0,3}\.?\s+[А-ЯЁA-Z][^.]{3,110}$)", re.I)
_CAPTION = re.compile(r"^\s*(?:рис\.|рисунок|табл\.|таблица|fig\.|figure|table)\s*\d", re.I)


def page_text(text: str, tables: list[tuple[str | None, str | None, str]]) -> str:
    """The page text followed by its tables (label, caption, cells as ``a | b | c``)."""
    parts = [(text or "").strip()]
    for label, caption, cells in tables:
        head = " ".join(x for x in (label, caption) if x) or "без подписи"
        parts.append(f"[ТАБЛИЦА: {head}]\n{(cells or '').strip()}")
    return "\n\n".join(p for p in parts if p)


def context_lines(text: str, captions: list[str], limit: int = 10) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and len(s) <= 160 and (_HEADING.match(s) or _CAPTION.match(s)):
            out.append(s)
    out += [c.strip() for c in captions if c and c.strip()]
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:limit]


def _page_key(pid: str) -> tuple[str, int]:
    """``VKM-SRC-012:p0050`` → (VKM-SRC-012, 50); rendered DOCX pages are ``…:r0003``."""
    src, _, p = pid.rpartition(":")
    return src, int(p.lstrip("pr"))


@dataclass
class Packet:
    packet_id: str
    tier: str
    page_ids: list[str]
    page_texts: dict[str, str]
    context: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)        # source_id → header text

    @property
    def source_ids(self) -> list[str]:
        out = []
        for pid in self.page_ids:
            s = _page_key(pid)[0]
            if s not in out:
                out.append(s)
        return out

    def render(self) -> str:
        lines = [f"### ПАКЕТ {self.packet_id}", f"Страницы пакета: {', '.join(self.page_ids)}", ""]
        order = sorted(set(self.page_ids) | set(self.context),
                       key=lambda p: (self.source_ids.index(_page_key(p)[0]), _page_key(p)[1]))
        cur = None
        for pid in order:
            src = _page_key(pid)[0]
            if src != cur:
                lines += [f"##### ИСТОЧНИК {src}", self.headers.get(src, ""), ""]
                cur = src
            if pid in self.page_texts:
                lines += [f"=== СТРАНИЦА {pid} ===", self.page_texts[pid], f"=== КОНЕЦ СТРАНИЦЫ {pid} ===", ""]
            else:
                lines += [f"--- КОНТЕКСТ (не извлекать): заголовки и подписи страницы {pid} ---",
                          *(self.context[pid] or ["(нет)"]), ""]
        return "\n".join(lines).rstrip() + "\n"

    def manifest(self) -> dict:
        body = self.render()
        return {"packet_id": self.packet_id, "source_ids": self.source_ids, "tier": self.tier,
                "page_ids": self.page_ids, "n_pages": len(self.page_ids), "chars": len(body),
                "packet_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "page_text_sha256": {p: hashlib.sha256(t.encode("utf-8")).hexdigest()
                                     for p, t in self.page_texts.items()}}


def _chunks(rows: list[dict], texts: dict[str, str]) -> list[list[dict]]:
    """Balanced chunks of one source's pages: ≤ MAX_PAGES pages and ≤ MAX_CHARS characters each."""
    n = len(rows)
    nums = {r["page_id"]: n_numbers(texts[r["page_id"]]) for r in rows}
    k = max(1, math.ceil(n / MAX_PAGES), math.ceil(sum(len(texts[r["page_id"]]) for r in rows) / MAX_CHARS),
            math.ceil(sum(nums.values()) / MAX_NUMBERS))
    size = math.ceil(n / k)
    out, cur, chars, numbers = [], [], 0, 0
    for r in rows:
        t, u = len(texts[r["page_id"]]), nums[r["page_id"]]
        if cur and (len(cur) >= size or chars + t > MAX_CHARS or numbers + u > MAX_NUMBERS):
            out.append(cur)
            cur, chars, numbers = [], 0, 0
        cur.append(r)
        chars += t
        numbers += u
    if cur:
        out.append(cur)
    return out


def group_pages(rows: list[dict], texts: dict[str, str]) -> list[list[dict]]:
    """Per source (sources in order, pages by index): balanced chunks; chunks shorter than MIN_PAGES are pooled
    into mixed packets, a source's pages kept together."""
    by_src: dict[str, list[dict]] = {}
    for r in rows:
        by_src.setdefault(r["source_id"], []).append(r)
    full, small = [], []
    for src in sorted(by_src):
        for ch in _chunks(sorted(by_src[src], key=lambda r: int(r["page_index"])), texts):
            (full if len(ch) >= MIN_PAGES else small).append(ch)
    small.sort(key=lambda ch: (min(r["tier"] for r in ch), ch[0]["source_id"]))
    mixed, cur, chars, numbers = [], [], 0, 0
    for ch in small:
        t = sum(len(texts[r["page_id"]]) for r in ch)
        u = sum(n_numbers(texts[r["page_id"]]) for r in ch)
        if cur and (len(cur) + len(ch) > MAX_PAGES or chars + t > MAX_CHARS or numbers + u > MAX_NUMBERS):
            mixed.append(cur)
            cur, chars, numbers = [], 0, 0
        cur += ch
        chars += t
        numbers += u
    if cur:
        mixed.append(cur)
    return full + mixed


def packet_id(pages: list[dict], mixed_no: int | None = None) -> str:
    srcs = {r["source_id"] for r in pages}
    if len(srcs) == 1 and mixed_no is None:
        return f"{pages[0]['source_id']}_p{int(pages[0]['page_index']):04d}-{int(pages[-1]['page_index']):04d}"
    return f"MIX-{mixed_no:03d}_{pages[0]['source_id']}"


def to_json(p: Packet) -> str:
    return json.dumps({"manifest": p.manifest(), "headers": p.headers, "page_texts": p.page_texts,
                       "context": p.context}, ensure_ascii=False)
