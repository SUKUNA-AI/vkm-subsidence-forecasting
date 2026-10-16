import json
from pathlib import Path

import pytest

from vkm_world.geometry_build.pipeline import pinned_files, run_step, verify_pins


def test_pins_reject_drift_unloaded_lfs_and_escape(tmp_path):
    (tmp_path / "source.txt").write_text("original", encoding="utf-8")
    pins = pinned_files(tmp_path, ["source.txt"])
    assert verify_pins(tmp_path, pins)["source.txt"] == pins[0]["sha256"]
    (tmp_path / "source.txt").write_text("edited", encoding="utf-8")
    with pytest.raises(ValueError, match="pins changed"):
        verify_pins(tmp_path, pins)
    (tmp_path / "source.txt").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:x\n")
    with pytest.raises(ValueError, match="LFS"):
        pinned_files(tmp_path, ["source.txt"])
    with pytest.raises(ValueError):
        pinned_files(tmp_path, ["../escape"])


def test_step_invalidates_changed_control_without_rebuilding_terrain(tmp_path):
    calls = []

    def build(path):
        calls.append(1)
        path.mkdir()
        (path / "result.json").write_text('{"status":"DERIVATION"}\n')

    def run(name, version, destination, dependencies=None):
        return run_step(name, cache=tmp_path / "cache", destination=tmp_path / destination,
                        inputs={"control": version}, parameters={}, code={"module": "v1"},
                        dependencies=dependencies or {}, builder=build)

    terrain = run("terrain", "DEM_v1", "terrain_a")
    run("geology", "GCP_v1", "geology_a")
    terrain2 = run("terrain", "DEM_v1", "terrain_b")
    geology2 = run("geology", "GCP_v2", "geology_b")
    assert terrain2["cache_hit"] and terrain2["key"] == terrain["key"]
    assert not geology2["cache_hit"] and len(calls) == 3
    run("scene", "constant", "scene_a", {"geology": "v1"})
    changed = run("scene", "constant", "scene_b", {"geology": "v2"})
    assert not changed["cache_hit"]


def test_cache_hashes_outputs_and_does_not_overwrite_destination(tmp_path):
    def build(path):
        path.mkdir()
        (path / "data.txt").write_text("true result")

    args = dict(name="geology", cache=tmp_path / "cache", inputs={}, parameters={}, code={},
                dependencies={}, builder=build)
    result = run_step(destination=tmp_path / "first", **args)
    with pytest.raises(FileExistsError):
        run_step(destination=tmp_path / "first", **args)
    cached = tmp_path / "cache" / "geology" / result["key"] / "bundle" / "data.txt"
    cached.write_text("tampered result")
    with pytest.raises(ValueError, match="cache integrity"):
        run_step(destination=tmp_path / "second", **args)
    assert not (tmp_path / "second").exists()


def test_recipe_rejects_new_discovered_input_and_incomplete_code_pins(tmp_path, monkeypatch):
    from scripts import build_spatial_model as runner

    (tmp_path / "source.csv").write_text("observed",encoding="utf-8")
    (tmp_path / "builder.py").write_text("recipe",encoding="utf-8")
    (tmp_path / "runner.py").write_text("integration",encoding="utf-8")
    monkeypatch.setattr(runner,"ROOT",tmp_path)
    monkeypatch.setattr(runner,"code_paths",lambda name:["builder.py"])
    monkeypatch.setattr(runner,"integration_paths",lambda:["runner.py"])
    config = {"stages":{"geology":{"private_inputs":pinned_files(tmp_path,["source.csv"]),
                                    "code":pinned_files(tmp_path,["builder.py"])}},
              "integration_code":pinned_files(tmp_path,["runner.py"])}
    runner.validate_recipe_pins(tmp_path,config,{"geology":["source.csv"]})
    with pytest.raises(ValueError,match="input file set changed"):
        runner.validate_recipe_pins(tmp_path,config,{"geology":["source.csv","new_control.csv"]})
    config["integration_code"] = []
    with pytest.raises(ValueError,match="integration code file set changed"):
        runner.validate_recipe_pins(tmp_path,config,{"geology":["source.csv"]})


def test_scene_comparison_retains_rendered_colour_and_interval_subset(tmp_path):
    import zipfile
    from scripts.verify_spatial_model import qgz_scene_digest

    def save(name, colour, subset, uid):
        path=tmp_path/name
        xml=(f'<qgis><title>scene</title><projectlayers><maplayer>'
             f'<layername>interval</layername><datasource>./data.gpkg</datasource>'
             f'<provider>ogr</provider><subsetstring>{subset}</subsetstring>'
             f'<renderer-v2 type="singleSymbol"><symbols><symbol><layer id="{uid}">'
             f'<Option name="color" value="{colour}"/></layer></symbol></symbols>'
             f'<ranges><range lower="0" upper="1" uuid="{uid}"/></ranges>'
             f'<categories><category value="rock" label="rock" uuid="{uid}"/></categories>'
             f'</renderer-v2></maplayer></projectlayers></qgis>')
        with zipfile.ZipFile(path,"w") as archive:
            archive.writestr("scene.qgs",xml)
        return qgz_scene_digest(path)

    a=save("a.qgz","red","A","{00000000-0000-0000-0000-000000000001}")
    assert a==save("b.qgz","red","A","{00000000-0000-0000-0000-000000000002}")
    assert a!=save("c.qgz","blue","A","{00000000-0000-0000-0000-000000000001}")
    assert a!=save("d.qgz","red","B","{00000000-0000-0000-0000-000000000001}")
