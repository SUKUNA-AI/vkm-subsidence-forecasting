"""One synthetic legend-conditioned inference with the pinned official UIUC UNet.

Requires the original Darpa_Unet_Release source and official checkpoint as local
inputs. Does not install packages, download weights, train, or run the pipeline.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid

COMMIT = "7f6f6450108e43df83a1db2a51b6c7b893c65f11"
WEIGHT_SHA = "453a711c315ff456965d8bb383c07dd5b0038a3d50615b8dfe5bc55f48160973"
WEIGHT_BYTES = 372605901
MODEL_SOURCE_SHA = "7e7e60cef310e38d1cb6fcb8c663a8d261194fda4979f83ba532abb39ebac727"
SOURCE_SHA = {"models/models.py": MODEL_SOURCE_SHA,
              "models/__init__.py": "9b83d5f102aaff65b776e770ff40065fc4c31817c322e10f85abcd7fca95022e",
              "models/res_unet.py": "75f0af93da04df057a847c37f7d363deedc9d89cf1f8dce148d7daaa1303ccd5"}


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def gpu_lock(path):
    token = uuid.uuid4().hex
    body = json.dumps({"task": "uiuc_official_component_smoke", "pid": os.getpid(),
                       "token": token, "started_at_utc": datetime.now(timezone.utc).isoformat()})
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(body)
    try:
        yield
    finally:
        # Never remove a lock whose ownership changed while this process ran.
        if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("token") == token:
            path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gpu-lock", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Use a fresh empty output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    result = {"status": "FAIL", "scope": "standalone official UIUC legend-conditioned polygon UNet; synthetic only",
              "repository": "https://github.com/xiyuez2/Darpa_Unet_Release", "commit": COMMIT,
              "license": "UNSPECIFIED in repository and weight metadata; no redistribution claim",
              "started_at_utc": datetime.now(timezone.utc).isoformat(), "model_forward_calls": 0,
              "frame": {"id": "SYNTHETIC_IMAGE_PX", "unit": "px", "origin": "TOP_LEFT", "epsg": None},
              "full_uiuc_pipeline": "NOT_RUN", "legend_detection": "NOT_RUN", "training": "NOT_RUN",
              "ta1_geopackage": "NOT_SUPPORTED_BY_THIS_STANDALONE_MODEL_INTERFACE",
              "script_sha256": sha(Path(__file__))}
    try:
        if args.checkpoint.stat().st_size != WEIGHT_BYTES or sha(args.checkpoint) != WEIGHT_SHA:
            raise RuntimeError("Official checkpoint size/SHA mismatch")
        if any(sha(args.source_root / name) != digest for name, digest in SOURCE_SHA.items()):
            raise RuntimeError("Original pinned UIUC model source SHA mismatch")
        result["checkpoint_sha256"] = WEIGHT_SHA
        result["checkpoint_bytes"] = WEIGHT_BYTES
        result["source_sha256"] = {str(p.relative_to(args.source_root)): sha(p)
                                   for p in (args.source_root / "models").glob("*.py")}
        # CPU thread limits apply before importing Torch/NumPy.
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[key] = "2"
        import numpy as np
        import torch
        from PIL import Image, ImageDraw
        torch.set_num_threads(2)
        sys.path.insert(0, str(args.source_root.resolve()))
        from models import SegmentationModel
        if Path(inspect.getfile(SegmentationModel)).resolve() != (args.source_root / "models/models.py").resolve():
            raise RuntimeError("Imported architecture does not come from the pinned source tree")

        result["versions"] = {name: importlib.metadata.version(name) for name in
                              ("torch", "torchvision", "pytorch-lightning", "torchmetrics", "adamp", "numpy", "pillow")}
        result["python"] = sys.executable
        result["torch_path"] = torch.__file__
        image = Image.new("RGB", (256, 256), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((32, 48, 192, 208), fill=(230, 170, 70), outline="black", width=2)
        draw.line((0, 128, 255, 128), fill="black", width=2)
        legend = Image.new("RGB", (256, 256), (230, 170, 70))
        image.save(args.output / "synthetic_map.png")
        legend.save(args.output / "synthetic_legend.png")
        image_array = np.asarray(image).copy()
        def normalize(pil):
            tensor = torch.from_numpy(np.asarray(pil).copy()).permute(2, 0, 1).float().unsqueeze(0) / 255
            mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
            std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
            return (tensor - mean) / std

        # Safe checkpoint loading; do not silently execute pickle globals.
        payload = torch.load(args.checkpoint, map_location="cpu", weights_only=True)
        if "state_dict" not in payload:
            raise RuntimeError("Expected original Lightning state_dict checkpoint")
        model = SegmentationModel(args=SimpleNamespace(model="Unet"))
        incompat = model.load_state_dict(payload["state_dict"], strict=True)
        result["strict_state_dict"] = {"missing_keys": incompat.missing_keys, "unexpected_keys": incompat.unexpected_keys}
        result["checkpoint_lightning_version"] = payload.get("pytorch-lightning_version")
        result["parameter_count"] = sum(p.numel() for p in model.parameters())
        del payload
        with gpu_lock(args.gpu_lock):
            if not torch.cuda.is_available():
                raise RuntimeError("CUDA is unavailable; no CPU substitution for this GPU smoke")
            free, total = torch.cuda.mem_get_info()
            if free < 2 * 1024**3:
                raise RuntimeError("Less than 2 GiB free VRAM before the bounded smoke")
            result["device"] = {"name": torch.cuda.get_device_name(0), "capability": torch.cuda.get_device_capability(0),
                                "free_bytes_before": free, "total_bytes": total}
            model = model.eval().to("cuda")
            map_tensor, legend_tensor = normalize(image).to("cuda"), normalize(legend).to("cuda")
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
            started = time.perf_counter()
            with torch.inference_mode():
                logits = model.model(map_tensor, legend_tensor)
                result["model_forward_calls"] += 1
                probability = torch.softmax(logits, dim=1)[:, 1]
            torch.cuda.synchronize()
            result["inference_seconds"] = round(time.perf_counter() - started, 6)
            result["peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
            result["logits_shape"] = list(logits.shape)
            if tuple(logits.shape) != (1, 2, 256, 256) or not torch.isfinite(logits).all().item():
                raise RuntimeError("Invalid original model logits shape or non-finite values")
            prob = probability.squeeze(0).cpu().numpy()
            if not np.isfinite(prob).all() or prob.min() < 0 or prob.max() > 1:
                raise RuntimeError("Invalid probability raster")
            del logits, probability, map_tensor, legend_tensor, model
            torch.cuda.empty_cache()
        mask = (prob >= 0.5).astype(np.uint8) * 255
        np.save(args.output / "probability.npy", prob)
        Image.fromarray(prob, mode="F").save(args.output / "probability.tif")
        Image.fromarray(mask).save(args.output / "mask.png")
        overlay = image_array.copy()
        overlay[mask > 0] = ((overlay[mask > 0].astype(np.uint16) + np.array([0, 160, 255])) // 2).astype(np.uint8)
        Image.fromarray(overlay).save(args.output / "overlay.png")
        reopened = np.asarray(Image.open(args.output / "probability.tif"))
        if reopened.shape != (256, 256) or not np.array_equal(reopened, prob):
            raise RuntimeError("Float TIFF raster readback mismatch")
        result["probability"] = {"min": float(prob.min()), "max": float(prob.max()), "mean": float(prob.mean()),
                                 "foreground_pixels": int((mask > 0).sum()), "finite": bool(np.isfinite(prob).all())}
        result["outputs_sha256"] = {p.name: sha(p) for p in args.output.iterdir() if p.is_file()}
        result["status"] = "PASS"
        result["limitations"] = ["Synthetic execution only; no accuracy or field-validation claim",
                                  "Checkpoint-specific standalone component, not full UIUC pipeline or YOLO legend detector",
                                  "Raw pixel TIFF has no geographic transform/EPSG; GeoPackage/TA1 export not supplied by this model interface"]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (args.output / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "error": result.get("error"), "output": str(args.output)}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
