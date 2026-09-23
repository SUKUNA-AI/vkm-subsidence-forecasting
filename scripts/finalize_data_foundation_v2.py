#!/usr/bin/env python3
"""Record final data-only verification, reader QA evidence and uncommitted diff.

Does not alter either dataset release. Run after the independent validator and
report builder. No model execution and no historical label parsing.
"""
from __future__ import annotations
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/"work/data_foundation_v2"
QA=ROOT/"artifacts/data_quality/scenario_simulation_v2"
OUT=QA/"reproducibility"
DOC=ROOT/"docs/reports/DATA_FOUNDATION_V2_RU.md"


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def item(path):
    return dict(path=path.relative_to(ROOT).as_posix(),size_bytes=path.stat().st_size,sha256=sha(path))


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def command(args):
    return subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding="utf-8",errors="replace")


def verify_manifest(path):
    data=json.loads(path.read_text(encoding="utf-8"))
    for section in ["inputs","outputs"]:
        for x in data[section]:
            p=(ROOT if section=="inputs" else path.parent)/x["path"]
            assert p.stat().st_size==x["size_bytes"] and sha(p)==x["sha256"],str(p)
    return item(path)


class Document(HTMLParser):
    def __init__(self):
        super().__init__();self.ids=[];self.links=[];self.images=[];self.headings=[]
    def handle_starttag(self,tag,attrs):
        d=dict(attrs)
        if "id" in d:self.ids.append(d["id"])
        if tag=="a":self.links.append(d.get("href",""))
        if tag=="img":self.images.append(d)
        if tag=="h2":self.headings.append(d.get("id"))


def main():
    # This file appends receipts to the new audit, not to the immutable datasets.
    if (QA/"finalization_manifest.json").exists():raise FileExistsError("Finalization is already frozen; preserve it.")
    for p in [ROOT/"data/scenario_simulation_v2/manifest.json",ROOT/"artifacts/reconstruction/scenario_constraints_v2/manifest.json",QA/"manifest.json"]:
        verify_manifest(p)
    OUT.mkdir(exist_ok=True)
    copies={
        "baseline_inventory.json":WORK/"baseline_inventory.json",
        "initial_git_status.txt":WORK/"initial_git_status.txt",
        "initial_commit.txt":WORK/"initial_commit.txt",
        "verified_inputs.json":WORK/"verified_inputs.json",
        "source_extraction_manifest.json":WORK/"source_text/extraction_manifest.json",
        "dataset_reproducibility.json":WORK/"dataset_reproducibility.json",
        "constraints_reproducibility.json":WORK/"constraints_reproducibility.json",
        "independent_validation_release.json":WORK/"independent_validation_release.json",
    }
    for name,source in copies.items():
        if (OUT/name).exists():assert sha(source)==sha(OUT/name)
        else:shutil.copyfile(source,OUT/name)
    independent=json.loads((OUT/"independent_validation_release.json").read_text(encoding="utf-8"))
    assert independent["status"]=="PASS" and independent["models_executed"]==0
    runs=[]
    cmds=[
        ["-m","pytest","tests/test_data_foundation_v2.py","tests/test_reconstruction_data.py","tests/test_reconstruction_atlas.py","-q","--basetemp=work/data_foundation_v2/pytest_receipt_v2"],
        ["-m","pytest","tests/test_scenario_simulation_v1.py","-k","catalog_is_full or static_temporal or generated_release or adapter_exposes","-q","--basetemp=work/data_foundation_v2/pytest_receipt_v1"],
    ]
    if (OUT/"test_execution.json").exists():
        receipt=json.loads((OUT/"test_execution.json").read_text(encoding="utf-8"))
        assert receipt["status"]=="PASS"
        runs=receipt["runs"]
    else:
        for i,args in enumerate(cmds,1):
            result=command([sys.executable,*args])
            log=OUT/f"pytest_{i}.txt";log.write_text(result.stdout+result.stderr,encoding="utf-8")
            runs.append(dict(command=[".venv/Scripts/python.exe",*args],exit_code=result.returncode,log=log.relative_to(ROOT).as_posix()))
            assert result.returncode==0,result.stdout+result.stderr
    assert "36 passed" in (OUT/"pytest_1.txt").read_text(encoding="utf-8")
    assert "4 passed, 3 deselected" in (OUT/"pytest_2.txt").read_text(encoding="utf-8")
    write(OUT/"test_execution.json",dict(status="PASS",passed=40,runs=runs,scope="data/reconstruction only; no model fits",repository_wide_tests_run=False))
    # Independently recheck all outputs, including byte identity to both scratch runs.
    comparisons=[]
    for release,first,second in [(ROOT/"data/scenario_simulation_v2",WORK/"dataset_final1",WORK/"dataset_final2"),(ROOT/"artifacts/reconstruction/scenario_constraints_v2",WORK/"constraints_final1",WORK/"constraints_final2")]:
        paths=sorted(p.relative_to(release) for p in release.rglob("*") if p.is_file())
        assert paths==sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
        assert paths==sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
        for p in paths:assert sha(release/p)==sha(first/p)==sha(second/p),str(p)
        comparisons.append(dict(release=release.relative_to(ROOT).as_posix(),files=len(paths),both_runs_and_release_identical=True))
    baseline=json.loads((OUT/"baseline_inventory.json").read_text(encoding="utf-8"));changed=[]
    for x in baseline:
        p=ROOT/x["path"]
        if not p.is_file() or p.stat().st_size!=x["size_bytes"] or sha(p)!=x["sha256"]:changed.append(x["path"])
    assert not changed,changed
    old=pd.read_csv(ROOT/"data/scenario_simulation_v1/campaign_observations.csv.gz")
    new_roster=pd.read_csv(ROOT/"data/scenario_simulation_v2/point_roster.csv")
    new_calendar=pd.read_csv(ROOT/"data/scenario_simulation_v2/campaign_catalog.csv")
    old_roster=old[["base_point_id","base_profile_id"]].drop_duplicates().rename(columns={"base_point_id":"point_id","base_profile_id":"profile_id"}).sort_values("point_id").reset_index(drop=True)
    pd.testing.assert_frame_equal(old_roster,new_roster[list(old_roster)].sort_values("point_id").reset_index(drop=True))
    # v1 serialized IDs, not x/y; bind those same IDs to its unchanged geometry
    # source. Do not pretend x/y or QA quadrant labels existed in v1 observations.
    cfg=json.loads((ROOT/"configs/scenario_simulation_v2.json").read_text(encoding="utf-8"))
    geo=pd.read_csv(ROOT/cfg["foundation"]["geometry"],usecols=["point_id","profile_id","x_local_m","y_local_m","chainage_m"])
    old_geo=old_roster.merge(geo,on=["point_id","profile_id"],validate="one_to_one").sort_values("point_id").reset_index(drop=True)
    pd.testing.assert_frame_equal(old_geo,new_roster[list(old_geo)].sort_values("point_id").reset_index(drop=True),check_exact=False,atol=1e-8)
    assert sorted(old.date.unique())==sorted(new_calendar.date.unique())
    write(OUT/"release_integrity.json",dict(status="PASS",comparisons=comparisons,old_files_verified=len(baseline),changed_old_files=changed,geometry_and_dates_unchanged=True,geometry_columns_checked=list(old_geo),geometry_verification="same serialized point/profile IDs mapped to unchanged source coordinates; zones are a common QA partition"))
    # Human image review was performed by the assistant using view_image on all
    # ten final PNGs; this receipt binds that review to exact file hashes.
    charts=json.loads((QA/"chart_manifest.json").read_text(encoding="utf-8"))
    write(OUT/"reader_qa.json",dict(status="PASS_PNG_REVIEW",review_method="assistant visual inspection of all 10 final PNGs through view_image; source PDF figures inspected separately",images=[dict(**item(QA/x["file"]),claim=x["claim"],review="readable axes/legends; no observed clipping; provenance caveat retained") for x in charts],html_validation="mechanical structure/links only; full browser interaction not tested",no_real_temporal_curve_overlay=True,unknown_points_not_interpolated=True))
    # Check the report contract, complete table shapes, local links and HTML TOC.
    md=DOC.read_text(encoding="utf-8");lines=md.splitlines()
    assert md.count("```")%2==0 and "@@" not in md
    expected=["A. EXECUTIVE SUMMARY","B. EMPIRICAL EVIDENCE AUDIT","C. DATASET CHANGELOG","D. OLD VS NEW REPORT","E. REMAINING ASSUMPTIONS","F. MODEL MIGRATION PLAN","G. IMM IMPROVEMENT DESIGN NOTE","H. REPRODUCIBILITY REPORT"]
    assert [s[3:] for s in lines if s.startswith("## ")]==expected
    table_width=None
    for s in lines:
        if s.startswith("|"):
            width=len(s.split("|"))
            if table_width is None:table_width=width
            assert width==table_width
        else:table_width=None
    # A few final receipts are written just below, so permit exactly those names.
    pending={"git_status.txt","change_inventory.json","new_text_files.patch","finalization_manifest.json"}
    local_links=[]
    for target in re.findall(r"\]\(([^)]+)\)",md):
        if re.match(r"https?://",target):continue
        p=(DOC.parent/unquote(target.split("#")[0])).resolve()
        assert p.is_relative_to(ROOT)
        assert p.is_file() or p.name in pending,str(p)
        local_links.append(p.relative_to(ROOT).as_posix())
    doc=Document();doc.feed(DOC.with_suffix(".html").read_text(encoding="utf-8"))
    assert len(doc.images)==10 and len(doc.headings)==8 and len(doc.ids)==len(set(doc.ids))
    assert all(im.get("alt") and im.get("src","").startswith("data:image/png;base64,") for im in doc.images)
    assert all(x[1:] in doc.ids for x in doc.links if x.startswith("#"))
    for p in [ROOT/"scripts/build_data_foundation_v2_report.py",Path(__file__)]:ast.parse(p.read_text(encoding="utf-8"))
    write(OUT/"report_structure.json",dict(status="PASS",required_sections=expected,embedded_figures=10,local_links_checked=len(local_links),tables_rectangular=True,fences_balanced=True,html_toc_valid=True,browser_visual_check=False))
    status=command(["git","status","--short"]);assert status.returncode==0
    (OUT/"git_status.txt").write_text(status.stdout,encoding="utf-8")
    tracked=command(["git","diff","--no-ext-diff"]);assert tracked.returncode==0
    (OUT/"tracked.diff").write_text(tracked.stdout,encoding="utf-8")
    initial_paths={x["path"] for x in baseline}
    new_text=[ROOT/p for p in ["configs/scenario_constraints_v2.json","configs/scenario_simulation_v2.json","src/skru1/empirical_constraints_v2.py","src/skru1/scenario_simulation_v2.py","scripts/build_scenario_constraints_v2.py","scripts/generate_scenario_v2.py","scripts/audit_scenario_v2.py","scripts/validate_scenario_v2.py","scripts/build_data_foundation_v2_report.py","scripts/finalize_data_foundation_v2.py","tests/test_data_foundation_v2.py","docs/governance/SCENARIO_EXPERIMENT_V2_PROTOCOL.md","docs/reports/IMM_V2_IMPROVEMENT_DESIGN_RU.md","docs/reports/DATA_FOUNDATION_V2_RU.md"]]
    assert all(p.relative_to(ROOT).as_posix() not in initial_paths for p in new_text)
    patches=[]
    for p in new_text:
        result=command(["git","diff","--no-index","--no-ext-diff","--","NUL",p.relative_to(ROOT).as_posix()])
        assert result.returncode==1,result.stderr
        assert result.stdout.startswith("diff --git")
        patches.append(result.stdout)
    (OUT/"new_text_files.patch").write_text("\n".join(patches),encoding="utf-8")
    write(OUT/"change_inventory.json",dict(initial_git_status_file="initial_git_status.txt",current_git_status_file="git_status.txt",tracked_diff_empty=not tracked.stdout,old_files_unchanged=len(baseline),new_text_files=[item(p) for p in new_text],generated_release_manifests=[item(ROOT/"data/scenario_simulation_v2/manifest.json"),item(ROOT/"artifacts/reconstruction/scenario_constraints_v2/manifest.json")],initial_untracked_files_preserved=True,auto_commit=False))
    files=[p for p in QA.rglob("*") if p.is_file() and p.name!="finalization_manifest.json"]+new_text+[DOC.with_suffix(".html")]
    write(QA/"finalization_manifest.json",dict(status="PASS_DATA_RELEASE_COMPLETE_NO_MODELS",inputs=[item(ROOT/"data/scenario_simulation_v2/manifest.json"),item(ROOT/"artifacts/reconstruction/scenario_constraints_v2/manifest.json")],files=[item(p) for p in sorted(set(files))],models_executed=0,legacy_holdout_labels_parsed=0,old_files_verified=len(baseline),commit_created=False))
    print(json.dumps(dict(status="PASS",tests=40,origins=independent["rows_recomputed"],old_files_unchanged=len(baseline),report=DOC.relative_to(ROOT).as_posix(),new_text_files=len(new_text)),ensure_ascii=False))


if __name__=="__main__":main()
