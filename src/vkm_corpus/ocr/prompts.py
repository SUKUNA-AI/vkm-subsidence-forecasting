"""Prompts, sampling and the pinned model identity of GLM-OCR (design §4.1; SDK v0.1.5 as the semantic reference).

The model identity enters ``call_signature`` (model id, revision, weights sha256, prompt, sampling, input pixels);
the serving engine and its version are recorded in every raw record but are not part of the key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODEL_ID = "zai-org/GLM-OCR"
MODEL_REVISION = "2e85a62840ccac27daa451df36c736c4636b8628"
WEIGHTS_SHA256 = "a16eb0de98d199293371c560f95f83130d2a2c9612449df16839f08ff9498815"
SERVED_MODEL_NAME = "glm-ocr"

TASK_PROMPTS: dict[str, str] = {
    "text": "Text Recognition:",
    "formula": "Formula Recognition:",
    "table": "Table Recognition:",
}

# layout label → recognition task; nothing is abandoned (header/footer/number/footnote/reference are recognised as
# text and typed as page furniture), figures are not recognised (their crops are kept)
LABEL_TASK: dict[str, str | None] = {
    "abstract": "text", "algorithm": "text", "aside_text": "text", "content": "text", "doc_title": "text",
    "figure_title": "text", "footer": "text", "footnote": "text", "formula_number": "text", "header": "text",
    "number": "text", "paragraph_title": "text", "reference": "text", "reference_content": "text", "text": "text",
    "vision_footnote": "text", "seal": "text", "vertical_text": "text",
    "table": "table",
    "formula": "formula", "display_formula": "formula", "inline_formula": "formula",
    "chart": None, "image": None,
}


@dataclass(frozen=True)
class Sampling:
    temperature: float = 0.0
    top_p: float = 0.00001
    top_k: int = 1
    repetition_penalty: float = 1.1
    max_tokens: int = 8192
    seed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "top_p": self.top_p, "top_k": self.top_k,
                "repetition_penalty": self.repetition_penalty, "max_tokens": self.max_tokens, "seed": self.seed}


@dataclass(frozen=True)
class ModelIdentity:
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    weights_sha256: str = WEIGHTS_SHA256
    served_model_name: str = SERVED_MODEL_NAME
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "model_revision": self.model_revision,
                "weights_sha256": self.weights_sha256, "served_model_name": self.served_model_name}


DEFAULT_SAMPLING = Sampling()
DEFAULT_MODEL = ModelIdentity()


def prompt_for(task: str) -> str:
    try:
        return TASK_PROMPTS[task]
    except KeyError as exc:
        raise ValueError(f"unknown OCR task {task!r}") from exc
