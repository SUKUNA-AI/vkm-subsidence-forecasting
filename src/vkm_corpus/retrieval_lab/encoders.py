"""Model specifications and encoder adapters of the lab.

Specs live in ``benchmarks/retrieval_v0/configs/models.json`` (public: repo, exact revision, prompts, pooling, lengths —
never machine paths). Weights are read from ``$VKM_MODELS_DIR/<org>__<name>/<revision>/`` (download receipt
``MODELS_RECEIPT_RETRIEVAL.json``); nothing is downloaded here (offline, ``HF_HUB_OFFLINE=1``).

Adapters (all return float32 numpy, L2-normalised unless stated):

* :class:`STDenseEncoder` — sentence-transformers models (jina-v5 retrieval, granite, pplx dense, mDenseOn, Qwen3,
  Giga, harrier) with the literal query/document prefixes of the model card;
* :class:`BGEM3Encoder` — BGE-M3 dense (CLS), learned sparse (``relu(sparse_linear)``, max per token id, special
  tokens dropped) and multi-vector (``colbert_linear`` on tokens without CLS) from one forward pass, as in
  FlagEmbedding's ``BGEM3FlagModel``;
* :class:`MultiVectorEncoder` — ColBERT models (mLateOn, pplx-late, jina-colbert-v2) through sentence-transformers
  ``MultiVectorEncoder`` or PyLate ``models.ColBERT`` (fallback), with the prefixes/lengths/expansion of the card;
* :class:`PplxContextEncoder` — pplx-embed-context (late chunking over a window of units);
* :class:`FakeEncoder` — deterministic hashing encoder for tests (dense, sparse and multi-vector), no model files.

Heavy imports (torch, transformers, sentence_transformers, pylate) happen inside the adapters.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from vkm_corpus.retrieval_lab.textproc import analyze, raw_tokens
from vkm_corpus.retrieval_lab.vectors import l2_normalize

FAMILIES = ("st_dense", "bge_m3", "multivector", "pplx_context", "visual_st", "fake")


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model_id: str
    revision: str
    family: str
    role: str                                   # mandatory | secondary | visual | fake
    dim: int
    modes: tuple[str, ...] = ("dense",)
    mrl_dims: tuple[int, ...] = ()
    pooling: str = ""
    normalize: bool = True
    query_prefix: str = ""
    doc_prefix: str = ""
    max_query_tokens: int = 128
    max_doc_tokens: int = 512
    trust_remote_code: bool = False
    weights_dtype: str = "bf16"
    license: str = ""
    late: dict[str, Any] = field(default_factory=dict)
    code_repos: dict[str, str] = field(default_factory=dict)   # remote-code repo → revision (auto_map outside the repo)
    native_precision: str = "fp32"              # pplx: int8 (tanh-quantised output of the model)
    backend: str = "st"                         # multivector: st (sentence-transformers 6.x) | pylate (reference venv)
    notes: str = ""

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "ModelSpec":
        obj = dict(obj)
        for k in ("modes", "mrl_dims"):
            if k in obj:
                obj[k] = tuple(obj[k])
        return cls(**obj)

    def local_dir(self, models_dir: str | Path) -> Path:
        return Path(models_dir) / self.model_id.replace("/", "__") / self.revision

    def public_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["modes"], d["mrl_dims"] = list(self.modes), list(self.mrl_dims)
        return d


def load_specs(path: str | Path) -> dict[str, ModelSpec]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    specs = [ModelSpec.from_json(m) for m in data["models"]]
    keys = [s.key for s in specs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate model keys in models.json")
    return {s.key: s for s in specs}


# ---------------------------------------------------------------- device / threads
def torch_setup(device: str, threads: int | None) -> Any:
    import torch

    if device == "cpu" and threads:
        torch.set_num_threads(threads)
    return torch


def _dtype(torch: Any, device: str, precision: str) -> Any:
    if device == "cpu" or precision == "fp32":
        return torch.float32
    return {"bf16": torch.bfloat16, "fp16": torch.float16}.get(precision, torch.float32)


# ---------------------------------------------------------------- remote code outside the model repo (jina-colbert-v2)
_AUTO_REF = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)--(?P<ref>.+)$")


def local_code_view(spec: ModelSpec, models_dir: str | Path) -> Path:
    """For models whose ``auto_map`` points to another repo: a derived directory with the model files (hard links,
    else copies), the pinned remote-code files and ``auto_map`` rewritten to local modules. Idempotent."""
    src = spec.local_dir(models_dir)
    if not spec.code_repos:
        return src
    view = Path(models_dir) / "derived" / "retrieval_lab" / (spec.model_id.replace("/", "__") + "__localcode") / spec.revision
    marker = view / ".vkm_local_code.json"
    if marker.is_file():
        return view
    view.mkdir(parents=True, exist_ok=True)
    for f in src.rglob("*"):
        if f.is_file():
            dest = view / f.relative_to(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                continue
            try:
                os.link(f, dest)
            except OSError:
                shutil.copy2(f, dest)
    for repo, rev in spec.code_repos.items():
        code_dir = Path(models_dir) / repo.replace("/", "__") / rev
        for py in code_dir.glob("*.py"):
            shutil.copy2(py, view / py.name)
    cfg_path = view / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for key, ref in list((cfg.get("auto_map") or {}).items()):
        m = _AUTO_REF.match(ref)
        if m and m["repo"] in spec.code_repos:
            cfg["auto_map"][key] = m["ref"]
    if (view / "config.json").exists():
        (view / "config.json").unlink()                    # never write through a hard link to the verified file
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    marker.write_text(json.dumps({"model": spec.model_id, "revision": spec.revision, "code_repos": spec.code_repos},
                                 indent=1), encoding="utf-8")
    return view


# ---------------------------------------------------------------- adapters
class BaseEncoder:
    spec: ModelSpec
    device: str = "cpu"
    precision: str = "fp32"

    def encode_queries(self, texts: Sequence[str]) -> Any:
        raise NotImplementedError

    def encode_docs(self, texts: Sequence[str]) -> Any:
        raise NotImplementedError

    def runtime(self) -> dict[str, Any]:
        return {"device": self.device, "compute_precision": self.precision}

    def close(self) -> None:
        pass


class STDenseEncoder(BaseEncoder):
    def __init__(self, spec: ModelSpec, models_dir: str | Path, *, device: str = "cpu", precision: str = "fp32",
                 batch_size: int = 16, threads: int | None = None) -> None:
        torch = torch_setup(device, threads)
        from sentence_transformers import SentenceTransformer

        self.spec, self.device, self.precision, self.batch_size = spec, device, precision, batch_size
        path = local_code_view(spec, models_dir)
        self.model = SentenceTransformer(str(path), device=device, trust_remote_code=spec.trust_remote_code,
                                         local_files_only=True, model_kwargs={"dtype": _dtype(torch, device, precision)})

    def _encode(self, texts: Sequence[str], prefix: str, max_tokens: int) -> np.ndarray:
        self.model.max_seq_length = max_tokens
        kwargs: dict[str, Any] = {"batch_size": self.batch_size, "convert_to_numpy": True,
                                  "show_progress_bar": False}
        if prefix:
            kwargs["prompt"] = prefix
        emb = self.model.encode(list(texts), **kwargs)
        return l2_normalize(np.asarray(emb, dtype=np.float32))

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, self.spec.query_prefix, self.spec.max_query_tokens)

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, self.spec.doc_prefix, self.spec.max_doc_tokens)

    def close(self) -> None:
        del self.model


class BGEM3Encoder(BaseEncoder):
    """Dense + sparse + multi-vector outputs of BGE-M3 from one forward pass."""

    def __init__(self, spec: ModelSpec, models_dir: str | Path, *, device: str = "cpu", precision: str = "fp32",
                 batch_size: int = 16, threads: int | None = None) -> None:
        torch = torch_setup(device, threads)
        from transformers import AutoModel, AutoTokenizer

        self.torch, self.spec, self.device, self.precision, self.batch_size = torch, spec, device, precision, batch_size
        path = spec.local_dir(models_dir)
        dtype = _dtype(torch, device, precision)
        self.tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
        self.model = AutoModel.from_pretrained(str(path), local_files_only=True, dtype=dtype).to(device).eval()
        hidden = self.model.config.hidden_size
        self.colbert = torch.nn.Linear(hidden, hidden)
        self.colbert.load_state_dict(torch.load(path / "colbert_linear.pt", map_location="cpu", weights_only=True))
        self.sparse = torch.nn.Linear(hidden, 1)
        self.sparse.load_state_dict(torch.load(path / "sparse_linear.pt", map_location="cpu", weights_only=True))
        self.colbert = self.colbert.to(device=device, dtype=dtype).eval()
        self.sparse = self.sparse.to(device=device, dtype=dtype).eval()
        self.special = {i for i in (self.tokenizer.cls_token_id, self.tokenizer.eos_token_id,
                                    self.tokenizer.pad_token_id, self.tokenizer.unk_token_id) if i is not None}

    def _forward(self, texts: Sequence[str], max_tokens: int) -> dict[str, list[Any]]:
        torch = self.torch
        out: dict[str, list[Any]] = {"dense": [], "sparse": [], "multivector": []}
        for i in range(0, len(texts), self.batch_size):
            batch = self.tokenizer(list(texts[i:i + self.batch_size]), padding=True, truncation=True,
                                   max_length=max_tokens, return_tensors="pt").to(self.device)
            with torch.inference_mode():
                h = self.model(**batch).last_hidden_state
                dense = torch.nn.functional.normalize(h[:, 0].float(), dim=-1)
                weights = torch.relu(self.sparse(h)).squeeze(-1).float()
                colb = self.colbert(h[:, 1:]).float() * batch["attention_mask"][:, 1:, None].float()
                colb = torch.nn.functional.normalize(colb, dim=-1)
            ids = batch["input_ids"].cpu().numpy()
            mask = batch["attention_mask"].cpu().numpy()
            w = weights.cpu().numpy()
            cv = colb.cpu().numpy()
            out["dense"].extend(dense.cpu().numpy())
            for j in range(len(ids)):
                lex: dict[str, float] = {}
                for tok, wt in zip(ids[j], w[j]):
                    if int(tok) in self.special or wt <= 0:
                        continue
                    key = str(int(tok))
                    if wt > lex.get(key, 0.0):
                        lex[key] = float(wt)
                out["sparse"].append(lex)
                n = int(mask[j].sum())
                out["multivector"].append(cv[j][: n - 1].astype(np.float32))    # tokens without CLS, as FlagEmbedding
        out["dense"] = [np.asarray(v, dtype=np.float32) for v in out["dense"]]
        return out

    def encode_all_queries(self, texts: Sequence[str]) -> dict[str, list[Any]]:
        return self._forward(texts, self.spec.max_query_tokens)

    def encode_all_docs(self, texts: Sequence[str]) -> dict[str, list[Any]]:
        return self._forward(texts, self.spec.max_doc_tokens)

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack(self.encode_all_queries(texts)["dense"])

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack(self.encode_all_docs(texts)["dense"])


class MultiVectorEncoder(BaseEncoder):
    """ColBERT-style token vectors. ``backend``: ``st`` (sentence-transformers MultiVectorEncoder) or ``pylate``."""

    def __init__(self, spec: ModelSpec, models_dir: str | Path, *, device: str = "cpu", precision: str = "fp32",
                 batch_size: int = 16, threads: int | None = None, backend: str | None = None) -> None:
        torch = torch_setup(device, threads)
        backend = backend or spec.backend
        self.spec, self.device, self.precision, self.batch_size, self.backend = spec, device, precision, batch_size, backend
        path = str(local_code_view(spec, models_dir))
        late = spec.late
        dtype = _dtype(torch, device, precision)
        if backend == "st":
            from sentence_transformers import MultiVectorEncoder as STMultiVector

            self.model = STMultiVector(path, device=device, trust_remote_code=spec.trust_remote_code,
                                       local_files_only=True, model_kwargs={"dtype": dtype})
            # benchmark caps (design §1): documents ≤ 512 tokens for every model; queries of models without
            # trained query expansion ≤ 128; models with expansion keep their trained width (32)
            tr = self.model[0]
            if late.get("document_length") and hasattr(tr, "document_length"):
                tr.document_length = int(late["document_length"])
            if late.get("query_length") and hasattr(tr, "query_length") and not getattr(tr, "query_expansion", None):
                tr.query_length = int(late["query_length"])
        elif backend == "pylate":
            from pylate import models as pl_models

            kwargs: dict[str, Any] = {"device": device, "trust_remote_code": spec.trust_remote_code,
                                      "model_kwargs": {"dtype": dtype}}
            for key in ("query_prefix", "document_prefix", "query_length", "document_length",
                        "attend_to_expansion_tokens", "do_query_expansion", "skiplist_words"):
                if key in late:
                    kwargs[key] = late[key]
            self.model = pl_models.ColBERT(model_name_or_path=path, **kwargs)
        else:
            raise ValueError("backend must be st or pylate")

    def _as_list(self, out: Any) -> list[np.ndarray]:
        return [l2_normalize(np.asarray(x.float().cpu().numpy() if hasattr(x, "cpu") else x, dtype=np.float32))
                for x in out]

    def encode_queries(self, texts: Sequence[str]) -> list[np.ndarray]:
        if self.backend == "st":
            return self._as_list(self.model.encode_query(list(texts), batch_size=self.batch_size,
                                                         show_progress_bar=False))
        return self._as_list(self.model.encode(list(texts), is_query=True, batch_size=self.batch_size,
                                               show_progress_bar=False))

    def encode_docs(self, texts: Sequence[str]) -> list[np.ndarray]:
        if self.backend == "st":
            return self._as_list(self.model.encode_document(list(texts), batch_size=self.batch_size,
                                                            show_progress_bar=False))
        return self._as_list(self.model.encode(list(texts), is_query=False, batch_size=self.batch_size,
                                               show_progress_bar=False))


class PplxContextEncoder(BaseEncoder):
    """pplx-embed-context: chunk vectors from late chunking over a window (list of unit texts)."""

    def __init__(self, spec: ModelSpec, models_dir: str | Path, *, device: str = "cpu", precision: str = "fp32",
                 batch_size: int = 4, threads: int | None = None) -> None:
        torch = torch_setup(device, threads)
        from transformers.dynamic_module_utils import get_class_from_dynamic_module

        self.spec, self.device, self.precision, self.batch_size = spec, device, precision, batch_size
        path = str(spec.local_dir(models_dir))
        # AutoModel loads the config class twice (two dynamic-module copies) and the model's isinstance check fails;
        # taking both classes from one dynamic module avoids that (transformers 5.x)
        model_cls = get_class_from_dynamic_module("modeling.PPLXQwen3ContextualModel", path)
        config = model_cls.config_class.from_pretrained(path, trust_remote_code=True)
        self.model = model_cls.from_pretrained(path, config=config, trust_remote_code=True, local_files_only=True,
                                               dtype=_dtype(torch, device, precision)).to(device)

    def encode_windows(self, windows: Sequence[Sequence[str]]) -> list[np.ndarray]:
        out = self.model.encode([list(w) for w in windows], batch_size=self.batch_size, quantization="int8",
                                convert_to_numpy=True)
        return [l2_normalize(np.asarray(x, dtype=np.float32)) for x in out]

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([w[0] for w in self.encode_windows([[t] for t in texts])])

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([w[0] for w in self.encode_windows([[t] for t in texts])])


# ---------------------------------------------------------------- deterministic fake (tests)
def _hash_vec(token: str, dim: int, seed: str) -> np.ndarray:
    h = hashlib.sha256(f"{seed}|{token}".encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "little"))
    return rng.standard_normal(dim).astype(np.float32)


class FakeEncoder(BaseEncoder):
    """Bag of analysed tokens and character trigrams hashed into ``dim`` (dense), term counts (sparse), per-token hashed
    vectors (multi-vector). Deterministic; lexical-ish, so tests can predict rankings."""

    def __init__(self, spec: ModelSpec | None = None, *, dim: int = 64, token_dim: int = 16) -> None:
        self.spec = spec or ModelSpec(key="FAKE", model_id="fake/hash", revision="0" * 40, family="fake", role="fake",
                                      dim=dim, modes=("dense", "sparse", "multivector"))
        self.dim, self.token_dim = self.spec.dim, token_dim
        self._cache: dict[tuple[str, int], np.ndarray] = {}

    def _vec(self, token: str, dim: int) -> np.ndarray:
        key = (token, dim)
        if key not in self._cache:
            self._cache[key] = _hash_vec(token, dim, self.spec.model_id)
        return self._cache[key]

    def _dense(self, text: str) -> np.ndarray:
        feats = analyze(text)
        low = " ".join(raw_tokens(text))
        feats += [low[i:i + 3] for i in range(max(len(low) - 2, 0))]
        v = np.zeros(self.dim, dtype=np.float32)
        for f in feats:
            v += self._vec(f, self.dim)
        return l2_normalize(v) if feats else l2_normalize(np.ones(self.dim, dtype=np.float32))

    def encode_queries(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._dense(t) for t in texts])

    def encode_docs(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._dense(t) for t in texts])

    def sparse(self, texts: Sequence[str]) -> list[dict[str, float]]:
        out = []
        for t in texts:
            counts: dict[str, float] = {}
            for tok in analyze(t):
                counts[tok] = counts.get(tok, 0.0) + 1.0
            out.append({k: 1.0 + np.log(v) for k, v in counts.items()})
        return out

    def tokens(self, texts: Sequence[str]) -> list[np.ndarray]:
        out = []
        for t in texts:
            toks = analyze(t) or ["<empty>"]
            out.append(l2_normalize(np.stack([self._vec(x, self.token_dim) for x in toks])))
        return out


def make_encoder(spec: ModelSpec, models_dir: str | Path | None, **kw: Any) -> BaseEncoder:
    if spec.family == "fake":
        return FakeEncoder(spec)
    if models_dir is None:
        raise ValueError("VKM_MODELS_DIR is not set (local model snapshots)")
    if spec.family == "st_dense":
        return STDenseEncoder(spec, models_dir, **kw)
    if spec.family == "bge_m3":
        return BGEM3Encoder(spec, models_dir, **kw)
    if spec.family == "multivector":
        return MultiVectorEncoder(spec, models_dir, **kw)
    if spec.family == "pplx_context":
        return PplxContextEncoder(spec, models_dir, **kw)
    raise ValueError(f"no adapter for family {spec.family}")
