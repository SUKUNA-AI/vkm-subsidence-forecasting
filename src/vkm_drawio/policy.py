"""Leakage policy for the ``public`` root (diagrams are committed to PUBLIC).

Rejected anywhere in a diagram (page names, labels, tooltips, links, custom data, styles):

* absolute machine paths (Windows drive or UNC paths, POSIX home/mount/service prefixes) and the fragments of
  ``vkm_world.governance.leakage``;
* private, carrier-grade-NAT and link-local IPv4 addresses (hosts are named by role, never by address);
* secret-looking tokens and ``password=…``-style assignments;
* embedded raster data (``image=data:…`` styles — e.g. crops of private figures);
* ``file:``/``javascript:``/``data:`` links (a draw.io page link ``data:page/id,…`` is allowed).

Only the rule and the location are reported, never the offending text.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator

from vkm_drawio.xmlio import Diagram

try:  # the shared guard's fragments (vkm_world is the base package of this repository)
    from vkm_world.governance.leakage import FORBIDDEN_PATH_FRAGMENTS, MACHINE_PATH_FRAGMENTS
except ImportError:  # pragma: no cover - vkm_world is always installed with this package
    FORBIDDEN_PATH_FRAGMENTS, MACHINE_PATH_FRAGMENTS = (), ()

RULES: dict[str, re.Pattern[str]] = {
    "HOST_PATH_WINDOWS": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    "HOST_PATH_UNC": re.compile(r"\\\\[A-Za-z0-9._$-]+\\"),
    "HOST_PATH_POSIX": re.compile(r"(?:^|[^\w.$~-])/(?:home|Users|root|mnt|srv|tmp|media|opt|var/lib)/"),
    "PRIVATE_IPV4": re.compile(
        r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}|169\.254(?:\.\d{1,3}){2})(?![\d.])"),
    "SECRET": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
        r"|\bhf_[A-Za-z0-9]{20,}|\bsk-[A-Za-z0-9_-]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
        r"|(?i:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*(?![$<{*%])[^\s;&]{6,}"),
    "EMBEDDED_IMAGE_DATA": re.compile(r"(?:^|;)\s*image=data:", re.IGNORECASE),
    "UNSAFE_LINK": re.compile(r"^\s*(?:file:|javascript:|vbscript:|data:(?!page/id,))", re.IGNORECASE),
}
_FRAGMENTS = tuple(FORBIDDEN_PATH_FRAGMENTS) + tuple(MACHINE_PATH_FRAGMENTS)


def _element_strings(el: ET.Element, where: str) -> Iterator[tuple[str, str]]:
    for key, value in el.attrib.items():
        yield f"{where} {el.tag}@{key}", value
    if el.text and el.text.strip():
        yield f"{where} {el.tag} text", el.text
    for child in el:
        yield from _element_strings(child, where)


def diagram_strings(diagram: Diagram) -> Iterator[tuple[str, str, str]]:
    """(location, field kind, text) for every string of the diagram."""
    for page in diagram.pages:
        where = f"page {page.id!r}"
        yield where, "name", page.name
        for key, value in page.model_attrs.items():
            yield f"{where} model@{key}", "attr", value
        for cell in page.cells:
            cw = f"{where} cell {cell.id!r}"
            if cell.value:
                yield cw, "value", cell.value
            if cell.style:
                yield cw, "style", cell.style
            for key, value in {**cell.cell_attrs, **cell.wrapper_attrs}.items():
                yield f"{cw} @{key}", "link" if key == "link" else "attr", value
            if cell.geometry is not None:
                for key, value in cell.geometry.other_attrs.items():
                    yield f"{cw} geometry@{key}", "attr", value
                for child in cell.geometry.other_children:
                    for loc, text in _element_strings(child, cw):
                        yield loc, "attr", text
            for child in cell.extra_children:
                for loc, text in _element_strings(child, cw):
                    yield loc, "attr", text


def policy_problems(diagram: Diagram) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for where, kind, text in diagram_strings(diagram):
        for rule, pattern in RULES.items():
            if rule == "EMBEDDED_IMAGE_DATA" and kind != "style":
                continue
            if rule == "UNSAFE_LINK" and kind != "link":
                continue
            if pattern.search(text):
                problems.append({"where": where, "rule": rule})
        if any(fragment in text for fragment in _FRAGMENTS):
            problems.append({"where": where, "rule": "PRIVATE_PATH_FRAGMENT"})
    return problems
