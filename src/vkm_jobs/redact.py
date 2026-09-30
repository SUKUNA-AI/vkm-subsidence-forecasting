"""Logical names instead of machine paths, and the public-text checks of receipts.

:class:`Redactor` rewrites known roots (``<VKM_SIM_ROOT>``, ``<MATLAB_ROOT>``, ``<ANSYS_ROOT>``, ``<PUBLIC>``,
``<VKM_WORK>``, ``<HOME>``, ``<TEMP>``) in any spelling (``\\`` or ``/``, doubled backslashes, 8.3 short names, any
letter case on Windows), then masks the account and host names and every remaining absolute path (``<ABS_PATH>``) and
private IPv4 address (``<IP>``). Receipts and job summaries pass through it; :func:`public_text_problems` is the gate
before anything is copied into the PUBLIC tree (same patterns as ``tests/corpus/test_public_hygiene.py``).
"""
from __future__ import annotations

import getpass
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import Any, Mapping

_PATH_CHARS = r"[^\s\"'<>|*?,;()\[\]{}]*"
ABS_WINDOWS = re.compile(r"(?<![A-Za-z0-9_<])[A-Za-z]:[\\/]+" + _PATH_CHARS)
ABS_UNC = re.compile(r"(?<![\\/\w])\\\\[A-Za-z0-9._$-]+\\" + _PATH_CHARS)
ABS_POSIX = re.compile(r"(?<![\w.~/\-<>])/(?:home|Users|root|mnt|media|tmp)/" + _PATH_CHARS)
PRIVATE_IPV4 = re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}"
                          r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?![\d.])")
SECRET = re.compile(r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bghp_[A-Za-z0-9]{20,}|\bhf_[A-Za-z0-9]{20,}"
                    r"|\bsk-[A-Za-z0-9]{20,}|(?i:password)\s*[:=]\s*['\"][^'\"<>{}$]{6,}['\"])")
DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?=\w)")
POSIX_HOST_DIR = re.compile(r"(?<![\w.~/\-])/(?:home|Users|root|mnt|media)/[\w.\-]")
UNC = re.compile(r"(?<![\\\w])\\\\[A-Za-z0-9._-]+\\")
MIN_NAME_LEN = 4            # account / host names shorter than this are not masked (they would hit ordinary words)


def _short_long_variants(path: str) -> set[str]:
    out = {path}
    if os.name == "nt" and os.path.exists(path):
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        for fn in (ctypes.windll.kernel32.GetShortPathNameW, ctypes.windll.kernel32.GetLongPathNameW):
            n = fn(ctypes.c_wchar_p(path), buf, 1024)
            if 0 < n < 1024:
                out.add(buf.value)
    return out


def _root_pattern(path: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.split(r"[\\/]+", path.rstrip("\\/")) if p != ""]
    body = r"[\\/]{1,2}".join(parts)
    if path.startswith(("/", "\\")) and not path.startswith(("\\\\", "//")):
        body = r"[\\/]" + body
    flags = re.IGNORECASE if os.name == "nt" or re.match(r"^[A-Za-z]:", path) else 0
    return re.compile(body + r"(?P<tail>(?:[\\/]{1,2}" + _PATH_CHARS + r")?)(?![\w.-])", flags)


def default_roots(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    roots: dict[str, str] = {}
    for logical, name in (("<VKM_SIM_ROOT>", "VKM_SIM_ROOT"), ("<VKM_WORK>", "VKM_WORK"),
                          ("<VKM_RESOURCES_ROOT>", "VKM_RESOURCES_ROOT"), ("<VKM_DATA_ROOT>", "VKM_DATA_ROOT"),
                          ("<MATLAB_ROOT>", "VKM_MATLAB_ROOT"), ("<ANSYS_ROOT>", "VKM_ANSYS_ROOT"),
                          ("<ANSYS_ROOT>", "AWP_ROOT261")):
        value = env.get(name)
        if value and "${" not in value and value.strip() and logical not in roots:
            roots[logical] = value.strip()
    roots.setdefault("<TEMP>", tempfile.gettempdir())
    roots.setdefault("<HOME>", str(Path.home()))
    return roots


class Redactor:
    """Rewrites machine-specific text to logical names (idempotent)."""

    def __init__(self, roots: Mapping[str, str | Path] | None = None, *, env: Mapping[str, str] | None = None,
                 include_defaults: bool = True) -> None:
        merged: dict[str, str] = {}
        for logical, path in (roots or {}).items():
            if path:
                merged.setdefault(logical, str(path))
        if include_defaults:
            for logical, path in default_roots(env).items():
                merged.setdefault(logical, path)
        pairs: list[tuple[str, str]] = []
        for logical, path in merged.items():
            for variant in _short_long_variants(os.path.abspath(path) if os.path.isabs(path) else path):
                if len(variant.strip("\\/")) >= 3:
                    pairs.append((variant, logical))
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)            # the most specific root wins
        self._roots = [(_root_pattern(v), logical) for v, logical in pairs]
        names = set()
        for name in (_safe(getpass.getuser), _safe(socket.gethostname)):
            if name and len(name) >= MIN_NAME_LEN:
                names.add(name)
        self._names = [re.compile(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", re.IGNORECASE) for n in names]

    def text(self, value: str) -> str:
        if not value:
            return value
        out = value
        for pattern, logical in self._roots:
            out = pattern.sub(lambda m, lg=logical: lg + m.group("tail").replace("\\\\", "/").replace("\\", "/"), out)
        for pattern in self._names:
            out = pattern.sub("<NAME>", out)
        out = ABS_UNC.sub("<ABS_PATH>", out)
        out = ABS_WINDOWS.sub("<ABS_PATH>", out)
        out = ABS_POSIX.sub("<ABS_PATH>", out)
        return PRIVATE_IPV4.sub("<IP>", out)

    def obj(self, value: Any) -> Any:
        """Redact every string (keys too) of a JSON-like value."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(k) if isinstance(k, str) else k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value


def _safe(fn) -> str | None:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - the name is optional
        return None


def public_text_problems(text: str) -> list[str]:
    """Why ``text`` may not enter the PUBLIC tree: host paths, drive letters, UNC, private IPs, secrets, names."""
    problems = []
    for label, pattern in (("windows drive path", DRIVE_PATH), ("posix host path", POSIX_HOST_DIR), ("UNC path", UNC),
                           ("private IPv4", PRIVATE_IPV4), ("secret", SECRET)):
        m = pattern.search(text)
        if m:
            problems.append(f"{label}: …{text[max(0, m.start() - 20):m.end() + 20]}…")
    for name in (_safe(getpass.getuser), _safe(socket.gethostname)):
        if name and len(name) >= MIN_NAME_LEN and re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text,
                                                            re.IGNORECASE):
            problems.append("account or host name of this machine")
    return problems
