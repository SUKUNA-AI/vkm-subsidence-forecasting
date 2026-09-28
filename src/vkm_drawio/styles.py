"""draw.io style strings (``"shape=note;rounded=1;"``) and label helpers — deterministic forms only.

Canonical style: bare tokens (``ellipse``, ``text``, ``swimlane``) first in their original order, then ``key=value``
pairs sorted by key, each followed by ``;``. A repeated key keeps its last value; ``key=`` with an empty value is kept
(draw.io uses it to clear an inherited value).
"""
from __future__ import annotations

import html
import re

# node presets: short names an agent can use instead of raw style strings
NODE_PRESETS: dict[str, str] = {
    "box": "rounded=0;whiteSpace=wrap;html=1;",
    "rounded": "rounded=1;whiteSpace=wrap;html=1;",
    "ellipse": "ellipse;whiteSpace=wrap;html=1;",
    "cylinder": "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;size=12;",
    "note": "shape=note;whiteSpace=wrap;html=1;backgroundOutline=1;size=14;",
    "document": "shape=document;whiteSpace=wrap;html=1;boundedLbl=1;",
    "hexagon": "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;size=0.2;",
    "actor": "shape=umlActor;verticalLabelPosition=bottom;verticalAlign=top;html=1;",
    "text": "text;html=1;align=left;verticalAlign=middle;whiteSpace=wrap;",
    "container": ("rounded=0;whiteSpace=wrap;html=1;container=1;collapsible=0;verticalAlign=top;align=left;"
                  "spacingLeft=8;spacingTop=4;dashed=1;"),
    "swimlane": "swimlane;whiteSpace=wrap;html=1;startSize=26;container=1;collapsible=0;",
}
EDGE_PRESETS: dict[str, str] = {
    "arrow": "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=classic;",
    "dashed": "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=classic;dashed=1;",
    "plain": "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=none;",
    "both": "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;endArrow=classic;startArrow=classic;",
    "straight": "rounded=0;html=1;endArrow=classic;",
}
DEFAULT_NODE_PRESET = "rounded"
DEFAULT_EDGE_PRESET = "arrow"

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")


def parse_style(style: str | None) -> tuple[list[str], dict[str, str]]:
    """Split a style string into bare tokens (order kept, duplicates dropped) and a key → value map."""
    bare: list[str] = []
    pairs: dict[str, str] = {}
    for raw in (style or "").split(";"):
        token = raw.strip()
        if not token:
            continue
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.strip()
            if key:
                pairs[key] = value.strip()
        elif token not in bare:
            bare.append(token)
    return bare, pairs


def format_style(bare: list[str], pairs: dict[str, str]) -> str:
    parts = list(bare) + [f"{k}={pairs[k]}" for k in sorted(pairs)]
    return "".join(f"{p};" for p in parts)


def canonical_style(style: str | None) -> str:
    return format_style(*parse_style(style))


def merge_style(base: str | None, override: str | None) -> str:
    """``override`` wins per key; bare tokens are united (base order first)."""
    b_bare, b_pairs = parse_style(base)
    o_bare, o_pairs = parse_style(override)
    bare = b_bare + [t for t in o_bare if t not in b_bare]
    return format_style(bare, {**b_pairs, **o_pairs})


def set_style_keys(style: str | None, changes: dict[str, str | None]) -> str:
    """Set (value) or remove (``None``) individual keys."""
    bare, pairs = parse_style(style)
    for key, value in changes.items():
        if not _KEY.match(key):
            raise ValueError(f"invalid style key {key!r}")
        if value is None:
            pairs.pop(key, None)
        else:
            if ";" in value:
                raise ValueError(f"style value for {key!r} may not contain ';'")
            pairs[key] = value
    return format_style(bare, pairs)


def style_flag(style: str | None, key: str) -> bool:
    return parse_style(style)[1].get(key, "0") not in ("", "0")


def is_html(style: str | None) -> bool:
    return style_flag(style, "html")


def text_to_value(text: str, html_label: bool) -> str:
    """Plain label text → draw.io ``value``. For HTML labels (``html=1``) special characters are escaped and new
    lines become ``<br>``; plain labels are stored as they are (new lines render as line breaks)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not html_label:
        return text
    return html.escape(text, quote=False).replace("\n", "<br>")


_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_END = re.compile(r"</(?:div|p|li|tr|h[1-6])\s*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def value_to_text(value: str | None, html_label: bool) -> str:
    """draw.io ``value`` → readable plain text (tags dropped, entities decoded) for agents and policy checks."""
    if not value:
        return ""
    if not html_label:
        return value
    text = _BR.sub("\n", value)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub("", text)
    return html.unescape(text).strip("\n")
