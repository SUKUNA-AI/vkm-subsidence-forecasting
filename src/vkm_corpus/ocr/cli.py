"""``vkm-corpus ocr``: GLM-OCR server check and a smoke test on synthetic images (no source pages involved).

    ocr check    health, engine version, served model and pinned-revision check
    ocr smoke    recognise three synthetic images (text, formula, table) and print outputs and timings
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
from typing import Any


def register(subparsers: Any) -> None:
    p = subparsers.add_parser("ocr", help="GLM-OCR server check and synthetic smoke test")
    sub = p.add_subparsers(dest="ocr_cmd", metavar="<command>")
    c = sub.add_parser("check", help="server identity and pinned revision")
    c.set_defaults(func=cmd_check)
    s = sub.add_parser("smoke", help="synthetic text / formula / table recognition")
    s.set_defaults(func=cmd_smoke)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])


def _url() -> str:
    from vkm_corpus.config import ConfigError, load_settings

    url = load_settings().ocr_url
    if not url:
        raise ConfigError("VKM_OCR_URL is not set")
    return url


def cmd_check(args: argparse.Namespace) -> int:
    from vkm_corpus.ocr.client import GlmOcrClient
    from vkm_corpus.ocr.prompts import DEFAULT_MODEL

    async def go() -> dict[str, Any]:
        async with GlmOcrClient(_url()) as c:
            return await c.server_info()

    info = asyncio.run(go())
    root = None
    try:
        root = info["models"]["data"][0]["root"]
    except (KeyError, IndexError, TypeError):
        pass
    info["pinned_revision"] = DEFAULT_MODEL.model_revision
    info["revision_ok"] = bool(root) and str(root).rstrip("/").endswith(DEFAULT_MODEL.model_revision)
    if isinstance(info.get("models"), dict):
        for m in info["models"].get("data", []):
            m.pop("root", None)  # a container path, not needed in the output
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0 if info["revision_ok"] else 5


def synthetic_images() -> list[tuple[str, str, Any]]:
    from PIL import Image, ImageDraw, ImageFont

    from vkm_corpus.extract.synthetic import font_file

    def font(size: int):
        # PIL resolves bare names in the OS font directories; font_file() adds VKM_FONT_FILE and fontconfig
        for name in (font_file(), "DejaVuSans.ttf", "arial.ttf"):
            if not name:
                continue
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    text = Image.new("L", (1200, 260), 255)
    d = ImageDraw.Draw(text)
    d.text((30, 40), "Синтетический тест распознавания 2026", fill=0, font=font(44))
    d.text((30, 140), "Synthetic recognition test line two", fill=0, font=font(44))
    formula = Image.new("L", (900, 180), 255)
    ImageDraw.Draw(formula).text((30, 50), "E = m c^2 + a/b    (1.1)", fill=0, font=font(56))
    table = Image.new("L", (900, 300), 255)
    d = ImageDraw.Draw(table)
    rows = [["A", "B", "C"], ["1", "2", "3"], ["4", "5", "6"]]
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            x0, y0 = 20 + c * 280, 20 + r * 85
            d.rectangle([x0, y0, x0 + 280, y0 + 85], outline=0, width=2)
            d.text((x0 + 20, y0 + 20), cell, fill=0, font=font(40))
    return [("text", "Text Recognition:", text), ("formula", "Formula Recognition:", formula),
            ("table", "Table Recognition:", table)]


def cmd_smoke(args: argparse.Namespace) -> int:
    from vkm_corpus.artifacts.render import png_bytes
    from vkm_corpus.artifacts.store import sha256_hex
    from vkm_corpus.contracts.signatures import pixel_sha256
    from vkm_corpus.ocr.client import GlmOcrClient, OcrRequest

    async def go() -> list[dict[str, Any]]:
        out = []
        async with GlmOcrClient(_url(), concurrency=3) as c:
            reqs = []
            for task, prompt, img in synthetic_images():
                png = png_bytes(img)
                reqs.append(OcrRequest(task=task, prompt=prompt, png=png, png_sha256=sha256_hex(png),
                                       pixel_sha256=pixel_sha256(img.mode, img.width, img.height, img.tobytes()),
                                       width=img.width, height=img.height, mode=img.mode))
            for r in await asyncio.gather(*(c.recognize(q) for q in reqs)):
                out.append({"task": r.request.task, "status": r.status, "finish_reason": r.finish_reason,
                            "latency_ms": r.latency_ms, "usage": r.usage, "content": r.content})
        return out

    res = asyncio.run(go())
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if all(r["status"] == "OK" for r in res) else 6
