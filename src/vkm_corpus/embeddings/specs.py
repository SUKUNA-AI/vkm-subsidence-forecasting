"""Encoder specifications of the Retrieval Lab candidates (постановка лаборатории §6, §8, §19, §37–38).

One :class:`EncoderSpec` per model revision: what the reference implementation (HF transformers / sentence-transformers
/ PyLate on WORKSTATION) and the RX580 backend (llama.cpp Vulkan on CORE) must do identically — tokenizer rules,
prompts, special markers, pooling, heads, normalisation and output transforms. The service tokenizes with the pinned
``tokenizer.json`` (HF ``tokenizers``) and sends token ids to the backend, so tokenization parity is exact by
construction and only numerics differ between the reference and the RX580.

Values are FACT from the pinned model repositories (config.json, modules.json, 1_Pooling, config_sentence_transformers,
tokenizer post-processor) unless marked in ``notes``. Truncation lengths used in production are deployment choices
(MODEL_CHOICE of the query/embedding signature), not model facts: ``max_len`` is the model limit.

No StrEnum here (H-01): closed vocabularies are ``Literal`` types.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Family = Literal["dense", "late", "multi"]
Pooling = Literal["cls", "mean", "last", "none"]
Attention = Literal["causal", "bidirectional"]
OutputTransform = Literal["none", "tanh_int8"]
Role = Literal["query", "document"]

LLAMA_CPP_COMMIT = "4da6337767f973e2b4d0797e5b323d77d8565e4a"   # b11223, 2026-09-27 (same pin as the EDGE rerankers)


@dataclass(frozen=True)
class ColbertSpec:
    """Late-interaction (ColBERT-style) token output. ``dim`` is the projected token dimension; ``dims`` are the
    storage-quality variants (Matryoshka prefixes, re-normalised after truncation)."""

    dim: int
    dims: tuple[int, ...]
    query_marker: str | None          # special token inserted right after the leading special token (jina/PyLate)
    doc_marker: str | None
    query_maxlen: int                 # queries are padded/expanded to this length (0 = no expansion)
    doc_maxlen: int
    expansion_token: str | None       # token used for query augmentation (attended if ``attend_to_expansion``)
    attend_to_expansion: bool
    skip_punctuation_in_docs: bool    # drop document token vectors of punctuation tokens (ColBERT mask_punctuation)
    normalize_tokens: bool = True
    drop_special_tokens: tuple[str, ...] = ()   # token strings whose vectors are removed from the output
    head: Literal["dense_2", "gateway"] = "gateway"   # projection in the gateway (numpy) or in the GGUF graph
    style: Literal["pylate", "stanford"] = "pylate"   # tokenization rules (see vkm_corpus.embeddings.tokenize)


@dataclass(frozen=True)
class EncoderSpec:
    key: str
    model_id: str
    model_revision: str
    license: str
    family: Family
    arch: str                          # llama.cpp architecture of the GGUF
    params_m: int                      # parameters, millions (approximate, from the checkpoint)
    hidden_size: int
    n_layers: int
    vocab_size: int
    pooling: Pooling                   # pooling of the dense vector (none: token vectors only)
    normalize: bool                    # L2 normalisation of the final dense vector
    output_dim: int
    matryoshka_dims: tuple[int, ...]
    query_prefix: str
    doc_prefix: str
    attention: Attention
    max_len: int
    tokenizer_file: str = "tokenizer.json"
    output_transform: OutputTransform = "none"
    colbert: ColbertSpec | None = None
    sparse: bool = False               # learned sparse output (BGE-M3)
    gguf_source: Literal["convert", "official"] = "convert"
    llama_patches: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prefix(self, role: Role) -> str:
        return self.query_prefix if role == "query" else self.doc_prefix


_PUNCT = True

SPECS: dict[str, EncoderSpec] = {}


def _add(spec: EncoderSpec) -> EncoderSpec:
    if spec.key in SPECS:
        raise ValueError(f"duplicate encoder key {spec.key}")
    SPECS[spec.key] = spec
    return spec


# ---------------------------------------------------------------------------------------------------------------- dense
_add(EncoderSpec(
    key="qwen3-emb-0.6b", model_id="Qwen/Qwen3-Embedding-0.6B",
    model_revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3", license="apache-2.0", family="dense", arch="qwen3",
    params_m=596, hidden_size=1024, n_layers=28, vocab_size=151669, pooling="last", normalize=True, output_dim=1024,
    matryoshka_dims=(1024, 512, 256), attention="causal", max_len=32768,
    query_prefix="Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:",
    doc_prefix="",
    notes=("tokenizer post-processor appends <|endoftext|>; last-token pooling reads it",
           "query instruction is the model default; a domain instruction is a MODEL_CHOICE of the query signature")))

_add(EncoderSpec(
    key="jina-v5-small-retrieval", model_id="jinaai/jina-embeddings-v5-text-small-retrieval",
    model_revision="6856e76bb72982e58de0620458a4e8b3614da340", license="cc-by-nc-4.0", family="dense", arch="qwen3",
    params_m=596, hidden_size=1024, n_layers=28, vocab_size=151936, pooling="last", normalize=True, output_dim=1024,
    matryoshka_dims=(1024, 512, 256), attention="causal", max_len=32768,
    query_prefix="Query: ", doc_prefix="Document: ", gguf_source="official",
    notes=("retrieval LoRA merged by Jina (official retrieval variant); official GGUFs Q8_0/Q6_K/Q5_K_M (imatrix)",
           "tokenizer does not append EOS: last-token pooling reads the last text token")))

_add(EncoderSpec(
    key="jina-v5-nano-retrieval", model_id="jinaai/jina-embeddings-v5-text-nano-retrieval",
    model_revision="ac5d898c8d382b17167c33e5c8af644a3519b47d", license="cc-by-nc-4.0", family="dense", arch="eurobert",
    params_m=212, hidden_size=768, n_layers=12, vocab_size=128256, pooling="last", normalize=True, output_dim=768,
    matryoshka_dims=(768, 512, 256), attention="bidirectional", max_len=8192,
    query_prefix="Query: ", doc_prefix="Document: ", gguf_source="official",
    notes=("EuroBERT-210m backbone, retrieval LoRA merged; tokenizer appends <|end_of_text|> (last-token pooling)",)))

_add(EncoderSpec(
    key="granite-311m-r2", model_id="ibm-granite/granite-embedding-311m-multilingual-r2",
    model_revision="44399559930365213510b1ee2eb15ded83374f0e", license="apache-2.0", family="dense",
    arch="modern-bert", params_m=311, hidden_size=768, n_layers=22, vocab_size=262152, pooling="cls", normalize=True,
    output_dim=768, matryoshka_dims=(768, 512, 384, 256), attention="bidirectional", max_len=32768,
    query_prefix="", doc_prefix="",
    notes=("ModernBERT (GeGLU), local attention 128 every layer except each 3rd; <bos> prepended, no EOS",
           "matryoshka_dims are candidate truncations (§19); whether the checkpoint is MRL-trained is for J to measure")))

_add(EncoderSpec(
    key="granite-97m-r2", model_id="ibm-granite/granite-embedding-97m-multilingual-r2",
    model_revision="835ad14087e140460703cf0fae09f97d469d65c2", license="apache-2.0", family="dense",
    arch="modern-bert", params_m=97, hidden_size=384, n_layers=12, vocab_size=180000, pooling="cls", normalize=True,
    output_dim=384, matryoshka_dims=(384, 256), attention="bidirectional", max_len=32768,
    query_prefix="", doc_prefix="",
    notes=("ModernBERT with SiLU (SwiGLU); <|startoftext|> … <|return|>",
           "hidden 384 is not a multiple of 256: K-quants fall back per tensor (see model matrix)")))

_add(EncoderSpec(
    key="pplx-embed-0.6b", model_id="perplexity-ai/pplx-embed-v1-0.6b",
    model_revision="2c4d510dd4a732063c31a0f70193e35067b51fd8", license="mit", family="dense", arch="qwen3",
    params_m=596, hidden_size=1024, n_layers=28, vocab_size=151936, pooling="mean", normalize=False, output_dim=1024,
    matryoshka_dims=(1024, 512, 256), attention="bidirectional", max_len=32768,
    query_prefix="", doc_prefix="", output_transform="tanh_int8",
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert", "0002-qwen3-embeddings-no-kv-no-lm-head"),
    notes=("bidirectional Qwen3 (PPLXQwen3Model); no instruction, no special tokens",
           "native output: round(127*tanh(mean_pool)) int8, compared by cosine; float tanh vector kept for parity")))

_add(EncoderSpec(
    key="pplx-embed-context-0.6b", model_id="perplexity-ai/pplx-embed-context-v1-0.6b",
    model_revision="b42df969d4d78d1840769e45c68c4e4ce763768b", license="mit", family="dense", arch="qwen3",
    params_m=596, hidden_size=1024, n_layers=28, vocab_size=151936, pooling="none", normalize=False, output_dim=1024,
    matryoshka_dims=(1024, 512, 256), attention="bidirectional", max_len=32768,
    query_prefix="", doc_prefix="", output_transform="tanh_int8",
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert", "0002-qwen3-embeddings-no-kv-no-lm-head"),
    notes=("contextual late chunking: chunks joined by <|endoftext|>, one forward pass, mean pool per chunk span",
           "token vectors from the backend (pooling none), chunk pooling in the gateway")))

_add(EncoderSpec(
    key="mdenseon", model_id="lightonai/mDenseOn", model_revision="a5fdb000f7a21da96c3bddde3a782ef777316df3",
    license="apache-2.0", family="dense", arch="modern-bert", params_m=307, hidden_size=768, n_layers=22,
    vocab_size=256000, pooling="cls", normalize=False, output_dim=768, matryoshka_dims=(768, 512, 256),
    attention="bidirectional", max_len=8192, query_prefix="query: ", doc_prefix="document: ",
    notes=("ModernBERT (mmBERT-style, Gemma tokenizer); modules: Transformer + CLS pooling, no Normalize module",
           "similarity is cosine: the service L2-normalises before OpenSearch (cosine space)")))

_add(EncoderSpec(
    key="bge-m3", model_id="BAAI/bge-m3", model_revision="5617a9f61b028005a4858fdac845db406aefb181", license="mit",
    family="multi", arch="bert", params_m=568, hidden_size=1024, n_layers=24, vocab_size=250002, pooling="cls",
    normalize=True, output_dim=1024, matryoshka_dims=(1024,), attention="bidirectional", max_len=8192,
    query_prefix="", doc_prefix="", sparse=True,
    colbert=ColbertSpec(dim=1024, dims=(1024,), query_marker=None, doc_marker=None, query_maxlen=0, doc_maxlen=8192,
                        expansion_token=None, attend_to_expansion=False, skip_punctuation_in_docs=False,
                        drop_special_tokens=("<s>",), head="gateway"),
    notes=("XLM-RoBERTa; dense = normalize(h[CLS]); sparse = relu(sparse_linear(h)) max per token id (no specials);",
           "colbert = normalize(colbert_linear(h[1:])) — one forward pass serves all three (shared backbone §28)")))

_add(EncoderSpec(
    key="giga-emb-480m", model_id="ai-sage/Giga-Embeddings-instruct-480M-0826",
    model_revision="1763d603adac8057bd6482a708001b0ed2e0a903", license="mit", family="dense", arch="qwen3",
    params_m=480, hidden_size=1024, n_layers=28, vocab_size=128256, pooling="mean", normalize=True, output_dim=1024,
    matryoshka_dims=(1024, 512, 256), attention="bidirectional", max_len=32768,
    query_prefix="Instruct: Given a query, retrieve relevant passages\nQuery: ", doc_prefix="",
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert", "0002-qwen3-embeddings-no-kv-no-lm-head"),
    notes=("secondary candidate X1 of J: Qwen3BidirectionalModel (head_dim 64), Russian-centric BPE (o200k-style regex)",
           "GGUF vocabulary written with pre='default' (id-based use only: the service tokenizes with tokenizer.json)")))

# ----------------------------------------------------------------------------------------------------------------- late
_add(EncoderSpec(
    key="pplx-embed-late-0.6b", model_id="perplexity-ai/pplx-embed-v1-late-0.6b",
    model_revision="4d28cf627d225552cfc29fb7df6cb0705ea0f1b3", license="mit", family="late", arch="qwen3",
    params_m=596, hidden_size=1024, n_layers=28, vocab_size=151671, pooling="none", normalize=False, output_dim=128,
    matryoshka_dims=(128,), attention="bidirectional", max_len=32768, query_prefix="", doc_prefix="",
    colbert=ColbertSpec(dim=128, dims=(128,), query_marker="[Q] ", doc_marker="[D] ", query_maxlen=32,
                        doc_maxlen=512, expansion_token=None, attend_to_expansion=True,
                        skip_punctuation_in_docs=True),
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert", "0002-qwen3-embeddings-no-kv-no-lm-head"),
    notes=("PyLate ColBERT: Transformer + Dense(1024→128, no bias); query expansion to 32 tokens",
           "fine-tuned from pplx-embed-v1-0.6b but separate weights and vocab (151671): no weight sharing with dense")))

_add(EncoderSpec(
    key="mlateon", model_id="lightonai/mLateOn", model_revision="edd378f99593c0ac8a15518b97ad89786b02685e",
    license="apache-2.0", family="late", arch="modern-bert", params_m=307, hidden_size=768, n_layers=22,
    vocab_size=256002, pooling="none", normalize=False, output_dim=128, matryoshka_dims=(128,),
    attention="bidirectional", max_len=8192, query_prefix="", doc_prefix="",
    colbert=ColbertSpec(dim=128, dims=(128,), query_marker="[Q] ", doc_marker="[D] ", query_maxlen=0,
                        doc_maxlen=8191, expansion_token=None, attend_to_expansion=False,
                        skip_punctuation_in_docs=False),
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert",),
    notes=("PyLate: Dense(768→1536,+res) → Dense(1536→768,+res) → Dense(768→128); all linear without bias or activation",
           "so the head is one exact 128×768 matrix W3·(W2+R2)·(W1+R1) folded at conversion (DERIVATION)")))

_add(EncoderSpec(
    key="jina-colbert-v2", model_id="jinaai/jina-colbert-v2", model_revision="a9dc5cd7293d4c71dbbba04829923ba4d0e4f6ea",
    license="cc-by-nc-4.0", family="late", arch="jina-bert-v3", params_m=559, hidden_size=1024, n_layers=24,
    vocab_size=250004, pooling="none", normalize=False, output_dim=128, matryoshka_dims=(128, 96, 64),
    attention="bidirectional", max_len=8192, query_prefix="", doc_prefix="",
    colbert=ColbertSpec(dim=128, dims=(128, 96, 64), query_marker="[QueryMarker]", doc_marker="[DocumentMarker]",
                        query_maxlen=32, doc_maxlen=300, expansion_token="<mask>", attend_to_expansion=True,
                        skip_punctuation_in_docs=True, style="stanford"),
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert",),
    notes=("XLM-RoBERTa with rotary positions (jina-embeddings-v3 backbone, no LoRA) + linear 1024→128",
           "jina-colbert-v2-96 is not public (HTTP 401): 96 = Matryoshka prefix of the 128 head (to verify against -64)")))


_add(EncoderSpec(
    key="jina-colbert-v2-64", model_id="jinaai/jina-colbert-v2-64",
    model_revision="7c9f323b61e6f96754300dea81bb51c3cc1d3f34", license="cc-by-nc-4.0", family="late",
    arch="jina-bert-v3", params_m=559, hidden_size=1024, n_layers=24, vocab_size=250004, pooling="none",
    normalize=False, output_dim=64, matryoshka_dims=(64,), attention="bidirectional", max_len=8192,
    query_prefix="", doc_prefix="",
    colbert=ColbertSpec(dim=64, dims=(64,), query_marker="[QueryMarker]", doc_marker="[DocumentMarker]",
                        query_maxlen=32, doc_maxlen=300, expansion_token="<mask>", attend_to_expansion=True,
                        skip_punctuation_in_docs=True, style="stanford"),
    llama_patches=("0001-convert-pplx-jina-colbert-mmbert",),
    notes=("backbone tensors and tokenizer identical to jina-colbert-v2 (294 tensors, 0 differences): the RX580 serves "
           "it from the jina-colbert-v2 GGUF with this repo's own 64×1024 head (not a prefix of the 128 head)",
           "shared backbone §28: one resident model, 128- and 64-dim token vectors from one forward pass")))

def get(key: str) -> EncoderSpec:
    try:
        return SPECS[key]
    except KeyError as exc:
        raise KeyError(f"unknown encoder {key!r}; known: {sorted(SPECS)}") from exc


def dense_keys() -> list[str]:
    return [k for k, s in SPECS.items() if s.family in ("dense", "multi")]


def late_keys() -> list[str]:
    return [k for k, s in SPECS.items() if s.colbert is not None]
