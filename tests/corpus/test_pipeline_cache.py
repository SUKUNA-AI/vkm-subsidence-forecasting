"""Artifact store and stage/call caches of the producer root (agent C): write-once blobs, the staging index,
reproducible (not stored) renders, call attempts, pixel-based call signatures independent of the PNG encoder."""
from __future__ import annotations

import pytest

from vkm_corpus.artifacts.store import ArtifactStore, load_index
from vkm_corpus.pipeline.cache import StageCache


def test_store_write_once_and_index(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts", index_path=tmp_path / "cache" / "artifacts" / "run=x" / "a.jsonl")
    a = store.put_bytes(b"synthetic-bytes", "OCR_RAW", "application/json", source_id="VKM-SRC-900")
    b = store.put_bytes(b"synthetic-bytes", "OCR_RAW", "application/json")
    assert a.artifact_id == b.artifact_id and a.storage_relpath.startswith("ocr_raw/")
    assert store.read_bytes(a.artifact_id) == b"synthetic-bytes"
    j = store.put_json({"b": 1, "a": [1, 2]}, "NATIVE_RAW", compress=True)
    assert store.read_json(j.artifact_id) == {"a": [1, 2], "b": 1}
    j2 = store.put_json({"a": [1, 2], "b": 1}, "NATIVE_RAW", compress=True)
    assert j2.artifact_id == j.artifact_id  # canonical JSON + gzip without timestamp
    r = store.register_reproducible("f" * 64, 100, "PAGE_RENDER", "image/x-portable-pixmap",
                                    {"renderer": "pymupdf", "profile": "r200c"})
    assert r.materialization == "NOT_STORED_REPRODUCIBLE" and r.storage_relpath is None
    with pytest.raises(ValueError):
        store.register_reproducible("e" * 64, 1, "OCR_RAW", "application/json", {"x": 1})
    store.close()
    idx = load_index(tmp_path / "cache" / "artifacts")
    assert set(idx) == {a.artifact_id, j.artifact_id, r.artifact_id}
    assert [x.artifact_id for x in store.take_records()] == [a.artifact_id, j.artifact_id, r.artifact_id]


def test_hash_mismatch_is_detected(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    a = store.put_bytes(b"one", "RUN_LOG", "text/plain")
    (tmp_path / "artifacts" / a.storage_relpath).write_bytes(b"two")
    with pytest.raises(RuntimeError, match="ARTIFACT_HASH_MISMATCH"):
        store.read_bytes(a.artifact_id)


def test_call_cache_attempts_and_reload(tmp_path):
    c = StageCache(tmp_path, "RUN-20260928T000000Z-00000001", writer_name="t")
    sig = "a" * 64
    assert c.best_call(sig) is None and c.next_attempt(sig) == 1
    c.add_call(call_signature=sig, attempt=1, status="ERROR", raw_artifact_id="sha256:" + "1" * 64,
               input_artifact_id=None, kind="OCR", source_id="VKM-SRC-900", page_id=None)
    c.add_call(call_signature=sig, attempt=2, status="OK", raw_artifact_id="sha256:" + "2" * 64,
               input_artifact_id=None, kind="OCR", source_id="VKM-SRC-900", page_id=None)
    c.add_stage(stage_signature="b" * 64, stage="PREPARE", source_id="VKM-SRC-900", page_index=None, outputs={"n": 1})
    c.close()
    c2 = StageCache(tmp_path, "RUN-20260928T000001Z-00000002", writer_name="t2")
    assert c2.best_call(sig)["attempt"] == 2 and c2.next_attempt(sig) == 3
    assert c2.get_stage("b" * 64)["outputs"] == {"n": 1}
    assert c2.latest_stage("PREPARE", "VKM-SRC-900") is not None


def test_call_signature_is_pixel_based():
    pil = pytest.importorskip("PIL.Image")
    from vkm_corpus.artifacts.render import png_bytes
    from vkm_corpus.contracts.signatures import call_signature, pixel_sha256

    img = pil.new("L", (40, 20), 200)
    p1 = pixel_sha256(img.mode, img.width, img.height, img.tobytes())
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    assert buf.getvalue() != png_bytes(img)  # different encodings …
    reloaded = pil.open(io.BytesIO(buf.getvalue()))
    p2 = pixel_sha256(reloaded.mode, reloaded.width, reloaded.height, reloaded.tobytes())
    assert p1 == p2  # … same pixels, same cache key
    kw = dict(model_id="m", model_revision="r", weights_sha256="w" * 64, prompt="Text Recognition:",
              sampling={"temperature": 0})
    assert call_signature(**kw, input_pixel_sha256=p1) == call_signature(**kw, input_pixel_sha256=p2)
    assert call_signature(**{**kw, "prompt": "Table Recognition:"}, input_pixel_sha256=p1) != \
        call_signature(**kw, input_pixel_sha256=p1)


def test_band_rows_cut_at_blank_rows():
    pil = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw

    from vkm_corpus.pipeline.ocr_stage import band_rows

    img = pil.new("L", (300, 1000), 255)
    d = ImageDraw.Draw(img)
    for y in range(0, 1000, 100):  # ten "rows" of ink with blank gaps between them
        d.rectangle([10, y + 10, 290, y + 80], fill=0)
    bands = band_rows(img, 450)
    assert bands[0][0] == 0 and bands[-1][1] == 1000
    assert all(b - a <= 450 for a, b, _ in bands) and not any(hard for _, _, hard in bands)
    assert all(a == 0 or 81 <= a % 100 <= 109 for a, _, _ in bands)  # cuts fall into blank gaps
    assert band_rows(img, 450) == bands  # deterministic
    assert band_rows(img, 0) == [(0, 1000, False)]
    # no sliver at the bottom: 604 px with a 600 px limit stays one band (a 4 px strip was rejected by the server)
    tall = pil.new("L", (900, 604), 255)
    ImageDraw.Draw(tall).rectangle([10, 10, 890, 594], outline=0, width=2)
    assert band_rows(tall, 600) == [(0, 604, False)]
    two = band_rows(pil.new("L", (900, 700), 255), 600)
    assert len(two) == 2 and two[-1][1] - two[-1][0] >= 64


def test_banded_table_image_is_the_whole_region(tmp_path):
    """A banded table's image is its band inputs stacked in order (TABLE_CROP) — exactly the crop before banding."""
    import io
    from types import SimpleNamespace

    pil = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw

    from vkm_corpus.artifacts.render import png_bytes
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.pipeline.ocr_stage import band_rows

    img = pil.new("L", (300, 1000), 255)
    d = ImageDraw.Draw(img)
    for y in range(0, 1000, 100):
        d.rectangle([10, y + 10, 290, y + 80], fill=y // 10)
    store = ArtifactStore(tmp_path / "artifacts")
    found = []
    for i, (top, bottom, _) in enumerate(band_rows(img, 450)):
        rec = store.put_bytes(png_bytes(img.crop((0, top, 300, bottom))), "OCR_INPUT", "image/png")
        found.append((SimpleNamespace(band_index=i), {"input_artifact_id": rec.artifact_id}, {}))
    fake = SimpleNamespace(store=store, src=SimpleNamespace(source_id="VKM-SRC-900"))
    page, spec = SimpleNamespace(page_index=3), SimpleNamespace(crop_dpi=200)
    aid = Assembler._region_image(fake, page, spec, list(reversed(found)))
    full = pil.open(io.BytesIO(store.read_bytes(aid)))
    assert full.size == img.size and full.tobytes() == img.tobytes()
    assert store.find(aid).parent.parent.parent.name == "tables"  # TABLE_CROP
    one = found[:1]
    assert Assembler._region_image(fake, page, spec, one) == one[0][1]["input_artifact_id"]


def test_manifest_crops_from_the_main_and_reocr_parts(tmp_path):
    """The assembler/plan task list of a re-OCR'd page is found from the two manifests the OCR phase records (main
    tasks, REOCR tasks) — no re-render; cached tasks are recognised by pixel-based call signatures."""
    from pathlib import Path

    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.ocr_stage import (CropOut, OcrTaskSpec, cached_specs, call_signature_for,
                                               crop_plan_signature, crops_to_manifest, manifest_crops)

    cfg = PipelineConfig(data_root=Path(tmp_path), resources_root=Path(tmp_path))
    cache = StageCache(tmp_path, "RUN-20260928T000000Z-00000003", writer_name="t")

    def spec(role, task, rank):
        return OcrTaskSpec(source_id="VKM-SRC-900", page_index=4, page_id="VKM-SRC-900:p0004", task=task, role=role,
                           crop_dpi=200, det_index=rank, bbox_pt=(10.0, 10.0 * rank, 100.0, 10.0 * rank + 8),
                           order_rank=rank)

    full = [spec("TABLE", "table", 1), spec("REOCR", "text", 2), spec("FORMULA", "formula", 3),
            spec("REOCR", "text", 4)]
    sha = "c" * 64
    for idxs in ([0, 2], [1, 3]):  # as recorded by the main pass and by the scenario-B pass
        sub = [full[i] for i in idxs]
        crops = [CropOut(spec=s, png=b"", png_sha256="0" * 64, pixel_sha256=f"{i:064x}", width=10, height=10,
                         mode="L", ink=0.1, crop_box_px=[0, 0, 10, 10], spec_index=j)
                 for j, (i, s) in enumerate(zip(idxs, sub))]
        cache.add_stage(stage_signature=crop_plan_signature(cfg, "PDF", sha, sub), stage="OCR_CROPS",
                        source_id="VKM-SRC-900", page_index=4, outputs={"crops": crops_to_manifest(crops)})
    got = manifest_crops(cfg, cache, "PDF", sha, full)
    assert got is not None and sorted(c.spec_index for c in got) == [0, 1, 2, 3]
    assert all(c.spec is full[c.spec_index] and c.pixel_sha256 == f"{c.spec_index:064x}" for c in got)
    for c in got[:2]:  # two of the four tasks have a successful cached call
        cache.add_call(call_signature=call_signature_for(cfg, c.spec.task, c.pixel_sha256), attempt=1, status="OK",
                       raw_artifact_id=None, input_artifact_id=None, kind="OCR", source_id="VKM-SRC-900",
                       page_id=c.spec.page_id)
    assert cached_specs(cfg, cache, got) == {got[0].spec_index, got[1].spec_index}
    assert manifest_crops(cfg, cache, "PDF", "d" * 64, full) is None  # no manifest → render


def test_pipeline_config_roundtrip():
    from pathlib import Path

    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.context import config_from_json, config_to_json

    cfg = PipelineConfig(data_root=Path("d"), resources_root=Path("r"), ocr_url="http://127.0.0.1:8080")
    cfg.scenario_b.enabled = False
    data = config_to_json(cfg)
    data["obsolete_field"] = 1          # tolerated: a run started with an older configuration schema
    data["ocr_crop"]["old_key"] = 300
    back = config_from_json(data)
    assert back.stage_config("OCR") == cfg.stage_config("OCR") and back.scenario_b.enabled is False
