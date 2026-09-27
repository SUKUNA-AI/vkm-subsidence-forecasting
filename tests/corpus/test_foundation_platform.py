"""Foundation of the Corpus Platform: dependency direction (CP-01), configuration safety (CP-12), logging, CLI."""
from __future__ import annotations

import ast
import io
import json
import logging
from pathlib import Path

import pytest

from vkm_corpus.config import ConfigError, load_settings
from vkm_corpus.logs import CONTEXT_FIELDS, JsonFormatter, bind

ROOT = Path(__file__).resolve().parents[2]


def test_vkm_world_never_imports_platform_packages():
    offenders = []
    for path in (ROOT / "src" / "vkm_world").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in {"vkm_corpus", "vkm_cad", "vkm_drawio"}:
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}: {name}")
    assert offenders == []


def test_data_root_inside_git_tree_is_refused(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    settings = load_settings({"VKM_DATA_ROOT": str(tmp_path / "repo" / "data")})
    with pytest.raises(ConfigError, match="git working tree"):
        settings.require_data_root()


def test_data_root_inside_resources_is_refused(tmp_path):
    res = tmp_path / "private"
    (res / "00_registry").mkdir(parents=True)
    settings = load_settings({"VKM_DATA_ROOT": str(res / "data"), "VKM_RESOURCES_ROOT": str(res)})
    with pytest.raises(ConfigError, match="inside VKM_RESOURCES_ROOT"):
        settings.require_data_root()


def test_secrets_come_from_files_and_are_redacted(tmp_path):
    secret = tmp_path / "pw"
    secret.write_bytes(b"not-a-real-password\n")
    settings = load_settings({"VKM_NEO4J_PASSWORD_FILE": str(secret), "VKM_DATA_ROOT": str(tmp_path / "d")})
    assert settings.neo4j_password == "not-a-real-password"
    shown = settings.redacted()
    assert shown["neo4j_password"] == "<set>" and "not-a-real-password" not in json.dumps(shown)


def test_unknown_data_role_is_rejected():
    with pytest.raises(ConfigError):
        load_settings({"VKM_DATA_ROLE": "master"})


def test_json_log_record_carries_context():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test"))
    logger = logging.getLogger("vkm.test.json")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    bind(logger, run_id="RUN-X", source_id="VKM-SRC-001").info("page done", extra={"vkm": {"page_id": "p", "n": 3}})
    record = json.loads(stream.getvalue())
    assert record["service"] == "test" and record["run_id"] == "RUN-X" and record["page_id"] == "p"
    assert record["data"] == {"n": 3} and set(CONTEXT_FIELDS) >= {"run_id", "stage", "error_code"}


def test_cli_config_show_runs(capsys, monkeypatch, tmp_path):
    from vkm_corpus import cli

    monkeypatch.setenv("VKM_DATA_ROOT", str(tmp_path / "d"))
    assert cli.main(["config"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pipeline_version"] and out["data_role"] == "producer"
