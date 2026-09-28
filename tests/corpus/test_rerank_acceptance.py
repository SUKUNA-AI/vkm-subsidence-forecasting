"""Acceptance script and EDGE infra of the rerankers (agent F): stdlib-only script, constants in sync, compose pins."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = ROOT / "src" / "vkm_corpus" / "retrieval" / "acceptance.py"
COMPOSE = ROOT / "infra" / "edge" / "compose.yml"


def test_acceptance_uses_only_the_standard_library():
    """It runs on EDGE with the system python3, without the project installed."""
    stdlib = set(sys.stdlib_module_names)
    imported = set()
    for node in ast.walk(ast.parse(ACCEPTANCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported - stdlib - {"__future__"} == set()


def test_acceptance_constants_match_the_contract():
    pytest.importorskip("pydantic")
    from vkm_corpus.retrieval import acceptance as A
    from vkm_corpus.retrieval import models as M

    assert (A.MAX_TEXT_CANDIDATES, A.MAX_VISUAL_CANDIDATES, A.TEXT_TOKEN_BUDGET, A.MAX_IMAGE_BYTES, A.HEADER_TOKEN) == (
        M.MAX_TEXT_CANDIDATES, M.MAX_VISUAL_CANDIDATES, M.TEXT_TOKEN_BUDGET, M.MAX_IMAGE_BYTES, M.HEADER_TOKEN)


def test_latency_helpers():
    from vkm_corpus.retrieval.acceptance import lat_summary, pct

    assert pct([], 0.5) is None
    s = lat_summary([10.0, 20.0, 30.0, 40.0, 1000.0])
    assert s["n"] == 5 and s["p50"] == 30.0 and s["max"] == 1000.0


def test_compose_llama_arguments_match_the_contract():
    text = COMPOSE.read_text(encoding="utf-8")
    args = re.findall(r"^\s+- \"?([^\"\n]+)\"?$", text, flags=re.M)

    def arg(flag):
        return args[args.index(flag) + 1]

    assert arg("--image-max-tokens") == "768" and arg("--image-min-tokens") == "4"
    assert arg("-ub") == "512" and arg("-c") == "1024" and arg("-np") == "1" and arg("--pooling") == "last"
    assert arg("--host") == "127.0.0.1" and arg("--port") == "18083" and arg("--fit") == "off"
    assert "--embeddings" in args and "--no-warmup" in args and "--no-webui" in args
    assert "careerops-reranker" not in re.sub(r"^#.*$", "", text, flags=re.M)   # the text service is not managed here
    assert "18082" in text                                                      # …only called on loopback
