"""Runtime configuration from the environment (no machine paths in code or committed config).

Roots:

* ``VKM_RESOURCES_ROOT`` — clone of the PRIVATE data repository (raw sources, ``SOURCE_REGISTER.csv``); read only;
* ``VKM_DATA_ROOT`` — runtime data root (canonical Parquet, artifacts, projections, logs, receipts). Its role is
  ``VKM_DATA_ROLE``: ``producer`` (a build/staging area where the pipeline writes) or ``canonical`` (the published
  corpus that projections and the API read). It must never lie inside a git working tree;
* ``VKM_WORK`` — scratch space (optional; defaults to ``$VKM_DATA_ROOT/tmp``).

Secrets are read from ``*_FILE`` variables (or the plain variable) and never logged: ``Settings.redacted()`` is the only
form that leaves the process.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping

DATA_ROLES = ("producer", "canonical")
SECRET_FIELDS = frozenset({"pg_dsn", "neo4j_password", "api_token", "api_write_token", "rerank_token",
                           "embed_token"})


class ConfigError(RuntimeError):
    """Missing or unsafe configuration."""


def _path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _secret(env: Mapping[str, str], name: str) -> str | None:
    """``NAME_FILE`` (file content, stripped) wins over ``NAME``; empty → None."""
    file_value = env.get(f"{name}_FILE", "").strip()
    if file_value:
        try:
            return Path(file_value).expanduser().read_text(encoding="utf-8").strip() or None
        except OSError as exc:
            raise ConfigError(f"cannot read secret file for {name}") from exc
    return env.get(name, "").strip() or None


def _inside_git_tree(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return None


@dataclass(frozen=True)
class Settings:
    resources_root: Path | None
    data_root: Path | None
    data_role: str
    work_root: Path | None
    pg_dsn: str | None
    ocr_url: str | None
    ocr_model: str
    neo4j_uri: str | None
    neo4j_user: str
    neo4j_password: str | None
    neo4j_database: str
    opensearch_url: str | None
    opensearch_index_prefix: str
    rerank_url: str | None
    rerank_token: str | None
    api_url: str | None
    api_token: str | None
    api_write_token: str | None
    log_level: str
    models_dir: Path | None = None  # local model snapshots (GLM-OCR, layout); read-only
    embed_url: str | None = None    # RX580 retrieval service (query embeddings, POST /embed/query)
    embed_token: str | None = None  # its optional bearer token (VKM_EMBED_TOKEN_FILE)

    # ---------------------------------------------------------------- accessors with checks
    def require_data_root(self) -> Path:
        if self.data_root is None:
            raise ConfigError("VKM_DATA_ROOT is not set")
        root = self.data_root.resolve()
        repo = _inside_git_tree(root)
        if repo is not None:
            raise ConfigError("VKM_DATA_ROOT lies inside a git working tree; runtime data must never be committed")
        if self.resources_root is not None and root.is_relative_to(self.resources_root.resolve()):
            raise ConfigError("VKM_DATA_ROOT lies inside VKM_RESOURCES_ROOT")
        return root

    def require_resources_root(self) -> Path:
        if self.resources_root is None:
            raise ConfigError("VKM_RESOURCES_ROOT is not set (clone of the PRIVATE data repository)")
        root = self.resources_root.resolve()
        if not (root / "00_registry" / "SOURCE_REGISTER.csv").is_file():
            raise ConfigError("VKM_RESOURCES_ROOT has no 00_registry/SOURCE_REGISTER.csv")
        return root

    def require_work_root(self) -> Path:
        work = self.work_root.resolve() if self.work_root else self.require_data_root() / "tmp"
        work.mkdir(parents=True, exist_ok=True)
        return work

    def redacted(self) -> dict[str, object]:
        """Printable view: secrets replaced by a presence flag, paths as given."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                out[f.name] = "<set>" if value else None
            else:
                out[f.name] = str(value) if isinstance(value, Path) else value
        return out


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    role = env.get("VKM_DATA_ROLE", "producer").strip() or "producer"
    if role not in DATA_ROLES:
        raise ConfigError(f"VKM_DATA_ROLE must be one of {DATA_ROLES}, got {role!r}")
    return Settings(
        resources_root=_path(env, "VKM_RESOURCES_ROOT"),
        data_root=_path(env, "VKM_DATA_ROOT"),
        data_role=role,
        work_root=_path(env, "VKM_WORK"),
        pg_dsn=_secret(env, "VKM_PG_DSN"),
        ocr_url=env.get("VKM_OCR_URL", "").strip() or None,
        ocr_model=env.get("VKM_OCR_MODEL", "glm-ocr").strip() or "glm-ocr",
        neo4j_uri=env.get("VKM_NEO4J_URI", "").strip() or None,
        neo4j_user=env.get("VKM_NEO4J_USER", "neo4j").strip() or "neo4j",
        neo4j_password=_secret(env, "VKM_NEO4J_PASSWORD"),
        neo4j_database=env.get("VKM_NEO4J_DATABASE", "neo4j").strip() or "neo4j",
        opensearch_url=env.get("VKM_OPENSEARCH_URL", "").strip() or None,
        opensearch_index_prefix=env.get("VKM_OPENSEARCH_INDEX_PREFIX", "vkm").strip() or "vkm",
        rerank_url=env.get("VKM_RERANK_URL", "").strip() or None,
        rerank_token=_secret(env, "VKM_RERANK_TOKEN"),
        api_url=env.get("VKM_API_URL", "").strip() or None,
        api_token=_secret(env, "VKM_API_TOKEN"),
        api_write_token=_secret(env, "VKM_API_WRITE_TOKEN"),
        log_level=env.get("VKM_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        models_dir=_path(env, "VKM_MODELS_DIR"),
        embed_url=env.get("VKM_EMBED_URL", "").strip() or None,
        embed_token=_secret(env, "VKM_EMBED_TOKEN"),
    )
