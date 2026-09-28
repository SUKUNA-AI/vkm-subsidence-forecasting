"""DRW-I1 (``desktop``): the real draw.io Desktop CLI — export png/svg/pdf, 1-based pages, ``--layout``, preview.

Opt-in: ``VKM_TEST_DESKTOP=1`` (a CLI call can reach a draw.io window the user has open, so routine runs never start
the application). Without the opt-in or without draw.io the tests are skipped = NOT_RUN.
"""
from __future__ import annotations

import os

import pytest

from vkm_drawio.locate import locate
from vkm_drawio.model import DiagramSpec
from vkm_drawio.service import DrawioService, png_size
from vkm_drawio.workspace import Workspace
from vkm_drawio.xmlio import read_diagram_bytes

pytestmark = pytest.mark.desktop


@pytest.fixture(scope="module")
def location():
    if os.environ.get("VKM_TEST_DESKTOP") != "1":
        pytest.skip("NOT_RUN: set VKM_TEST_DESKTOP=1 to run draw.io Desktop tests")
    loc = locate()
    if not loc.found:
        pytest.skip(f"NOT_RUN: {loc.reason}")
    return loc


def test_export_layout_and_preview(location, tmp_path):
    svc = DrawioService(Workspace(public_root=tmp_path / "public", work_root=tmp_path / "work"),
                        locator=lambda: location)
    spec = DiagramSpec.model_validate({"pages": [
        {"id": "p1", "name": "Поток", "layout": "drawio:verticalFlow", "nodes": [
            {"id": "a", "label": "Источник"}, {"id": "b", "label": "Страница"}, {"id": "c", "label": "Рисунок"}],
         "edges": [{"id": "e1", "source": "a", "target": "b"}, {"id": "e2", "source": "b", "target": "c"}]},
        {"id": "p2", "name": "Вторая", "nodes": [{"id": "x", "label": "X", "x": 0, "y": 0},
                                                  {"id": "y", "label": "Y", "x": 400, "y": 0}]}]})
    created = svc.create("work", "d.drawio", spec)
    assert created["pages"][0]["layout_engine"].startswith("drawio ")
    page1 = {n["id"]: n for n in svc.read("work", "d.drawio")["pages"][0]["nodes"]}
    assert page1["a"]["y"] < page1["b"]["y"] < page1["c"]["y"]          # a vertical flow really happened
    svg = svc.export("work", "d.drawio", "svg")
    png = svc.export("work", "d.drawio", "png", page=2)
    pdf = svc.export("work", "d.drawio", "pdf", all_pages=True)
    assert svg["bytes"] > 0 and png["bytes"] > 0 and pdf["bytes"] > 0
    embedded = read_diagram_bytes((tmp_path / "work" / "d.p2.png").read_bytes())
    assert [c.id for c in embedded.pages[0].cells if c.vertex] == ["x", "y"]  # --page-index is 1-based
    assert (tmp_path / "work" / "d.pdf").read_bytes().startswith(b"%PDF")
    blob, meta = svc.preview("work", "d.drawio", page=2, max_side=300)
    assert max(png_size(blob)) <= 330 and meta["px"] == list(png_size(blob))
