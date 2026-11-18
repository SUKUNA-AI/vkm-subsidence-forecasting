import json

import numpy as np
import pytest

from scripts.build_spatial_model import code_paths
from vkm_world.geometry_build import geology
from vkm_world.geometry_build.geology import grouped_cv,number,predict,qualified_matches,unique_depths,render_source_columns


def test_qualified_matching_preserves_area_and_does_not_match_bare_number():
    symbols=[{"borehole_id_catalogue":"BH-AREA1-7","catalogue_match":"HIGH"},
             {"borehole_id_catalogue":"BH-AREA2-7","catalogue_match":"LOW"}]
    picks=[{"borehole_id":"BH-AREA2-7","pick_id":"other-site"}]
    joined=qualified_matches(symbols,picks)
    assert len(joined)==1 and joined[0]["picks"]==[]
    assert not joined[0]["accepted_physical_identity"]


def test_geology_recipe_pins_the_checked_symbol_reviews_points_and_policy(tmp_path):
    configuration = tmp_path / geology.SECTION
    configuration.parent.mkdir(parents=True)
    configuration.write_text(json.dumps({"image": "image.png", "source": {"document": "origin.pdf"}}),
                             encoding="utf-8")
    paths = set(geology.input_paths(tmp_path))
    assert {geology.SYMBOLS.with_name(name).as_posix() for name in (
        "skru1_boreholes_checked.csv", "skru1_points.geojson",
    )} <= paths
    assert "src/vkm_world/geometry_build/mining.py" in code_paths("geology")


def test_qualified_matching_rejects_two_positions_for_one_catalogue_id():
    symbols = [{"key": key, "borehole_id_catalogue": "BH-ONE-7", "catalogue_match": "HIGH"}
               for key in ("A", "B")]
    picks = [{"borehole_id": "BH-ONE-7", "pick_id": "pick-7"}]
    with pytest.raises(ValueError, match="Ambiguous qualified borehole position.*BH-ONE-7"):
        qualified_matches(symbols, picks)


def test_each_qualified_position_uses_only_its_own_picks():
    ids = ("BH-AREA1-7", "BH-AREA2-7", "BH-AREA2-75")
    symbols = [{"borehole_id_catalogue": well, "catalogue_match": "HIGH"} for well in ids]
    picks = [{"borehole_id": ids[0], "pick_id": "pick-a", "top_depth_m": "9"},
             {"borehole_id": ids[1], "pick_id": "pick-b", "top_depth_m": "13"}]
    linked = qualified_matches(symbols, picks)
    assert [[row["top_depth_m"] for row in result["picks"]] for result in linked] == [["9"], ["13"], []]


def test_repeated_depths_do_not_add_support_and_conflicts_are_not_averaged():
    def row(identity,value,well="A"):
        return {"pick_id":identity,"borehole_id":well,"source_id":"VKM-SRC-012",
                "reference":"depth_below_collar","units":"m","unit_canonical_guess":"U","top_depth_m":value}
    values,conflicts=unique_depths([row("a",10),row("b",10),row("c",20,"B"),row("d",21,"B")])
    assert values[("A","U")]=={"depth_m":10.,"pick_ids":["a","b"]}
    assert ("B","U") not in values and len(conflicts)==1
    assert number("UNKNOWN") is None and number("nan") is None


def test_cv_withholds_every_row_at_the_same_borehole_and_retains_non_predictions():
    obs=[{"borehole_id":well,"x":x,"y":y,"value_m":z}
         for well,x,y,z in [("A",0,0,1),("A",0,0,2),("B",10,0,5),("C",0,10,7),("D",10,10,9)]]
    result=grouped_cv(obs)
    assert result["predictions"]
    assert all(row["held_well"] not in row["training_wells"] for row in result["predictions"])
    assert result["summary"]["linear"]["not_predicted"]>0
    assert predict(np.array([[0.,0.],[10.,0.],[0.,10.]]),np.array([1.,2.,3.]),np.array([100.,100.]),"linear") is None


def test_column_png_is_identical_across_host_styles_and_preserves_source_rows(tmp_path):
    import copy
    import matplotlib
    matplotlib.get_backend()  # Resolve the lazy backend before comparing the caller's selected style.
    rows=[{"borehole_id":"DEPTH","reference":"depth_below_collar","top_depth_m":"10","bottom_depth_m":"20",
           "top_abs_m":"","bottom_abs_m":"","unit_canonical_guess":"A very long source label that must stay inside its own column",
           "unit_as_printed":"full raw label"},
          {"borehole_id":"ABSOLUTE","reference":"absolute_elevation","top_depth_m":"","bottom_depth_m":"",
           "top_abs_m":"140","bottom_abs_m":"120","unit_canonical_guess":"","unit_as_printed":"Unit B"}]
    original=copy.deepcopy(rows)
    images=[]
    for index,style in enumerate([{"font.size":8},{"font.size":10},
                                  {"font.size":25,"font.family":["monospace"],"axes.facecolor":"pink",
                                   "xtick.labelsize":20,"ytick.labelsize":24,"savefig.facecolor":"yellow",
                                   "figure.autolayout":True,"lines.linewidth":5}]):
        with matplotlib.rc_context(rc=style):
            before=matplotlib.rcParams.copy()
            path=tmp_path/f"columns_{index}.png"
            stats=render_source_columns(rows,path)
            assert matplotlib.rcParams==before
            images.append(path.read_bytes())
    assert images[0]==images[1]==images[2]
    assert rows==original
    assert stats=={"columns":2,"shortened_labels":1}
