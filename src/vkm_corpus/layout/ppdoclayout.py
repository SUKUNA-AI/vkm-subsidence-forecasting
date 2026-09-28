"""PP-DocLayoutV3 inference (transformers 5.x ``PPDocLayoutV3ForObjectDetection``), offline, pinned revision.

Post-processing is done here from the raw model outputs (logits, boxes, reading-order logits) – the HF helper needs
OpenCV only for mask polygons, which v0 does not use. The raw record keeps every (query, class) pair of the top-300
flattened scores above ``raw_floor`` with its box in render pixels and in PAGE_PT_TL, and the reading-order rank of
its query, so thresholds can be changed later without the GPU.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_ID = "PaddlePaddle/PP-DocLayoutV3_safetensors"
MODEL_REVISION = "97d101e6db2642e162a1d05392d1b0231c91033e"
WEIGHTS_SHA256 = "5ea422c6cc5fe759a47e1357c35639b58173508e025a3131cbe4b6ac59e2b85e"
EXTRACTOR_ID = "pp-doclayoutv3-hf"
EXTRACTOR_VERSION = "0.1.0"
RAW_SCHEMA = "vkm.layout_raw/1"


@dataclass(frozen=True)
class LayoutConfig:
    raw_floor: float = 0.05     # detections kept in LAYOUT_RAW
    top_k: int = 300
    batch_size: int = 4
    dtype: str = "float32"
    deterministic: bool = True

    def as_config(self) -> dict[str, Any]:
        return {"raw_floor": self.raw_floor, "top_k": self.top_k, "dtype": self.dtype, "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION, "post_process": "vkm-topk-flat-v1"}


def model_dir(models_root: Path) -> Path:
    return Path(models_root) / "PaddlePaddle__PP-DocLayoutV3_safetensors" / MODEL_REVISION


class LayoutModel:
    """Loaded detector; ``detect(images)`` returns raw detection dicts (one per image)."""

    def __init__(self, models_root: Path, device: str | None = None, config: LayoutConfig = LayoutConfig()):
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        import torch
        from transformers import PPDocLayoutV3ForObjectDetection, PPDocLayoutV3ImageProcessor

        self.config = config
        path = model_dir(models_root)
        if not (path / "model.safetensors").exists():
            raise FileNotFoundError(f"MODEL_UNAVAILABLE: PP-DocLayoutV3 snapshot not found under the models root")
        if config.deterministic:
            torch.backends.cudnn.benchmark = False
            torch.backends.cudnn.deterministic = True
            torch.backends.cuda.matmul.allow_tf32 = False
            torch.backends.cudnn.allow_tf32 = False
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.torch = torch
        self.processor = PPDocLayoutV3ImageProcessor.from_pretrained(str(path), local_files_only=True)
        self.model = PPDocLayoutV3ForObjectDetection.from_pretrained(str(path), local_files_only=True)
        self.model.to(self.device).eval()
        self.id2label = {int(k): v for k, v in self.model.config.id2label.items()}
        import transformers

        self.runtime = {"torch": torch.__version__, "transformers": transformers.__version__, "device": self.device,
                        "cuda": torch.version.cuda, "dtype": config.dtype,
                        "gpu": torch.cuda.get_device_name(0) if self.device.startswith("cuda") else None,
                        "deterministic": config.deterministic}

    def _order_ranks(self, order_logits: Any) -> Any:
        torch = self.torch
        scores = torch.sigmoid(order_logits)
        _, n, _ = scores.shape
        votes = scores.triu(diagonal=1).sum(dim=1) + (1.0 - scores.transpose(1, 2)).tril(diagonal=-1).sum(dim=1)
        pointers = torch.argsort(votes, dim=1, stable=True)
        ranks = torch.empty_like(pointers)
        ar = torch.arange(n, device=pointers.device, dtype=pointers.dtype).expand(pointers.shape[0], -1)
        ranks.scatter_(1, pointers, ar)
        return ranks

    def detect(self, images: list[Any]) -> list[dict[str, Any]]:
        """Raw detections for PIL images (RGB). Boxes in image pixels (x0, y0, x1, y1)."""
        torch = self.torch
        out: list[dict[str, Any]] = []
        cfg = self.config
        for start in range(0, len(images), cfg.batch_size):
            batch = [im.convert("RGB") for im in images[start:start + cfg.batch_size]]
            inputs = self.processor(images=batch, return_tensors="pt")
            with torch.inference_mode():
                res = self.model(pixel_values=inputs["pixel_values"].to(self.device))
            logits = res.logits.float()
            boxes = res.pred_boxes.float()
            ranks = self._order_ranks(res.order_logits.float())
            nq, nc = logits.shape[1], logits.shape[2]
            probs = torch.sigmoid(logits).flatten(1)
            top_scores, top_idx = torch.topk(probs, min(cfg.top_k, nq * nc), dim=-1)
            for b, im in enumerate(batch):
                W, H = im.width, im.height
                dets = []
                for s, idx in zip(top_scores[b].tolist(), top_idx[b].tolist()):
                    if s < cfg.raw_floor:
                        break
                    q, lab = divmod(int(idx), nc)
                    cx, cy, w, h = boxes[b, q].tolist()
                    x0, y0, x1, y1 = (cx - w / 2) * W, (cy - h / 2) * H, (cx + w / 2) * W, (cy + h / 2) * H
                    dets.append({"query": q, "label_id": lab, "label": self.id2label.get(lab, str(lab)),
                                 "score": round(float(s), 6),
                                 "box_px": [round(max(0.0, x0), 2), round(max(0.0, y0), 2),
                                            round(min(float(W), x1), 2), round(min(float(H), y1), 2)],
                                 "order_rank": int(ranks[b, q].item())})
                out.append({"n_queries": nq, "n_classes": nc, "detections": dets})
        return out


def raw_record(det: dict[str, Any], *, render: dict[str, Any], config: LayoutConfig, runtime: dict[str, Any],
               id2label: dict[int, str], run_id: str | None, source_id: str, page_id: str) -> dict[str, Any]:
    """LAYOUT_RAW JSON of one page; boxes are also given in PAGE_PT_TL (px * 72 / dpi)."""
    s = 72.0 / float(render["dpi"])
    dets = []
    for i, d in enumerate(det["detections"]):
        x0, y0, x1, y1 = d["box_px"]
        dets.append({**d, "i": i, "box_pt": [round(x0 * s, 3), round(y0 * s, 3), round(x1 * s, 3), round(y1 * s, 3)]})
    return {"schema": RAW_SCHEMA, "source_id": source_id, "page_id": page_id, "processing_run_id": run_id,
            "model": {"model_id": MODEL_ID, "model_revision": MODEL_REVISION, "weights_sha256": WEIGHTS_SHA256},
            "extractor": {"id": EXTRACTOR_ID, "version": EXTRACTOR_VERSION}, "runtime": runtime,
            "config": config.as_config(), "id2label": {str(k): v for k, v in sorted(id2label.items())},
            "input": render, "n_queries": det["n_queries"], "detections": dets}
