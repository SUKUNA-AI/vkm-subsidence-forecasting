"""Formula layer of the navigation layer (NAV §3): context, symbols, textual references, parameter candidates.

Rules only (no LLM, no model), over the canonical snapshot in DuckDB:

* ``formula_context`` — the printed equation number of a formula (a FORMULA_NUMBER block beside a DISPLAY formula; a
  number closing a text-layer block inside/beside it; ``\\tag{}`` or a trailing ``(3.2)`` in the LaTeX; else the
  row's ``equation_label``), the text block before it, the «где …» block(s) after it (same page or the top of the
  next page), the host block of an INLINE formula and the section (via N1's ``section_pages``);
* ``formula_symbols`` — normalised identifiers of the LaTeX (``\\sigma_{1}`` → ``σ_1``, ``\\dot{\\varepsilon}`` →
  ``ε̇``) with the side of ``=``, and the definitions parsed from the «где» list («σ — напряжение, МПа») linked to
  them after normalising both sides (LaTeX ↔ Unicode, Cyrillic/Latin look-alikes); symbols live inside a source;
* ``formula_refs`` — textual references («по формуле (3.2)», «подставляя (2.1) в (2.4)») resolved to the formula with
  that number in the same source (same section first, then the nearest preceding in reading order);
* ``formula_parameters`` — parameter-value candidates («n = 4,5», «A = 2·10⁻⁵ 1/сут») in the where-block, the next
  text block or an assignment formula itself.

Everything is navigation (``AUTO_EXTRACTED_UNREVIEWED``): no formula semantics, law or parameter value is asserted.
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from vkm_corpus.navigation import ids as nav_ids

RULE_VERSION = nav_ids.RULE_VERSIONS["formulas"]
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"

# ================================================================================================= LaTeX symbols
_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ", "varkappa": "κ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο", "pi": "π", "varpi": "π", "rho": "ρ", "varrho": "ρ",
    "sigma": "σ", "varsigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ",
    "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω", "varGamma": "Γ", "varDelta": "Δ", "varTheta": "Θ",
    "varLambda": "Λ", "varPhi": "Φ", "varPsi": "Ψ", "varOmega": "Ω", "ell": "ℓ", "hbar": "ħ",
}
_UNI_FOLD = str.maketrans({"ϵ": "ε", "ϕ": "φ", "ϑ": "θ", "ϱ": "ρ", "ϰ": "κ", "ς": "σ", "ϖ": "π", "µ": "μ"})
_ACCENTS = {
    "dot": "\u0307", "ddot": "\u0308", "hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0304",
    "tilde": "\u0303", "widetilde": "\u0303", "vec": "\u20d7", "overrightarrow": "\u20d7", "check": "\u030c",
    "breve": "\u0306", "acute": "\u0301", "grave": "\u0300", "mathring": "\u030a",
}
_FONT_KEEP = {"mathrm", "mathit", "mathbf", "mathsf", "mathtt", "boldsymbol", "bm", "mathcal", "mathscr", "mathfrak",
              "pmb", "rm", "bf", "it", "cal", "mathnormal", "mathbfit", "bold"}
_FONT_TEXT = {"text", "textrm", "textit", "textbf", "mbox", "hbox", "textnormal", "textup", "textsf"}
_FUNCS = {
    "exp", "ln", "log", "lg", "lb", "sin", "cos", "tan", "tg", "cot", "ctg", "sec", "csc", "cosec", "sinh", "cosh",
    "tanh", "coth", "sh", "ch", "th", "cth", "arcsin", "arccos", "arctan", "arctg", "arcctg", "arccot", "max", "min",
    "sup", "inf", "lim", "liminf", "limsup", "det", "dim", "ker", "deg", "arg", "gcd", "Pr", "mod", "bmod", "pmod",
    "sgn", "sign", "const", "grad", "div", "rot", "curl", "Re", "Im", "tr", "Tr", "diag", "erf", "erfc", "var",
    "Var", "cov", "Cov", "sn", "cn", "dn", "si", "ci", "Ei", "li", "argmin", "argmax", "lcm", "Ker", "rank",
}
_RELATIONS = {"=", "\\approx", "\\simeq", "\\equiv", "\\cong", "≈", "≡", "\\coloneqq", "\\triangleq", "\\doteq",
              "\\eqqcolon"}
_LINE_BREAKS = {"\\\\", "\\cr", "\\newline"}
_DELIM_CMDS = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr",
               "Biggl", "Biggr", "middle"}
_SKIP_ARG_CMDS = {"tag", "label", "eqref", "ref", "hspace", "vspace", "phantom", "hphantom", "vphantom", "notag"}
_PLAIN_CMD = {
    "cdot": "·", "times": "×", "circ": "°", "prime": "′", "infty": "∞", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤",
    "leqslant": "≤", "geq": "≥", "ge": "≥", "geqslant": "≥", "approx": "≈", "sim": "~", "ne": "≠", "neq": "≠",
    "dots": "…", "ldots": "…", "cdots": "…", "div": "÷", "partial": "∂", "nabla": "∇", "sqrt": "√", "ast": "*",
    "degree": "°", "prime\\prime": "″", "to": "→", "rightarrow": "→", "in": "∈", "sum": "Σ", "int": "∫",
    "lg": "lg", "ln": "ln", "log": "log", "exp": "exp", "sin": "sin", "cos": "cos", "tg": "tg", "max": "max",
    "min": "min", "lim": "lim", "quad": " ", "qquad": " ", "cdotp": "·", "colon": ":", "mid": "|", "vert": "|",
}
_LTX_TOK = re.compile(r"\\[A-Za-z]+\*?|\\.|\s+|.", re.S)


def _is_letter(t: str) -> bool:
    return len(t) == 1 and t.isalpha()


def _is_cyr(c: str) -> bool:
    return "\u0400" <= c <= "\u04ff"


class _Atom:
    __slots__ = ("kind", "text", "marks", "primes", "sub", "sup", "kids", "font")

    def __init__(self, kind: str, text: str = "", kids: list[_Atom] | None = None):
        self.kind = kind          # id | grp | op | func | word
        self.text = text
        self.marks = ""
        self.primes = 0
        self.sub: list[_Atom] | None = None
        self.sup: list[_Atom] | None = None
        self.kids = kids
        self.font = ""


def _single_id(atoms: list[_Atom] | None) -> _Atom | None:
    """The only identifier of a group (``{\\sigma}``), else None."""
    if not atoms:
        return None
    real = [a for a in atoms if not (a.kind == "op" and a.text.isspace())]
    if len(real) != 1:
        return None
    a = real[0]
    if a.kind == "id":
        return a
    if a.kind == "grp" and not (a.sub or a.sup or a.primes):
        return _single_id(a.kids)
    return None


class _LatexParser:
    """A small LaTeX-math reader: identifiers with scripts, accents, fonts; everything else is an operator."""

    def __init__(self, latex: str):
        self.toks = _LTX_TOK.findall(latex or "")
        self.i = 0

    def parse(self) -> list[_Atom]:
        out: list[_Atom] = []
        while self.i < len(self.toks):
            out.extend(self._seq())
            if self.i < len(self.toks):          # a stray "}"
                self.i += 1
        return out

    def _peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _skip_ws(self) -> None:
        while self.i < len(self.toks) and self.toks[self.i].isspace():
            self.i += 1

    def _seq(self) -> list[_Atom]:
        atoms: list[_Atom] = []
        while self.i < len(self.toks):
            t = self.toks[self.i]
            if t == "}":
                return atoms
            if t.isspace():
                self.i += 1
                continue
            if t in ("^", "_"):
                self.i += 1
                arg = self._arg()
                if len(arg) == 1 and arg[0].kind == "grp" and not (arg[0].sub or arg[0].sup or arg[0].primes):
                    arg = arg[0].kids or []
                tgt = atoms[-1] if atoms else None
                if tgt is None:
                    tgt = _Atom("op", "")
                    atoms.append(tgt)
                if t == "_":
                    tgt.sub = (tgt.sub or []) + arg
                else:
                    rest = []
                    for a in arg:
                        if a.kind == "op" and a.text in ("\\prime", "′", "'"):
                            tgt.primes += 1
                        elif a.kind == "op" and a.text == "\\prime\\prime":
                            tgt.primes += 2
                        else:
                            rest.append(a)
                    if rest:
                        tgt.sup = (tgt.sup or []) + rest
                continue
            if t == "'":
                self.i += 1
                if atoms:
                    atoms[-1].primes += 1
                continue
            atoms.extend(self._one(single=False))
        return atoms

    def _arg(self) -> list[_Atom]:
        self._skip_ws()
        return self._one(single=True)

    def _eat(self, tok: str) -> None:
        if self._peek() == tok:
            self.i += 1

    def _one(self, single: bool) -> list[_Atom]:
        self._skip_ws()
        if self.i >= len(self.toks):
            return []
        t = self.toks[self.i]
        if t == "}":
            return []
        self.i += 1
        if t == "{":
            kids = self._seq()
            self._eat("}")
            return [_Atom("grp", kids=kids)]
        if t.startswith("\\") and len(t) > 1 and t[1].isalpha():
            return self._command(t[1:])
        if t.startswith("\\"):
            return [_Atom("op", t)]
        if _is_letter(t):
            run = [t]
            if not single:
                while self.i < len(self.toks) and _is_letter(self.toks[self.i]):
                    run.append(self.toks[self.i])
                    self.i += 1
            return self._letters(run)
        return [_Atom("op", t)]

    @staticmethod
    def _letters(run: list[str]) -> list[_Atom]:
        s = "".join(run)
        if len(run) >= 2 and s in _FUNCS:
            return [_Atom("func", s)]
        if len(run) >= 2 and any(_is_cyr(c) for c in run):
            return [_Atom("word", s)]
        if len(run) >= 3 and sum(c.islower() for c in run) >= 2:
            return [_Atom("word", s)]
        return [_Atom("id", c) for c in run]

    def _command(self, name: str) -> list[_Atom]:
        base = name.rstrip("*")
        if base in _GREEK:
            return [_Atom("id", _GREEK[base])]
        if base in _ACCENTS:
            arg = self._arg()
            one = _single_id(arg)
            if one is not None:
                one.marks += _ACCENTS[base]
                return [one]
            return [_Atom("grp", kids=arg)]
        if base == "mathbb":
            self._arg()
            return [_Atom("op", "\\mathbb")]
        if base == "operatorname":
            return [_Atom("func", _flat(self._arg()))]
        if base in _FONT_TEXT:
            return [_Atom("word", _flat(self._arg()))]
        if base in _FONT_KEEP:
            return self._font(base, self._arg())
        if base == "begin":
            env = _flat(self._arg()).rstrip("*")
            if env in ("array", "tabular"):
                self._arg()                               # column specification
            return [_Atom("op", "\\begin")]
        if base == "end":
            self._arg()
            return [_Atom("op", "\\end")]
        if base in _DELIM_CMDS:
            self._skip_ws()
            if self.i < len(self.toks):
                d = self.toks[self.i]
                self.i += 1
                return [_Atom("op", d)]
            return []
        if base in _SKIP_ARG_CMDS:
            if base != "notag":
                self._arg()
            return []
        if base in _FUNCS:
            return [_Atom("func", base)]
        if base in ("ast", "star"):
            return [_Atom("op", "*")]
        return [_Atom("op", "\\" + base)]

    @staticmethod
    def _font(name: str, arg: list[_Atom]) -> list[_Atom]:
        if arg and all(a.kind in ("id", "word", "func") and not (a.sub or a.sup) for a in arg):
            s = "".join(a.text for a in arg)
            if s in _FUNCS:
                return [_Atom("func", s)]
            if len(arg) == 1 and arg[0].kind == "id":
                arg[0].font = name
                return arg
            if name == "mathrm" and len(s) == 2 and s[0] == "d" and s[1].isalpha():
                return [_Atom("op", "d"), _Atom("id", s[1])]            # \mathrm{d q}: a differential
            if name != "mathrm" or (s.isascii() and s.isupper() and len(s) <= 3):
                return [_Atom("id", c) for c in s if c.isalpha()]       # product of upright letters
            return [_Atom("word", s)]
        if len(arg) == 1 and arg[0].kind == "grp":
            return _LatexParser._font(name, arg[0].kids or [])
        if name == "mathrm" and any(a.kind == "op" and a.text in ("/", "\\cdot", "·") for a in arg):
            return [_Atom("word", _flat(arg))]                          # a unit: \mathrm{m / s}
        return arg


def _flat(atoms: list[_Atom] | None) -> str:
    """Compact text of a (sub/super)script or a font argument: ``{i j}`` → ``ij``, ``{\\mathrm{P , v}}`` → ``P,v``."""
    out = []
    for a in atoms or ():
        if a.kind == "id":
            out.append(a.text.translate(_UNI_FOLD) + a.marks + "′" * a.primes)
        elif a.kind in ("word", "func"):
            out.append(a.text + "′" * a.primes)
        elif a.kind == "grp":
            out.append(_flat(a.kids) + "′" * a.primes)
        else:
            t = a.text
            if len(t) == 1 and (t.isdigit() or t in ",.-+()/*|:<>=!"):
                out.append(t)
            elif t in ("−", "–"):
                out.append("-")
            elif t.startswith("\\") and t[1:] in _PLAIN_CMD and t[1:] not in ("quad", "qquad"):
                out.append(_PLAIN_CMD[t[1:]])
            elif t.startswith("\\") and t[1:] in _GREEK:
                out.append(_GREEK[t[1:]])
            out.append("′" * a.primes)
        if a.sub:
            out.append("_" + _flat(a.sub))
        if a.sup:
            out.append("^" + _flat(a.sup))
    return re.sub(r"\s+", "", "".join(out))


def _starts_with_id(a: _Atom) -> bool:
    if a.kind == "id":
        return True
    if a.kind == "grp" and a.kids:
        return _starts_with_id(a.kids[0])
    return False


def _ids_in(atoms: list[_Atom] | None) -> bool:
    return any(a.kind == "id" or (a.kind == "grp" and _ids_in(a.kids)) for a in atoms or ())


def _symbol_of(a: _Atom) -> tuple[str, tuple[str, ...], list[_Atom]]:
    """(symbol, alternative forms, exponent atoms to walk) of an identifier atom."""
    base = a.text.translate(_UNI_FOLD) + a.marks
    primes = "′" * a.primes
    star = ""
    sub = _flat(a.sub) if a.sub else ""
    sup_label = ""
    exps: list[_Atom] = []
    alts: list[str] = []
    if a.sup:
        s = _flat(a.sup)
        sup_ids = [x for x in a.sup if x.kind == "id"]
        if s in ("*", "**", "∗"):
            star = "*"
        elif s.startswith("(") and s.endswith(")") and len(s) <= 8:
            sup_label = s
        elif not _ids_in(a.sup):
            pass                                                  # a numeric power: dropped
        elif len(a.sup) == 1 and len(sup_ids) == 1 and not sup_ids[0].sub and not sup_ids[0].sup:
            if a.marks or a.sub:                                  # ε̇^c, ε̄_pl^f: a label
                sup_label = s
            else:                                                 # σ^n: a power, n is a symbol of its own
                exps = a.sup
        elif all(x.kind in ("id", "word") for x in a.sup) and len(s) >= 2:
            sup_label = s                                         # f^{HB}, c^{eff}
        else:
            exps = a.sup
    core = base + primes + star + ("_" + sub if sub else "")
    sym = core + ("^" + sup_label if sup_label else "")
    if exps and len(exps) == 1 and exps[0].kind == "id":
        alts.append(core + "^" + _flat(exps))
    if sup_label and len(sup_label) == 1:
        alts.append(core)
    return sym, tuple(alts), exps


def _emit(a: _Atom, seq: list[_Atom], k: int, side: str, out: list[tuple[str, str, tuple[str, ...]]]) -> None:
    if a.kind == "grp":
        one = _single_id(a.kids)
        if one is not None and (a.sub or a.sup or a.primes):
            merged = _Atom("id", one.text)
            merged.marks, merged.primes = one.marks, one.primes + a.primes
            merged.sub = (one.sub or []) + (a.sub or []) or None
            merged.sup = (one.sup or []) + (a.sup or []) or None
            _emit(merged, seq, k, side, out)
            return
        for j, c in enumerate(a.kids or ()):
            _emit(c, a.kids, j, side, out)
        if a.sup and _ids_in(a.sup):
            for j, c in enumerate(a.sup):
                _emit(c, a.sup, j, side, out)
        return
    if a.kind != "id":
        return
    nxt = seq[k + 1] if k + 1 < len(seq) else None
    if a.text == "d" and not (a.sub or a.sup or a.marks or a.primes) and nxt is not None and _starts_with_id(nxt):
        return                                                    # a differential: d x, \mathrm{d} t
    if a.text == "e" and a.sup and not a.sub and (_ids_in(a.sup) or "-" in _flat(a.sup)):
        for j, c in enumerate(a.sup):                             # Euler's e: exp(...)
            _emit(c, a.sup, j, side, out)
        return
    sym, alts, exps = _symbol_of(a)
    out.append((sym, side, alts))
    for j, c in enumerate(exps):
        _emit(c, exps, j, side, out)


def latex_symbol_occurrences(latex: str | None) -> list[tuple[str, str, tuple[str, ...]]]:
    """Every identifier of a LaTeX formula in order: (symbol, side LHS/RHS/NONE, alternative forms)."""
    atoms = _LatexParser(latex or "").parse()
    lines: list[list[_Atom]] = [[]]
    for a in atoms:
        if a.kind == "op" and a.text in _LINE_BREAKS:
            lines.append([])
        else:
            lines[-1].append(a)
    out: list[tuple[str, str, tuple[str, ...]]] = []
    for line in lines:
        has_rel = any(a.kind == "op" and a.text in _RELATIONS for a in line)
        side = "LHS" if has_rel else "NONE"
        for k, a in enumerate(line):
            if a.kind == "op" and a.text in _RELATIONS:
                side = "RHS"
                continue
            _emit(a, line, k, side, out)
    return out


def latex_symbols(latex: str | None) -> dict[str, dict[str, Any]]:
    """Distinct symbols of a formula: {symbol: {role, n, alts}} with role LHS > RHS > UNKNOWN."""
    res: dict[str, dict[str, Any]] = {}
    rank = {"LHS": 2, "RHS": 1, "NONE": 0}
    for sym, side, alts in latex_symbol_occurrences(latex):
        cur = res.setdefault(sym, {"side": "NONE", "n": 0, "alts": set()})
        cur["n"] += 1
        cur["alts"].update(alts)
        if rank[side] > rank[cur["side"]]:
            cur["side"] = side
    for v in res.values():
        v["role"] = {"LHS": "LHS", "RHS": "RHS"}.get(v.pop("side"), "UNKNOWN")
    return res


def latex_symbol_list(latex: str) -> list[str] | None:
    """Symbols of a math span that is only a list of symbols (``$P, p$``, ``$a_{i}$``), else None."""
    atoms = _LatexParser(latex).parse()
    syms: list[str] = []
    depth = 0
    for a in atoms:
        if a.kind == "op" and a.text in ("(", "["):
            if not syms:
                return None
            depth += 1
            continue
        if a.kind == "op" and a.text in (")", "]"):
            depth -= 1
            continue
        if depth > 0:
            continue
        if a.kind == "op" and a.text in (",", ";", "\\dots", "\\ldots", "\\cdots", "…", ".", "\\,", "\\;", "\\ "):
            continue
        one = a if a.kind == "id" else (_single_id(a.kids) if a.kind == "grp" else None)
        if one is None:
            return None
        if a.kind == "grp":
            merged = _Atom("id", one.text)
            merged.marks, merged.primes = one.marks, one.primes + a.primes
            merged.sub, merged.sup = (one.sub or []) + (a.sub or []) or None, (one.sup or []) + (a.sup or []) or None
            one = merged
        syms.append(_symbol_of(one)[0])
    return syms or None


# ================================================================================================= symbol keys
_BASE_FOLD = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "У": "Y", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "п": "n", "и": "u",
    "ѕ": "s", "і": "i", "ј": "j",
})
_LOOSE_FOLD = str.maketrans({
    "π": "n", "п": "n", "κ": "k", "к": "k", "τ": "t", "т": "t", "ρ": "p", "р": "p", "ν": "v", "υ": "u", "ο": "o",
    "о": "o", "α": "a", "а": "a", "е": "e", "χ": "x", "х": "x", "у": "y", "с": "c", "м": "m", "н": "h", "в": "b",
    "ι": "i", "і": "i", "г": "r", "и": "u", "ε": "e", "ш": "w", "П": "n", "К": "k", "Т": "t", "Р": "p", "О": "o",
    "А": "a", "Е": "e", "Х": "x", "У": "y", "С": "c", "М": "m", "Н": "h", "В": "b", "Г": "r", "И": "u",
})
_SUPER_MAP = str.maketrans({"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7",
                            "⁸": "8", "⁹": "9", "⁻": "-", "⁺": "+"})


def _split_base(sym: str) -> tuple[str, str]:
    if not sym:
        return "", ""
    i = 1
    while i < len(sym) and unicodedata.combining(sym[i]):
        i += 1
    return sym[:i], sym[i:]


def symbol_key(sym: str | None, *, loose: bool = False) -> str:
    """Matching key of a normalised symbol: look-alike base letters folded to Latin, braces/spaces/commas dropped;
    ``loose`` also folds Cyrillic/Greek look-alikes in indices and ignores their case (``ε_π`` ~ ``ε_п``)."""
    s = unicodedata.normalize("NFC", (sym or "").translate(_UNI_FOLD))
    s = re.sub(r"[{}\s]", "", s).replace("'", "′").replace("’", "′")
    base, rest = _split_base(s)
    base = base.translate(_BASE_FOLD)
    rest = rest.replace(",", "")
    if loose:
        rest = rest.translate(_LOOSE_FOLD).lower().replace("_", "").replace("^", "")
    return base + rest


def plain_symbol(text: str | None) -> str | None:
    """Normalise a symbol written as text (``σ1``, ``Qвмп``, ``𝑄𝑗``, ``σ_{1}``, ``ε̇``) to the LaTeX-side form."""
    s = (text or "").strip()
    s = re.sub(r"^[\s(\[«\"]+|[\s,;:.)\]»\"]+$", "", s)
    if not s:
        return None
    if "\\" in s or "{" in s or "^" in s:
        syms = latex_symbol_list(s)
        return syms[0] if syms and len(syms) == 1 else None
    s = unicodedata.normalize("NFKC", s.translate(_SUPER_MAP)).translate(_UNI_FOLD)
    if "_" in s:
        head, _, tail = s.partition("_")
        head_sym = plain_symbol(head)
        tail = tail.strip("{}")
        return f"{head_sym}_{tail}" if head_sym and tail and len(tail) <= 10 else head_sym
    if not s[0].isalpha():
        return None
    base, rest = _split_base(s)
    deco = ""
    j = 0
    while j < len(rest) and rest[j] in "′'’*∗″":
        deco += "′′" if rest[j] == "″" else ("*" if rest[j] in "*∗" else "′")
        j += 1
    rest = rest[j:].strip(".")
    if len(rest) > 10 or " " in rest:
        return None
    return base + deco + ("_" + rest if rest else "")


# ================================================================================================= numbers
_NUM_BODY = r"\d{1,3}(?:\s*[.:\-–]\s*\d{1,3}){0,3}"
_NUMBER_TEXT = re.compile(r"^\s*[(（ð]?\s*(" + _NUM_BODY + r")\s*([a-zа-яё]?)\s*(['′’]?)\s*[)）Þ]?\s*[.,]?\s*$", re.I)
_TRAIL_NUMBER = re.compile(r"[(（ð]\s*(" + _NUM_BODY + r")\s*([a-zа-яё]?)\s*(['′’]?)\s*[)）Þ]\s*[.,]?\s*$", re.I)
_TAG_RX = re.compile(r"\\tag\s*\*?\s*\{([^{}]+)\}")
_LATEX_TRAIL_NUM = re.compile(r"(?:\\q?quad|\\[,;:! ]|~|\s)*\(\s*((?:\d\s*){1,3}(?:[.\-]\s*(?:\d\s*){1,3}){0,3})\s*"
                              r"([a-zа-я]?)\s*\)\s*[.,]?\s*$", re.I)
_SUFFIX_FOLD = str.maketrans({"а": "a", "б": "b", "в": "v", "е": "e", "с": "c", "о": "o", "р": "p", "х": "x"})


def number_key(body: str | None, suffix: str = "", prime: str = "") -> str | None:
    """Normalised equation number: ``(3.2)`` → ``3.2``, ``(5-99)`` → ``5.99``, ``(2.1а)`` → ``2.1a``."""
    if not body:
        return None
    b = re.sub(r"\s+", "", body)
    b = re.sub(r"[.:\-–]+", ".", b).strip(".")
    if not b or not b[0].isdigit():
        return None
    return b + (suffix or "").lower().translate(_SUFFIX_FOLD) + ("'" if prime else "")


def parse_number_text(text: str | None, *, trailing: bool = False, parens: bool = False) -> tuple[str, str] | None:
    """(key, as printed) of a printed equation number: a whole number block or, with ``trailing``, a number that
    closes a text-layer line (``ð1:10Þ`` of Springer fonts included); ``parens`` requires both parentheses."""
    s = (text or "").strip()
    if not s:
        return None
    m = (_TRAIL_NUMBER.search(s) if trailing else _NUMBER_TEXT.match(s))
    if not m:
        return None
    if parens and not (re.match(r"[(（ð]", m.group(0).strip()) and re.search(r"[)）Þ][.,]?$", m.group(0).strip())):
        return None
    key = number_key(m.group(1), m.group(2), m.group(3))
    if key is None:
        return None
    raw = m.group(0).strip().replace("ð", "(").replace("Þ", ")").replace("（", "(").replace("）", ")")
    return key, raw


def latex_number(latex: str | None) -> tuple[str, str] | None:
    """Number written inside the LaTeX: ``\\tag{3.2}`` or a trailing ``\\qquad (3.2)``."""
    if not latex:
        return None
    m = _TAG_RX.search(latex)
    if m:
        inner = m.group(1).strip()
        mm = _NUMBER_TEXT.match(inner)
        if mm:
            key = number_key(mm.group(1), mm.group(2), mm.group(3))
            if key:
                return key, f"({inner})"
    m = _LATEX_TRAIL_NUM.search(latex)
    if m and m.start() > 0:
        key = number_key(m.group(1), m.group(2))
        if key:
            printed = re.sub(r"\s+", "", m.group(1)) + m.group(2)
            return key, f"({printed})"
    return None


# ================================================================================================= text helpers
_MATH_RX = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$|\\\((.+?)\\\)", re.S)
_WHERE_RX = re.compile(r"^\s*(где|здесь|в\s+котор(?:ой|ом|ых)|where|here|in\s+which)(?=[\s,:;$]|$)[\s,:]*", re.I)
_EN_MARKERS = {"where", "here", "in which"}


def where_marker(text: str | None) -> str | None:
    m = _WHERE_RX.match(text or "")
    return re.sub(r"\s+", " ", m.group(1).lower()) if m else None


def latex_to_plain(latex: str | None) -> str:
    """Readable text of a short LaTeX span (units, descriptions): ``\\mathrm{c m}^{3}`` → ``cm^3``."""
    s = latex or ""
    s = s.replace("\\%", "%").replace("\\,", " ").replace("\\;", " ").replace("\\!", "").replace("\\ ", " ")
    s = s.replace("{,}", ",")
    for _ in range(2):
        s = re.sub(r"\\[dtc]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", s)
    for _ in range(2):
        s = re.sub(r"\\(?:mathrm|mathit|mathbf|mathsf|mathtt|text|textrm|textit|textbf|mbox|operatorname|boldsymbol"
                   r"|bm|mathcal|mathbb)\s*\{([^{}]*)\}", lambda m: re.sub(r"\s+", "", m.group(1)), s)
    s = re.sub(r"\\([A-Za-z]+)(?![A-Za-z])", lambda m: _GREEK.get(m.group(1), "\\" + m.group(1)), s)
    s = re.sub(r"\\(" + "|".join(_ACCENTS) + r")\s*\{\s*([^{}\s\\])\s*\}",
               lambda m: m.group(2) + _ACCENTS[m.group(1)], s)
    s = re.sub(r"\\([A-Za-z]+)", lambda m: _PLAIN_CMD.get(m.group(1), " "), s)
    s = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: "^" + re.sub(r"\s+", "", m.group(1)), s)
    s = re.sub(r"_\s*\{([^{}]*)\}", lambda m: "_" + re.sub(r"\s+", "", m.group(1)), s)
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    s = re.sub(r"(?<=\d)\s*([.,])\s*(?=\d)", r"\1", s)
    s = re.sub(r"\s*\^\s*", "^", s)
    s = re.sub(r"\s*_\s*", "_", s)
    return re.sub(r"\s+", " ", s).strip()


def _mask_math(text: str) -> tuple[str, list[str]]:
    spans: list[str] = []

    def rep(m: re.Match) -> str:
        spans.append(m.group(1) or m.group(2) or m.group(3) or "")
        return f" \x01{len(spans) - 1}\x02 "

    return _MATH_RX.sub(rep, text), spans


def _unmask_plain(s: str, spans: list[str]) -> str:
    s = re.sub(r"\x01(\d+)\x02", lambda m: latex_to_plain(spans[int(m.group(1))]), s)
    return re.sub(r"\s+", " ", s).strip()


def to_plain_text(text: str | None) -> str:
    """Block text with math spans rendered as plain text, superscripts as ``^``, hyphenation joined."""
    t = _dehyphen(text or "")
    t = re.sub(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+", lambda m: "^" + m.group(0).translate(_SUPER_MAP), t)
    masked, spans = _mask_math(t)
    return _unmask_plain(masked, spans)


def _dehyphen(t: str) -> str:
    t = re.sub(r"[\u00ad¬]\s*", "", t)
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)


def _stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    return w if len(w) <= 4 else w[: max(4, len(w) - 2)]


def definition_key(description: str | None, n_words: int = 6) -> str | None:
    """Loose key of a definition text (lower case, ё → е, crude stems of the first words) for concept matching."""
    words = re.findall(r"[^\W\d_]{2,}", (description or "").lower().replace("ё", "е"))
    return " ".join(_stem(w) for w in words[:n_words]) or None


# ================================================================================================= units
_UNIT_WORDS = {
    "м", "мм", "см", "дм", "км", "мкм", "нм", "m", "mm", "cm", "dm", "km", "µm", "μm", "um", "nm",
    "с", "сек", "мин", "ч", "час", "сут", "сутки", "суток", "год", "года", "лет", "мес", "s", "sec", "min", "h", "hr",
    "d", "day", "days", "yr", "year", "years", "кг", "г", "т", "мг", "тс", "кгс", "kg", "g", "t", "mg", "tf",
    "kgf", "н", "кн", "мн", "гн", "n", "kn", "mn", "па", "кпа", "мпа", "гпа", "pa", "kpa", "mpa", "gpa", "бар",
    "bar", "атм", "atm", "ат", "дж", "кдж", "мдж", "j", "kj", "mj", "вт", "квт", "мвт", "w", "kw", "mw", "к", "k",
    "c", "°c", "град", "градус", "градусы", "deg", "рад", "rad", "моль", "mol", "л", "мл", "l", "ml", "гц", "hz", "ом",
    "ohm", "доли", "доля", "ед", "единицы", "единиц", "отн", "безразмерная", "безразмерный", "безразмерная величина",
    "б", "р", "раз", "мпа^-1", "mhz", "khz", "ghz", "мгц", "кгц", "ггц", "мкс", "ms", "мс", "сm", "см3", "m3",
    "м3", "м2", "руб", "тыс", "млн", "гпа^-1",
}


_LAT2CYR = str.maketrans({"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у", "k": "к", "m": "м",
                          "r": "г", "t": "т", "h": "н", "b": "в", "n": "п", "u": "и"})


def is_unit(text: str | None) -> bool:
    t = unicodedata.normalize("NFKC", (text or "").translate(_SUPER_MAP)).strip().strip(".")
    if not t or len(t) > 24:
        return False
    if re.search(r"[^\w\s/·⋅*×^\-−.,()°%µμΩ]", t):
        return False
    alph = re.findall(r"[^\W\d_]+", t)
    if not alph:
        return t in ("%", "‰") or t.startswith("°")
    return all(a.lower() in _UNIT_WORDS or a.lower().translate(_LAT2CYR) in _UNIT_WORDS for a in alph)


def _split_unit(desc: str) -> tuple[str, str | None]:
    m = re.search(r"[,;]\s*([^,;]{1,32})$", desc)
    if m and is_unit(m.group(1)):
        return desc[: m.start()].rstrip(" ,;"), m.group(1).strip().rstrip(".")
    m = re.search(r"\(\s*([^()]{1,24})\s*\)$", desc)
    if m and is_unit(m.group(1)):
        return desc[: m.start()].rstrip(" ,;"), m.group(1).strip()
    m = re.search(r"\s(?:в|in)\s+([^\s,;]{1,16})$", desc)
    if m and is_unit(m.group(1)):
        return desc[: m.start()].rstrip(" ,;"), m.group(1).strip()
    return desc, None


# ================================================================================================= definitions
@dataclass
class Definition:
    symbols: list[str]
    symbols_raw: str
    description: str
    unit: str | None
    block_id: str | None = None


_WTOK = re.compile(r"\x01\d+\x02|\n|[;:,]|[—–−]|\((?:[^()\n]{0,24})\)|[^\s;:,\x01\x02()]+|[()]")
_DASHES = {"—", "–", "−", "-", "--"}
_EN_SEP = {"is", "are", "denotes", "denote", "represents", "represent", "means"}
_STOP = {"и", "а", "в", "на", "по", "из", "от", "до", "за", "не", "но", "же", "ли", "то", "их", "ее", "её", "он",
         "мы", "вы", "об", "со", "во", "ко", "при", "для", "как", "это", "так", "или", "the", "of", "to", "in", "on",
         "at", "by", "as", "is", "be", "an", "or", "if", "it", "and", "are", "we", "that", "its", "а", "т", "е", "см",
         "рис", "табл", "тем", "там", "где", "здесь", "all", "for", "with", "from", "this", "not", "но"}


def _is_symbol_token(t: str) -> bool:
    s = unicodedata.normalize("NFKC", t).strip(".,")
    if not s or len(s) > 8 or not s[0].isalpha():
        return False
    if s.lower() in _STOP and len(s) > 1:
        return False
    if len(s) <= 2:
        return True
    if any(c.isdigit() for c in s) or any(c in "_*′'^’" for c in s):
        return True
    if "\u0370" <= s[0] <= "\u03ff":
        return True
    if s.isupper() and len(s) <= 4:
        return True
    if s[0].isascii() and not s[1:].isascii():
        return True
    if s[0].isupper() and len(s) <= 4 and not s[1:].isupper() and s[0].isascii() != s[1].isascii():
        return True
    return False


def _merge_plain(tokens: list[str]) -> list[str]:
    """Symbol(s) from plain tokens written without separators: native text puts an index on its own line
    (``ВМП`` then ``Q`` → ``Q_ВМП``); a lone preposition is dropped."""
    toks = [unicodedata.normalize("NFKC", t).strip(".,") for t in tokens]
    toks = [t for t in toks if t]
    if len(toks) == 1:
        s = plain_symbol(toks[0])
        return [s] if s else []
    singles = [i for i, t in enumerate(toks) if len(t) == 1 and t.isalpha()]
    if len(singles) == 1:
        base = toks[singles[0]]
        label = "".join(t for i, t in enumerate(toks) if i != singles[0])
        s = plain_symbol(base)
        return [f"{s}_{label}"] if s and label else ([s] if s else [])
    out = []
    for t in toks:
        s = plain_symbol(t)
        if s:
            out.append(s)
    return out


def _symbol_part(toks: list[tuple[str, int, int]], k: int, spans: list[str]) -> tuple[int | None, list[str], str]:
    """Walk back from the separator at ``k`` over symbol chunks; (first token index, symbols, raw) or (None, …)."""
    chunks: list[tuple[str, Any, bool, int]] = []    # (kind, value, joined_to_right, token index)
    sep_pending = True
    j = k - 1
    while j >= 0:
        t = toks[j][0]
        if t == "\n":
            j -= 1
            continue
        if t in (";", ":", ".", "(", ")"):
            break
        if t == ",":
            if not chunks:
                break
            sep_pending = True
            j -= 1
            continue
        low = t.lower()
        if chunks and low in ("а", "a"):
            break                                     # «…, а v — их скорости»: «а» opens a new clause
        if chunks and low in ("и", "and", "or", "или"):
            sep_pending = True
            j -= 1
            continue
        if t.startswith("(") and t.endswith(")"):
            if chunks:
                break
            j -= 1
            continue
        if t.startswith("\x01"):
            syms = latex_symbol_list(spans[int(t[1:-1])])
            if not syms:
                break
            chunks.append(("ph", syms, not sep_pending, j))
        elif _is_symbol_token(t):
            chunks.append(("tok", t, not sep_pending, j))
        else:
            break
        sep_pending = False
        j -= 1
    if not chunks:
        return None, [], ""
    chunks.reverse()
    symbols: list[str] = []
    run: list[str] = []
    for n, (kind, val, joined, _idx) in enumerate(chunks):
        if kind == "ph":
            if run:
                symbols.extend(_merge_plain(run))
                run = []
            symbols.extend(val)
            continue
        run.append(val)
        if not (joined and n + 1 < len(chunks) and chunks[n + 1][0] == "tok"):
            symbols.extend(_merge_plain(run))
            run = []
    if run:
        symbols.extend(_merge_plain(run))
    first = chunks[0][3]
    raw_parts = []
    for i in range(first, k):
        t = toks[i][0]
        raw_parts.append(latex_to_plain(spans[int(t[1:-1])]) if t.startswith("\x01") else t)
    raw = re.sub(r"\s+", " ", " ".join(raw_parts)).strip()
    seen: list[str] = []
    for s in symbols:
        if s and s not in seen:
            seen.append(s)
    return first, seen, raw


def parse_definitions(text: str | None) -> list[Definition]:
    """Definitions of a «где …» list: ``где σ — напряжение, МПа; ε — деформация`` → [(σ, напряжение, МПа),
    (ε, деформация, None)]. Symbols in ``$…$`` math or as plain text; English ``where σ is the stress`` too."""
    if not text:
        return []
    marker = where_marker(text)
    english = marker in _EN_MARKERS if marker else bool(re.search(r"\b(is|are|denotes)\b", text)) and \
        not re.search(r"[а-яё]", text, re.I)
    body = _WHERE_RX.sub("", _dehyphen(text), count=1)
    masked, spans = _mask_math(body)
    masked = re.sub(r"\s*([—–−])\s*", r" \1 ", masked).replace("--", " — ")
    masked = re.sub(r"\.(?=\s|$)", " . ", masked)
    toks = [(m.group(0), m.start(), m.end()) for m in _WTOK.finditer(masked)]
    seps = [k for k, (t, _, _) in enumerate(toks) if t in _DASHES or (english and t.lower() in _EN_SEP)]
    items: list[tuple[int, int, list[str], str]] = []
    for k in seps:
        if items and k <= items[-1][1]:
            continue
        start, syms, raw = _symbol_part(toks, k, spans)
        if start is None or not syms:
            continue
        if items and start <= items[-1][1]:
            continue
        items.append((start, k, syms, raw))
    out: list[Definition] = []
    for n, (start, k, syms, raw) in enumerate(items):
        end_tok = items[n + 1][0] if n + 1 < len(items) else len(toks)
        a = toks[k][2]
        b = toks[end_tok - 1][2] if end_tok - 1 > k else a
        desc = masked[a:b].split(";")[0]
        desc = _unmask_plain(desc, spans)
        desc = re.split(r"\s\.\s+(?=[A-ZА-ЯЁ])|\s\.\s*$", desc)[0]
        desc = re.sub(r"\s\.(\s|$)", r".\1", desc)
        desc = re.sub(r"(\S) -(?=[а-яё])", r"\1-", desc)
        desc = re.sub(r"(?:\s*[,;:.]|\s+(?:и|а|and|or|или))+\s*$", "", desc).strip(" ,;:.")
        desc, unit = _split_unit(desc)
        desc = desc.strip(" ,;:.")
        if english:
            desc = re.sub(r"^(?:the|a|an)\s+", "", desc, flags=re.I)
        if len(desc) < 2 or not re.search(r"[^\W\d_]{2,}", desc) or _POINTER.match(desc):
            continue
        out.append(Definition(symbols=syms, symbols_raw=raw, description=desc[:300], unit=unit))
    return out


_POINTER = re.compile(r"^(?:(?:is|are)\s+)?(?:(?:given|defined|determined|obtained|calculated|expressed)"
                      r"\s+(?:by|in|from|"
                      r"through)|see\b|cf\.|(?:определя|вычисля|находи|задан|приведен|см\.)\S*\s*(?:по|из|в|формул|"
                      r"выражени|уравнени|\())", re.I)
_DEF_START = re.compile(r"^\s*(?:\$[^$]{1,60}\$|[^\s,;:]{1,10})(?:\s*(?:,|и|and)\s*(?:\$[^$]{1,60}\$|[^\s,;:]{1,10}))*"
                        r"\s*[—–−]\s*\S", re.I)


def looks_like_definition_item(text: str | None) -> bool:
    return bool(_DEF_START.match(text or ""))


# ================================================================================================= parameters
_NUM = (r"[-−–]?\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?(?:\s*[·⋅×*]\s*10\s*\^\s*\(?\s*[-−–]?\s*\d+\s*\)?"
        r"|\s*[eE][-−+]?\d+)?|10\s*\^\s*\(?\s*[-−–]?\s*\d+\s*\)?")
_PARAM_RX = re.compile(
    r"(?<![\w.′'’^])(?P<sym>(?:[A-Za-zΑ-Ωα-ω][\u0300-\u036f]?[′'’*]*(?:_\{?[\w.,′]{1,8}\}?|\d{1,2}(?![\d.,])"
    r"|[а-яё]{1,4}(?![а-яё]))?)|(?:[А-ЯЁа-яё][\u0300-\u036f]?[′'’*]*(?:_\{?[\w.,′]{1,8}\}?|\d{1,2}(?![\d.,]))?))"
    r"\s*(?P<op>=|≈|≅)\s*(?P<v1>" + _NUM + r")(?:\s*(?:\.\.\.|…|[–—÷]|-(?=\s*\d))\s*(?P<v2>" + _NUM + r"))?"
    r"(?=(?P<rest>[^\n;]{0,40}))")
_UNIT_HEAD = re.compile(r"^\s*([^\s,;]+(?:\s*[/·⋅]\s*[^\s,;]+)*)")


@dataclass
class ParameterCandidate:
    symbol: str
    symbol_raw: str
    value_text: str
    value: float | None
    value_min: float | None
    value_max: float | None
    unit: str | None


def parse_number(text: str | None) -> float | None:
    t = re.sub(r"[\s\u00a0\u202f]", "", text or "").replace("−", "-").replace("–", "-").replace(",", ".")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)(?:[·⋅×*]10\^\(?(-?\d+)\)?|[eE]([-+]?\d+))?", t)
    try:
        if m:
            mant = float(m.group(1))
            exp = m.group(2) or m.group(3)
            return mant * 10 ** int(exp) if exp else mant
        m = re.fullmatch(r"10\^\(?(-?\d+)\)?", t)
        return float(10 ** int(m.group(1))) if m else None
    except (OverflowError, ValueError):
        return None


def parse_parameters(text: str | None) -> list[ParameterCandidate]:
    """Parameter-value candidates ``SYM = NUMBER [unit]`` of a text block (math spans included)."""
    plain = to_plain_text(text)
    out: list[ParameterCandidate] = []
    for m in _PARAM_RX.finditer(plain):
        rest = m.group("rest") or ""
        if plain[: m.start()].rstrip().endswith("(") and rest.lstrip().startswith(")"):
            continue                                          # an index range «(i = 1,4)»
        if re.match(r"\s*[+*/^(=]|[A-Za-zα-ωΑ-Ω]", rest) and not re.match(r"\s*[·⋅×]\s*10", rest):
            continue                                          # an expression, not a value
        if re.match(r"\s*[-−]\s*[A-Za-zα-ω\d(]", rest):
            continue
        nxt = re.match(r"\s+([A-Za-zα-ωΑ-Ω][\w′]*)", rest)
        if nxt and not is_unit(nxt.group(1)) and (len(nxt.group(1)) == 1 or "_" in nxt.group(1)):
            continue                                          # a product «2 V_i», «2 y», not a value
        sym = plain_symbol(m.group("sym"))
        if not sym:
            continue
        v1, v2 = m.group("v1"), m.group("v2")
        unit = None
        um = _UNIT_HEAD.match(rest)
        if um and is_unit(um.group(1).rstrip(".")):
            unit = um.group(1).rstrip(".")
        n1 = parse_number(v1)
        n2 = parse_number(v2) if v2 else None
        vt = (v1 + ("–" + v2 if v2 else "")).strip()
        if n1 is None:
            continue
        if re.match(r"\s*(?:,\s*(?:\d|…|⋯|\.\.\.)|(?:or|или|и|and)\s+\d)", rest) or \
                (re.fullmatch(r"\d+,\d+", v1.strip()) and re.match(r"\s*,", rest)):
            continue                                          # an enumeration «n = 1, 2, …», «j = 1 or 2»
        if sym in ("i", "j", "k", "l") and not v2 and float(n1).is_integer():
            continue                                          # an index
        if not v2 and unit is None and n1 in (0.0, 1.0, -1.0):
            continue                                          # a condition «t = 0», «α = 1», not a value
        if v2 and n2 is not None:
            out.append(ParameterCandidate(sym, m.group("sym"), vt, None, min(n1, n2), max(n1, n2), unit))
        else:
            out.append(ParameterCandidate(sym, m.group("sym"), vt, n1, None, None, unit))
    return out


# ================================================================================================= references
_CUE_NOUN = re.compile(r"(формул\w*|выражени\w*|уравнени\w*|соотношени\w*|равенств\w*|неравенств\w*|зависимост\w*|"
                       r"систем\w*|услови\w*|тождеств\w*|закон\w*|eqs?\.?|equations?|formula[es]?|formulas|"
                       r"expressions?|relations?|conditions?|inequalit\w*|identit\w*|law)\s*(?:№\s*)?$", re.I)
_CUE_PREP = re.compile(r"(?:^|[\s(,])(из|в|во|по|с\s+уч[её]том|согласно|подставляя|подставив|подстановкой|используя|"
                       r"сравнивая|учитывая|на\s+основании|в\s+силу|см\.?|вместо|from|in|into|by|using|substituting|"
                       r"combining|comparing|see|cf\.?)\s*$", re.I)
_CUE_NOUN_ONLY = re.compile(r"^(?:формул|выражени|уравнени|соотношени|равенств|неравенств|зависимост|систем|услови|"
                            r"тождеств|закон|eq|equation|formula|expression|relation|condition|inequalit|identit|law)",
                            re.I)
_LIST_TAIL = re.compile(r"\)\s*(?:,\s*(?:и|and)?|и|или|and|or|[–—-])\s*$", re.I)
_REF_PAREN = re.compile(r"\(\s*(" + _NUM_BODY + r")\s*([a-zа-я]?)\s*(['′]?)\s*\)", re.I)
_REF_BARE = re.compile(r"(формул\w*|выражени\w*|уравнени\w*|соотношени\w*|eqs?\.|equations?|formulas?)\s+(?:№\s*)?"
                       r"(\d{1,3}(?:\.\d{1,3}){1,3})(?![\d.]*\d)", re.I)
_SUBST = re.compile(r"подстав|подстано|внос[яи]|внес[яеёш]|substitut|plugging|inserting", re.I)
_DERIV = re.compile(r"получ|следует|вытека|выводит|приходим|находим|имеем|откуда|отсюда|дифференцир|интегрир|решая|"
                    r"складыва|вычита|сравнива|исключ|преобраз|перепиш|сводит|приводит к|lead[s]? to|yield|gives|"
                    r"obtain|we get|follows|deriv|integrat|differentiat|solving|combining|comparing|adding|"
                    r"subtracting|eliminat|rearrang|reduces? to|results? in", re.I)


def ref_type_of(sentence: str) -> str:
    if _SUBST.search(sentence):
        return "SUBSTITUTION"
    if _DERIV.search(sentence):
        return "DERIVATION_HINT"
    return "MENTION"


_ABBR = re.compile(r"(?:\b(?:eqs?|fig|figs|sect|ch|vol|no|cf|e\.g|i\.e|см|рис|табл|гл|ср|стр|т|е|п|пп|ур|ф-ле|ф-лы)"
                   r"|\b[A-ZА-Я])$", re.I)


def _sentence(text: str, a: int, b: int) -> str:
    bounds = [m.start() for m in re.finditer(r"\.\s|;|\n\n", text)
              if not (text[m.start()] == "." and _ABBR.search(text[max(0, m.start() - 6): m.start()]))]
    left = max([x for x in bounds if x < a], default=-1)
    right = min([x for x in bounds if x >= b], default=len(text))
    return text[left + 1: right]


@dataclass
class RefMention:
    key: str
    number_text: str
    cue: str | None
    start: int
    end: int
    ref_type: str
    compound: bool


def _springer_parens(t: str) -> str:
    """``ð1:10Þ`` (parentheses and a colon of a Springer math font in the text layer) → ``(1.10)``, same length."""
    if "ð" not in t:
        return t
    return re.sub(r"ð(\s*\d{1,3}(?:\s*:\s*\d{1,3}){0,3}\s*[a-z]?\s*)Þ",
                  lambda m: "(" + m.group(1).replace(":", ".") + ")", t)


def find_ref_mentions(text: str | None) -> list[RefMention]:
    """Candidate formula references of a text block (before resolution against the numbers of the source)."""
    t = _springer_parens(text or "")
    out: list[RefMention] = []
    prev_end, prev_cue = -10, None
    for m in _REF_PAREN.finditer(t):
        key = number_key(m.group(1), m.group(2), m.group(3))
        if key is None:
            continue
        pre = t[max(0, m.start() - 48): m.start()]
        cue = None
        cm = _CUE_NOUN.search(pre)
        if cm:
            cue = cm.group(1)
        elif prev_cue is not None and m.start() - prev_end <= 12 and \
                _LIST_TAIL.search(t[max(0, prev_end - 1): m.start()]):
            cue = prev_cue
        else:
            pm = _CUE_PREP.search(pre)
            if pm:
                cue = re.sub(r"\s+", " ", pm.group(1))
        compound = "." in key
        if cue is None and (not compound or "–" in m.group(1)):
            continue                                          # a bare simple number or an en-dash range
        if cue is None and not t[m.end():].strip(" .,;"):
            continue                                          # an equation label closing a formula line
        out.append(RefMention(key, m.group(0), cue.lower() if cue else None, m.start(), m.end(),
                              ref_type_of(_sentence(t, m.start(), m.end())), compound))
        prev_end, prev_cue = m.end(), (cue.lower() if cue else "(list)")
    for m in _REF_BARE.finditer(t):
        if any(r.start <= m.start(2) < r.end for r in out):
            continue
        key = number_key(m.group(2))
        if key:
            out.append(RefMention(key, m.group(2), m.group(1).lower(), m.start(2), m.end(2),
                                  ref_type_of(_sentence(t, m.start(2), m.end(2))), True))
    return out


# ================================================================================================= build
FLOW_TYPES = {"TEXT", "LIST_ITEM", "ABSTRACT", "HEADING", "TITLE", "CODE"}
CONTEXT_TYPES = {"TEXT", "LIST_ITEM", "ABSTRACT"}
REF_TYPES = {"TEXT", "LIST_ITEM", "ABSTRACT", "CAPTION", "FOOTNOTE"}


class _Blk:
    __slots__ = ("id", "source_id", "page_id", "page_index", "type", "ro", "x0", "y0", "x1", "y1", "geo", "text",
                 "junk")

    def __init__(self, row: tuple):
        (self.id, self.source_id, self.page_id, self.page_index, self.type, self.ro, self.x0, self.y0, self.x1,
         self.y1, space, self.text) = row
        self.page_index = int(self.page_index or 0)
        self.ro = int(self.ro) if self.ro is not None else 0
        self.geo = space == "PAGE_PT_TL" and None not in (self.x0, self.y0, self.x1, self.y1)
        self.text = self.text or ""
        self.junk = False


class _Fml:
    __slots__ = ("id", "source_id", "page_id", "page_index", "kind", "label", "latex", "raw", "x0", "y0", "x1", "y1",
                 "geo", "number", "number_raw", "number_method", "number_block", "extra", "pos", "intro", "next",
                 "host", "where", "section", "symbols", "n_defined", "n_refs_in", "n_params")

    def __init__(self, row: tuple):
        (self.id, self.source_id, self.page_id, self.page_index, self.kind, self.label, self.latex, self.raw,
         self.x0, self.y0, self.x1, self.y1, space) = row
        self.page_index = int(self.page_index or 0)
        self.geo = space == "PAGE_PT_TL" and None not in (self.x0, self.y0, self.x1, self.y1)
        self.kind = self.kind or "UNKNOWN"
        self.number = self.number_raw = self.number_method = self.number_block = None
        self.extra: list[str] = []
        self.pos = None
        self.intro = self.next = self.host = None
        self.where: list[_Blk] = []
        self.section = None
        self.symbols: dict[str, dict[str, Any]] = {}
        self.n_defined = self.n_refs_in = self.n_params = 0

    @property
    def text(self) -> str:
        return self.latex or _raw_math(self.raw)


def _raw_math(raw: str | None) -> str:
    s = (raw or "").strip()
    for rx in (r"^\$\$(.*)\$\$$", r"^\\\[(.*)\\\]$", r"^\\\((.*)\\\)$", r"^\$(.*)\$$"):
        m = re.match(rx, s, re.S)
        if m:
            return m.group(1).strip()
    return s


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _inside_share(b: _Blk, f: _Fml, pad: float = 2.0) -> float:
    area = max(1e-6, (b.x1 - b.x0) * (b.y1 - b.y0))
    return _overlap(b.x0, b.x1, f.x0 - pad, f.x1 + pad) * _overlap(b.y0, b.y1, f.y0 - pad, f.y1 + pad) / area


def _xover(b: _Blk, f: _Fml) -> bool:
    ov = _overlap(b.x0, b.x1, f.x0, f.x1)
    return ov >= min(10.0, 0.25 * max(1.0, min(b.x1 - b.x0, f.x1 - f.x0)))


def _rows(obj: Any) -> list[dict[str, Any]]:
    if obj is None:
        return []
    if hasattr(obj, "to_pylist"):
        return obj.to_pylist()
    if hasattr(obj, "to_dict") and hasattr(obj, "columns"):
        return obj.to_dict("records")
    if isinstance(obj, str):
        import pyarrow.parquet as pq

        return pq.read_table(obj).to_pylist()
    return [dict(r) for r in obj]


class _Sections:
    """Section of a page from N1's ``section_pages`` (the latest-starting section wins on a shared page) and the
    top-level ancestor (chapter) from ``sections`` when given."""

    def __init__(self, section_pages: Any, sections: Any, page_index: dict[str, int]):
        rows = _rows(section_pages)
        start: dict[str, int] = {}
        by_page: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            sid, pid = r.get("section_id"), r.get("page_id")
            if not sid or not pid:
                continue
            by_page[pid].append(sid)
            pi = page_index.get(pid)
            if pi is not None:
                start[sid] = min(start.get(sid, pi), pi)
        self.page: dict[str, str] = {}
        for pid, sids in by_page.items():
            self.page[pid] = max(sorted(set(sids)), key=lambda s: start.get(s, -1))
        parent = {r.get("section_id"): r.get("parent_section_id") for r in _rows(sections) if r.get("section_id")}
        self.top: dict[str, str] = {}
        for sid in set(self.page.values()):
            cur, seen = sid, set()
            while parent.get(cur) and cur not in seen:
                seen.add(cur)
                cur = parent[cur]
            self.top[sid] = cur
        self.enabled = bool(self.page)

    def of(self, page_id: str) -> str | None:
        return self.page.get(page_id)

    def chapter(self, section: str | None) -> str | None:
        return self.top.get(section, section) if section else None


_FORMULA_SQL = """
SELECT f.object_id, f.source_id, f.page_id, p.page_index, f.formula_kind, f.equation_label, f.normalized_latex,
       f.raw_output, f.bbox_x0, f.bbox_y0, f.bbox_x1, f.bbox_y1, f.bbox_space
FROM canonical.formulas f LEFT JOIN canonical.pages p ON p.page_id = f.page_id
{where}
ORDER BY f.source_id, p.page_index, f.bbox_y0, f.object_id
"""
_BLOCK_SQL = """
SELECT b.object_id, b.source_id, b.page_id, p.page_index, b.block_type, b.reading_order, b.bbox_x0, b.bbox_y0,
       b.bbox_x1, b.bbox_y1, b.bbox_space, b.text
FROM canonical.blocks b LEFT JOIN canonical.pages p ON p.page_id = b.page_id
WHERE b.is_primary_layer AND b.source_id IN (SELECT DISTINCT source_id FROM canonical.formulas {fwhere})
ORDER BY b.source_id, p.page_index, b.reading_order, b.object_id
"""


def _load(con: Any, source_ids: Iterable[str] | None) -> tuple[list[_Fml], list[_Blk]]:
    params: list[Any] = []
    fwhere = where = ""
    if source_ids:
        ids_ = sorted(set(source_ids))
        ph = ", ".join("?" for _ in ids_)
        where = f"WHERE f.source_id IN ({ph})"
        fwhere = f"WHERE source_id IN ({ph})"
        params = ids_
    fml = [_Fml(r) for r in con.execute(_FORMULA_SQL.format(where=where), params).fetchall()]
    blk = [_Blk(r) for r in con.execute(_BLOCK_SQL.format(fwhere=fwhere), params).fetchall()]
    return fml, blk


@dataclass
class _Source:
    formulas: list[_Fml] = field(default_factory=list)
    blocks: list[_Blk] = field(default_factory=list)


def _assign_numbers(page_f: list[_Fml], page_b: list[_Blk]) -> None:
    """Equation numbers from FORMULA_NUMBER blocks and number-only / number-closing text-layer blocks beside a
    formula (same page, overlapping y-range, to the right)."""
    disp = [f for f in page_f if f.geo and f.kind == "DISPLAY"]
    inl = [f for f in page_f if f.geo and f.kind != "DISPLAY" and "=" in (f.text or "")]
    if not disp and not inl:
        return
    cands: list[tuple[_Blk, tuple[str, str], str]] = []
    for b in page_b:
        if not b.geo:
            continue
        if b.type == "FORMULA_NUMBER":
            p = parse_number_text(b.text) or parse_number_text(b.text, trailing=True)
            if p:
                cands.append((b, p, "FORMULA_NUMBER_BLOCK"))
        elif b.type in ("TEXT", "CODE") and len(b.text) <= 200:
            p = parse_number_text(b.text, parens=True)
            if p:
                cands.append((b, p, "TEXT_LAYER_NUMBER"))
            elif b.junk:
                p = parse_number_text(b.text, trailing=True, parens=True)
                right_of = any(b.x1 > f.x1 + 5 and _overlap(b.y0, b.y1, f.y0, f.y1) > 0 for f in disp)
                if p and right_of:
                    cands.append((b, p, "TEXT_LAYER_NUMBER"))
    for b, (key, raw), method in sorted(cands, key=lambda c: (c[0].y0, c[0].x0)):
        best, best_score = None, 0.0
        for pool in (disp, inl):
            for f in pool:
                h = max(1.0, b.y1 - b.y0)
                ov = _overlap(b.y0 - 2, b.y1 + 2, f.y0, f.y1)
                cx = (b.x0 + b.x1) / 2
                right = cx >= (f.x0 + f.x1) / 2
                if ov <= 0:
                    gap = min(abs(b.y0 - f.y1), abs(f.y0 - b.y1))
                    if gap > 6 or not right:
                        continue
                    score = 0.1 / (1 + gap)
                else:
                    score = ov / h + (0.5 if right else 0.0) - (0.2 if b.junk and not right else 0.0)
                if pool is inl:
                    score -= 0.5
                    if ov < 0.5 * h:
                        continue
                if score > best_score:
                    best, best_score = f, score
            if best is not None:
                break
        if best is None:
            continue
        if best.number is None:
            best.number, best.number_raw, best.number_method, best.number_block = key, raw, method, b.id
        elif key != best.number and key not in best.extra:
            best.extra.append(key)


def _process_page_geometry(page_f: list[_Fml], page_b: list[_Blk]) -> None:
    disp = [f for f in page_f if f.geo and f.kind == "DISPLAY"]
    for b in page_b:
        if b.geo and b.type not in ("FORMULA_NUMBER",) and disp:
            if max(_inside_share(b, f) for f in disp) >= 0.5:
                b.junk = True
            elif len(b.text) <= 300 and parse_number_text(b.text, trailing=True, parens=True) and any(
                    _overlap(b.y0, b.y1, f.y0 - 3, f.y1 + 3) >= 0.5 * max(1.0, b.y1 - b.y0) for f in disp):
                b.junk = True                     # the text-layer line of a formula with its number
    _assign_numbers(page_f, page_b)
    flow = [b for b in page_b if b.type in FLOW_TYPES and not b.junk]
    for f in page_f:
        if not f.geo:
            continue
        if f.kind == "DISPLAY":
            geo_flow = [b for b in flow if b.geo]
            below = [b.ro for b in geo_flow if b.y0 >= f.y1 - 3 and _xover(b, f)]
            if below:
                f.pos = min(below) - 0.5
            else:
                above = [b.ro for b in geo_flow if b.y1 <= f.y0 + 3 and _xover(b, f)]
                if above:
                    f.pos = max(above) + 0.5
                else:
                    cy = (f.y0 + f.y1) / 2
                    before = [b.ro for b in geo_flow if (b.y0 + b.y1) / 2 < cy]
                    f.pos = (max(before) + 0.5) if before else 0.5
        else:
            best, share = None, 0.0
            area = max(1e-6, (f.x1 - f.x0) * (f.y1 - f.y0))
            for b in flow:
                if not b.geo:
                    continue
                s = _overlap(b.x0 - 2, b.x1 + 2, f.x0, f.x1) * _overlap(b.y0 - 2, b.y1 + 2, f.y0, f.y1) / area
                if s > share:
                    best, share = b, s
            if best is not None and share >= 0.5:
                f.host = best


def _continuation(seq: list[_Blk], i: int, first: _Blk) -> list[_Blk]:
    """Blocks continuing a «где» list: a lone marker takes the next block; items «X — …» are appended."""
    out = [first]
    lone = not _WHERE_RX.sub("", first.text).strip()
    j = i + 1
    while j < len(seq) and len(out) < 6:
        c = seq[j]
        if c.type not in CONTEXT_TYPES or c.page_index - first.page_index > 1:
            break
        if (lone and len(out) == 1) or looks_like_definition_item(c.text):
            out.append(c)
            j += 1
            continue
        break
    return out


def build(con: Any, *, section_pages: Any = None, sections: Any = None, source_ids: Iterable[str] | None = None,
          **_: Any) -> dict[str, Any]:
    """Formula datasets of the snapshot behind ``con`` (DuckDB with the ``canonical`` schema)."""
    import pyarrow as pa

    formulas, blocks = _load(con, source_ids)
    page_index = {f.page_id: f.page_index for f in formulas}
    page_index.update({b.page_id: b.page_index for b in blocks})
    secs = _Sections(section_pages, sections, page_index)
    by_src: dict[str, _Source] = defaultdict(_Source)
    for f in formulas:
        by_src[f.source_id].formulas.append(f)
    for b in blocks:
        by_src[b.source_id].blocks.append(b)

    ctx_rows: list[dict[str, Any]] = []
    sym_rows: list[dict[str, Any]] = []
    ref_rows: list[dict[str, Any]] = []
    par_rows: list[dict[str, Any]] = []
    stats: Counter = Counter()

    for sid in sorted(by_src):
        src = by_src[sid]
        pf: dict[int, list[_Fml]] = defaultdict(list)
        pb: dict[int, list[_Blk]] = defaultdict(list)
        for f in src.formulas:
            pf[f.page_index].append(f)
        for b in src.blocks:
            pb[b.page_index].append(b)
            if b.type in ("TEXT", "CODE") and len(b.text) <= 24 and parse_number_text(b.text, parens=True):
                b.junk = True                         # a printed equation number in the text layer
        for pi, fl in pf.items():
            _process_page_geometry(fl, pb.get(pi, []))
        # reading sequence of flow blocks of the source
        seq = sorted((b for b in src.blocks if b.type in FLOW_TYPES and not b.junk),
                     key=lambda b: (b.page_index, b.ro, b.id))
        keys = [(b.page_index, b.ro) for b in seq]
        pos_of = {b.id: n for n, b in enumerate(seq)}
        next_groups: dict[str, list[_Fml]] = defaultdict(list)
        for f in src.formulas:
            f.section = secs.of(f.page_id)
            if f.number is None:
                ln = latex_number(f.raw) or latex_number(f.latex)
                if ln:
                    f.number, f.number_raw, f.number_method = ln[0], ln[1], "LATEX_TAG"
            if f.number is None and f.label:
                p = parse_number_text(f.label)
                if p:
                    f.number, f.number_raw, f.number_method = p[0], f.label, "EQUATION_LABEL"
            f.symbols = latex_symbols(f.text)
            if f.kind != "DISPLAY" or f.pos is None:
                continue
            k = bisect.bisect_left(keys, (f.page_index, f.pos))
            if k > 0:
                b = seq[k - 1]
                if f.page_index - b.page_index <= 1 and b.type in CONTEXT_TYPES:
                    f.intro = b
            if k < len(seq):
                b = seq[k]
                if b.page_index - f.page_index <= 1 and b.type in CONTEXT_TYPES:
                    f.next = b
                    next_groups[b.id].append(f)
        # «где» blocks, definitions, symbols
        where_cache: dict[str, tuple[list[_Blk], list[Definition]]] = {}
        for nb_id, group in next_groups.items():
            nb = seq[pos_of[nb_id]]
            marker = where_marker(nb.text)
            if not marker:
                continue
            wblocks = _continuation(seq, pos_of[nb_id], nb)
            defs: list[Definition] = []
            for wb in wblocks:
                for d in parse_definitions(wb.text):
                    d.block_id = wb.id
                    defs.append(d)
            if marker in ("здесь", "here") and not defs:
                continue                              # «Здесь …»/«Here …» without a definition is ordinary prose
            where_cache[nb_id] = (wblocks, defs)
            group.sort(key=lambda f: (f.page_index, f.y0 or 0.0))
            for gi, f in enumerate(group):
                f.where = wblocks
                _link_definitions(f, defs, last=(gi == len(group) - 1))
        # numbers of the source: key -> [(page_index, y, formula)]
        numbered: dict[str, list[tuple[int, float, _Fml]]] = defaultdict(list)
        for f in src.formulas:
            for key in [f.number, *f.extra] if f.number else []:
                numbered[key].append((f.page_index, f.y0 or 0.0, f))
        for v in numbered.values():
            v.sort(key=lambda t: (t[0], t[1]))
        intro_of: dict[str, _Fml] = {}
        for f in src.formulas:
            if f.intro is not None and f.intro.id not in intro_of:
                intro_of[f.intro.id] = f
        # references
        for b in src.blocks:
            if b.type not in REF_TYPES or b.junk or "(" not in b.text and not re.search(r"\d\.\d", b.text):
                continue
            seen: dict[str, dict[str, Any]] = {}
            for rm in find_ref_mentions(b.text):
                stats["ref_mentions"] += 1
                cands = numbered.get(rm.key)
                if not cands:
                    stats["ref_unresolved"] += 1
                    continue
                target, how = _resolve(cands, b, secs)
                if target is None:
                    stats["ref_unresolved"] += 1
                    continue
                if how.endswith("NEAREST_FOLLOWING") and target.page_index - b.page_index > 3:
                    stats["ref_far_forward"] += 1
                    continue                          # a forward reference far ahead: another article's numbering
                if not rm.compound and not _CUE_NOUN_ONLY.match(rm.cue or "") and (
                        how == "UNIQUE" and abs(b.page_index - target.page_index) > 5
                        or how != "UNIQUE" and b.page_index - target.page_index > 5):
                    stats["ref_simple_far"] += 1
                    continue                          # «из (11)» far from its only (11): steps, items, lists
                if rm.cue is None and target.page_id == b.page_id and target.geo and b.geo and \
                        _overlap(b.y0, b.y1, target.y0 - 3, target.y1 + 3) > 0:
                    stats["ref_label_skipped"] += 1
                    continue
                if target.id in seen:
                    seen[target.id]["n_mentions"] += 1
                    continue
                cit = intro_of.get(b.id)
                row = {
                    "ref_id": nav_ids.formula_ref_id(b.id, target.id), "block_id": b.id, "source_id": sid,
                    "page_id": b.page_id, "formula_id": target.id, "number_text": rm.number_text,
                    "number_key": rm.key, "ref_type": rm.ref_type, "cue": rm.cue, "resolution": how,
                    "n_candidates": len(cands), "n_mentions": 1, "char_start": rm.start,
                    "citing_formula_id": cit.id if cit is not None and cit.id != target.id else None,
                    "rule_version": RULE_VERSION, "review_status": REVIEW_STATUS,
                }
                seen[target.id] = row
                target.n_refs_in += 1
            ref_rows.extend(seen.values())
        # parameters
        for f in src.formulas:
            ctx_blocks: list[tuple[_Blk, str]] = [(w, "WHERE_BLOCK") for w in f.where]
            if f.next is not None and f.next not in f.where:
                ctx_blocks.append((f.next, "NEXT_BLOCK"))
            found: list[tuple[ParameterCandidate, str | None, str]] = []
            for blk, kind in ctx_blocks:
                for pc in parse_parameters(blk.text):
                    found.append((pc, blk.id, kind))
            if f.symbols and len(f.symbols) == 1 and (f.text or "").count("=") == 1:
                plain_f = latex_to_plain(f.text)
                for pc in parse_parameters("$" + f.text + "$"):
                    if re.match(r"\s*" + re.escape(pc.symbol_raw) + r"\s*=", plain_f):
                        found.append((pc, None, "FORMULA_SELF"))
            keys_f = _key_index(f)
            done = set()
            for pc, bid, kind in found:
                pid = nav_ids.parameter_id(f.id, pc.symbol, pc.value_text)
                if pid in done:
                    continue
                done.add(pid)
                in_f = symbol_key(pc.symbol) in keys_f[0] or symbol_key(pc.symbol, loose=True) in keys_f[1]
                par_rows.append({
                    "parameter_id": pid, "formula_id": f.id, "source_id": sid, "symbol": pc.symbol,
                    "symbol_raw": pc.symbol_raw, "value_text": pc.value_text, "value": pc.value,
                    "value_min": pc.value_min, "value_max": pc.value_max, "unit": pc.unit, "block_id": bid,
                    "in_formula": bool(in_f), "context_kind": kind, "rule_version": RULE_VERSION,
                    "review_status": REVIEW_STATUS,
                })
                f.n_params += 1
        # symbol rows + context rows
        for f in src.formulas:
            for sym, info in f.symbols.items():
                d: Definition | None = info.get("definition")
                sym_rows.append({
                    "formula_id": f.id, "source_id": sid, "symbol": sym, "symbol_key": symbol_key(sym),
                    "symbol_id": nav_ids.symbol_id(sid, sym), "role": info["role"], "n_occurrences": info["n"],
                    "in_formula": info.get("in_formula", True),
                    "definition": d.description if d else None, "unit": d.unit if d else None,
                    "definition_block_id": d.block_id if d else None,
                    "definition_symbol_raw": info.get("def_symbol"),
                    "definition_key": definition_key(d.description) if d else None,
                    "match_method": info.get("match"), "rule_version": RULE_VERSION, "review_status": REVIEW_STATUS,
                })
            text = f.text or ""
            ctx_rows.append({
                "formula_id": f.id, "source_id": sid, "page_id": f.page_id, "page_index": f.page_index,
                "kind": f.kind, "latex_len": len(text), "equation_number": f.number,
                "equation_number_raw": f.number_raw, "number_method": f.number_method,
                "number_block_id": f.number_block, "extra_numbers": list(f.extra), "section_id": f.section,
                "intro_block_id": f.intro.id if f.intro is not None else None,
                "where_block_ids": [w.id for w in f.where], "next_block_id": f.next.id if f.next is not None else None,
                "host_block_id": f.host.id if f.host is not None else None,
                "n_symbols": sum(1 for v in f.symbols.values() if v.get("in_formula", True)),
                "n_defined_symbols": f.n_defined, "n_refs_in": f.n_refs_in, "n_parameters": f.n_params,
                "rule_version": RULE_VERSION, "review_status": REVIEW_STATUS,
            })

    return {
        "formula_context": pa.Table.from_pylist(ctx_rows, schema=FORMULA_CONTEXT_SCHEMA()),
        "formula_symbols": pa.Table.from_pylist(sym_rows, schema=FORMULA_SYMBOLS_SCHEMA()),
        "formula_refs": pa.Table.from_pylist(ref_rows, schema=FORMULA_REFS_SCHEMA()),
        "formula_parameters": pa.Table.from_pylist(par_rows, schema=FORMULA_PARAMETERS_SCHEMA()),
    }


def _key_index(f: _Fml) -> tuple[dict[str, str], dict[str, str]]:
    strict: dict[str, str] = {}
    loose: dict[str, str] = {}
    for sym, info in f.symbols.items():
        if not info.get("in_formula", True):
            continue
        for form in (sym, *sorted(info.get("alts", ()))):
            strict.setdefault(symbol_key(form), sym)
            loose.setdefault(symbol_key(form, loose=True), sym)
    return strict, loose


def _link_definitions(f: _Fml, defs: list[Definition], *, last: bool) -> None:
    strict, loose = _key_index(f)
    for d in defs:
        for ds in d.symbols:
            target, how = strict.get(symbol_key(ds)), "EXACT"
            if target is None:
                target, how = loose.get(symbol_key(ds, loose=True)), "LOOSE"
            if target is not None:
                info = f.symbols[target]
                if info.get("definition") is None:
                    info["definition"], info["def_symbol"], info["match"] = d, ds, how
                    f.n_defined += 1
            elif last and ds not in f.symbols:
                f.symbols[ds] = {"role": "UNKNOWN", "n": 0, "alts": set(), "in_formula": False, "definition": d,
                                 "def_symbol": ds, "match": "NONE"}


def _resolve(cands: list[tuple[int, float, _Fml]], b: _Blk, secs: _Sections) -> tuple[_Fml | None, str]:
    """The formula a reference points to: unique; else same section, same chapter, nearest preceding, following."""
    if len(cands) == 1:
        return cands[0][2], "UNIQUE"
    here = (b.page_index, b.y0 if b.geo else 0.0)

    def nearest(pool: list[tuple[int, float, _Fml]]) -> tuple[_Fml | None, str]:
        before = [c for c in pool if (c[0], c[1]) <= here]
        if before:
            return before[-1][2], "NEAREST_PRECEDING"
        after = [c for c in pool if (c[0], c[1]) > here]
        return (after[0][2], "NEAREST_FOLLOWING") if after else (None, "")

    if secs.enabled:
        s = secs.of(b.page_id)
        if s:
            pool = [c for c in cands if c[2].section == s]
            if pool:
                t, how = nearest(pool)
                return t, "SAME_SECTION_" + how
            ch = secs.chapter(s)
            pool = [c for c in cands if secs.chapter(c[2].section) == ch]
            if pool and ch != s:
                t, how = nearest(pool)
                return t, "SAME_CHAPTER_" + how
    return nearest(cands)


# ================================================================================================= schemas
def FORMULA_CONTEXT_SCHEMA():  # noqa: N802 - a schema constant built lazily (pyarrow is optional at import)
    import pyarrow as pa

    return pa.schema([
        ("formula_id", pa.string()), ("source_id", pa.string()), ("page_id", pa.string()),
        ("page_index", pa.int32()), ("kind", pa.string()), ("latex_len", pa.int32()),
        ("equation_number", pa.string()), ("equation_number_raw", pa.string()), ("number_method", pa.string()),
        ("number_block_id", pa.string()), ("extra_numbers", pa.list_(pa.string())), ("section_id", pa.string()),
        ("intro_block_id", pa.string()), ("where_block_ids", pa.list_(pa.string())), ("next_block_id", pa.string()),
        ("host_block_id", pa.string()), ("n_symbols", pa.int32()), ("n_defined_symbols", pa.int32()),
        ("n_refs_in", pa.int32()), ("n_parameters", pa.int32()), ("rule_version", pa.string()),
        ("review_status", pa.string()),
    ])


def FORMULA_SYMBOLS_SCHEMA():  # noqa: N802
    import pyarrow as pa

    return pa.schema([
        ("formula_id", pa.string()), ("source_id", pa.string()), ("symbol", pa.string()), ("symbol_key", pa.string()),
        ("symbol_id", pa.string()), ("role", pa.string()), ("n_occurrences", pa.int32()), ("in_formula", pa.bool_()),
        ("definition", pa.string()), ("unit", pa.string()), ("definition_block_id", pa.string()),
        ("definition_symbol_raw", pa.string()), ("definition_key", pa.string()), ("match_method", pa.string()),
        ("rule_version", pa.string()), ("review_status", pa.string()),
    ])


def FORMULA_REFS_SCHEMA():  # noqa: N802
    import pyarrow as pa

    return pa.schema([
        ("ref_id", pa.string()), ("block_id", pa.string()), ("source_id", pa.string()), ("page_id", pa.string()),
        ("formula_id", pa.string()), ("number_text", pa.string()), ("number_key", pa.string()),
        ("ref_type", pa.string()), ("cue", pa.string()), ("resolution", pa.string()), ("n_candidates", pa.int32()),
        ("n_mentions", pa.int32()), ("char_start", pa.int32()), ("citing_formula_id", pa.string()),
        ("rule_version", pa.string()), ("review_status", pa.string()),
    ])


def FORMULA_PARAMETERS_SCHEMA():  # noqa: N802
    import pyarrow as pa

    return pa.schema([
        ("parameter_id", pa.string()), ("formula_id", pa.string()), ("source_id", pa.string()),
        ("symbol", pa.string()), ("symbol_raw", pa.string()), ("value_text", pa.string()), ("value", pa.float64()),
        ("value_min", pa.float64()), ("value_max", pa.float64()), ("unit", pa.string()), ("block_id", pa.string()),
        ("in_formula", pa.bool_()), ("context_kind", pa.string()), ("rule_version", pa.string()),
        ("review_status", pa.string()),
    ])


def summarize(tables: dict[str, Any]) -> dict[str, Any]:
    """Counts for the public receipt (ids and numbers only, no corpus text)."""
    ctx = tables["formula_context"].to_pylist()
    sym = tables["formula_symbols"].to_pylist()
    refs = tables["formula_refs"].to_pylist()
    par = tables["formula_parameters"].to_pylist()
    disp = [r for r in ctx if r["kind"] == "DISPLAY"]
    methods = Counter(r["number_method"] for r in ctx if r["number_method"])
    defined = [r for r in sym if r["definition"]]
    concepts: dict[str, set[str]] = defaultdict(set)
    for r in defined:
        if r["definition_key"]:
            concepts[r["definition_key"]].add(r["source_id"])
    return {
        "formulas": len(ctx), "display": len(disp), "inline": sum(1 for r in ctx if r["kind"] == "INLINE"),
        "sources": len({r["source_id"] for r in ctx}),
        "display_with_number": sum(1 for r in disp if r["equation_number"]),
        "all_with_number": sum(1 for r in ctx if r["equation_number"]),
        "number_methods": dict(sorted(methods.items())),
        "display_with_intro": sum(1 for r in disp if r["intro_block_id"]),
        "display_with_where": sum(1 for r in disp if r["where_block_ids"]),
        "inline_with_host": sum(1 for r in ctx if r["kind"] == "INLINE" and r["host_block_id"]),
        "display_with_section": sum(1 for r in disp if r["section_id"]),
        "symbol_rows": len(sym), "symbols_in_formulas": sum(1 for r in sym if r["in_formula"]),
        "symbols_with_definition": sum(1 for r in defined if r["in_formula"]),
        "definitions_not_matched": sum(1 for r in defined if not r["in_formula"]),
        "definitions_with_unit": sum(1 for r in defined if r["unit"]),
        "formulas_with_defined_symbol": len({r["formula_id"] for r in defined if r["in_formula"]}),
        "definition_keys": len(concepts), "definition_keys_in_2plus_sources": sum(1 for v in concepts.values()
                                                                                  if len(v) >= 2),
        "refs": len(refs), "ref_types": dict(sorted(Counter(r["ref_type"] for r in refs).items())),
        "ref_resolution": dict(sorted(Counter(r["resolution"] for r in refs).items())),
        "refs_with_citing_formula": sum(1 for r in refs if r["citing_formula_id"]),
        "formulas_referenced": len({r["formula_id"] for r in refs}),
        "parameters": len(par), "parameters_in_formula": sum(1 for r in par if r["in_formula"]),
        "parameters_with_unit": sum(1 for r in par if r["unit"]),
        "parameter_context": dict(sorted(Counter(r["context_kind"] for r in par).items())),
    }
