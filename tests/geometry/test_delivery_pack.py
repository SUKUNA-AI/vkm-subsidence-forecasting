"""Meaningful synthetic security/reproducibility tests for the delivery packer."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

MODULE_PATH=Path(__file__).resolve().parents[2]/"scripts/pack_geometry_delivery.py"
spec=importlib.util.spec_from_file_location("geometry_delivery_candidate",MODULE_PATH)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def fixture(root):
    root.mkdir()
    (root/"inputs").mkdir()
    (root/"inputs/a.bin").write_bytes(b"actual synthetic bytes\x00\x01")
    return {"source":"inputs/a.bin","target":"geometry/a.bin","sha256":hashlib.sha256((root/"inputs/a.bin").read_bytes()).hexdigest(),"role":"geometry"}


def refused(call):
    with pytest.raises((ValueError,OSError,RuntimeError,TypeError,KeyError)):
        call()


def test_pack_and_read_only_verify_repeat(tmp_path):
    root,out=tmp_path/"source",tmp_path/"delivery"
    entry=fixture(root)
    manifest=mod.pack(root,[entry],out)
    assert manifest["schema"]=="vkm.geometry-delivery/1"
    first=(out/"MANIFEST.json").read_bytes()
    times={p.relative_to(out).as_posix():p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file()}
    assert mod.pack(root,[entry],out,verify=True)==manifest
    assert (out/"MANIFEST.json").read_bytes()==first
    assert times=={p.relative_to(out).as_posix():p.stat().st_mtime_ns for p in out.rglob("*") if p.is_file()}
    refused(lambda:mod.pack(root,[entry],out))


def test_deterministic_manifest_independent_of_entry_order(tmp_path):
    root=tmp_path/"source"
    first=fixture(root)
    (root/"inputs/b.json").write_text('{"qa":true}',encoding="utf-8")
    second={"source":"inputs/b.json","target":"qa/report.json","sha256":hashlib.sha256((root/"inputs/b.json").read_bytes()).hexdigest(),"role":"qa"}
    a,b=tmp_path/"a",tmp_path/"b"
    mod.pack(root,[first,second],a)
    mod.pack(root,[second,first],b)
    assert (a/"MANIFEST.json").read_bytes()==(b/"MANIFEST.json").read_bytes()
    assert str(tmp_path).encode() not in (a/"MANIFEST.json").read_bytes()


@pytest.mark.parametrize("bad",["", "/a", "C:/a", "C:a", "//host/a", "../a", "a/../b", "a/./b", "a//b", "a/", "a\\b", "a\x00b", "a\nb", "a:stream", "a?b", "a*b", "a<b", 'a"b', "a|b", "a.", "a "])
def test_reject_unsafe_relative_paths(bad):
    refused(lambda:mod.safe_rel(bad))


@pytest.mark.parametrize("bad",["CON","con.txt","PRN.bin","AUX","NUL.tar.gz","COM1","lpt9.log","COM¹.txt","LPT²/x","CLOCK$","NUL .txt","COM1 .log","aux .json"])
def test_reject_windows_device_paths(bad):
    refused(lambda:mod.safe_rel(bad))


@pytest.mark.parametrize("bad",[".env","data/.ENV.local","data/prod.env",".codex/config.toml","data/secrets/a.json","data/SECRET.txt",".git/config"])
def test_reject_secret_paths(bad):
    refused(lambda:mod.safe_rel(bad))


def test_missing_file_and_bad_hash_fail_before_output(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    for i,bad in enumerate((dict(entry,source="missing.bin"),dict(entry,sha256="0"*64),dict(entry,sha256=entry["sha256"].upper()),dict(entry,sha256="short"))):
        out=tmp_path/("bad"+str(i))
        refused(lambda:mod.pack(root,[bad],out))
        assert not out.exists()


@pytest.mark.parametrize("targets",[["a.bin","A.BIN"],["folder","folder/a.bin"],["folder/a.bin","FOLDER"],["Straße.bin","STRASSE.bin"]])
def test_target_casefold_or_prefix_collision_is_preflight_error(tmp_path,targets):
    root=tmp_path/"source"
    entry=fixture(root)
    out=tmp_path/"bad"
    refused(lambda:mod.pack(root,[dict(entry,target=t) for t in targets],out))
    assert not out.exists()


def test_manifest_name_reserved(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    out=tmp_path/"bad"
    refused(lambda:mod.pack(root,[dict(entry,target="manifest.JSON")],out))
    assert not out.exists()


def test_target_directory_case_alias_is_preflight_error(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    out=tmp_path/"bad"
    refused(lambda:mod.pack(root,[dict(entry,target="Geo/a.bin"),dict(entry,target="geo/b.bin")],out))
    assert not out.exists()


def test_unlisted_files_and_large_profile_tree_are_not_copied(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    (root/".env").write_text("synthetic-placeholder",encoding="utf-8")
    (root/"profiles").mkdir()
    (root/"profiles/unlisted.bin").write_bytes(b"not an approved delivery artifact")
    out=tmp_path/"delivery"
    mod.pack(root,[entry],out)
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())==["MANIFEST.json","geometry/a.bin"]
    assert (root/".env").read_text()=="synthetic-placeholder"


def test_verify_detects_tamper_missing_and_unexpected_file(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    for i,mode in enumerate(("tamper","missing","extra")):
        out=tmp_path/("delivery"+str(i))
        mod.pack(root,[entry],out)
        if mode=="tamper":
            (out/"geometry/a.bin").write_bytes(b"wrong")
        elif mode=="missing":
            (out/"geometry/a.bin").rename(out/"geometry/renamed.bin")
        else:
            (out/"unlisted.txt").write_text("extra",encoding="utf-8")
        refused(lambda:mod.pack(root,[entry],out,verify=True))


def test_overlap_with_source_root_rejected(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    out=root/"delivery"
    refused(lambda:mod.pack(root,[entry],out))
    assert not out.exists()


def make_link(link,target):
    try:
        link.symlink_to(target,target_is_directory=target.is_dir())
        return "symlink"
    except OSError:
        if os.name=="nt" and target.is_dir():
            p=subprocess.run(["cmd.exe","/d","/c","mklink","/J",str(link),str(target)],capture_output=True)
            if p.returncode==0:
                return "junction"
        raise


def test_real_symlink_or_junction_escape_rejected(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    outside=tmp_path/"outside"
    outside.mkdir()
    (outside/"a.bin").write_bytes((root/entry["source"]).read_bytes())
    kind=make_link(root/"escape",outside)
    assert kind in {"symlink","junction"}
    out=tmp_path/"bad"
    refused(lambda:mod.pack(root,[dict(entry,source="escape/a.bin")],out))
    assert not out.exists()


def test_output_link_ancestor_rejected(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    outside=tmp_path/"outside"
    outside.mkdir()
    make_link(tmp_path/"linked",outside)
    out=tmp_path/"linked/delivery"
    refused(lambda:mod.pack(root,[entry],out))
    assert not (outside/"delivery").exists()


def test_verify_rejects_replaced_directory_link(tmp_path):
    root=tmp_path/"source"
    entry=fixture(root)
    out=tmp_path/"delivery"
    mod.pack(root,[entry],out)
    moved=tmp_path/"moved_geometry"
    (out/"geometry").rename(moved)
    make_link(out/"geometry",moved)
    refused(lambda:mod.pack(root,[entry],out,verify=True))


def test_verify_fails_closed_on_unreadable_extra_directory(tmp_path,monkeypatch):
    root=tmp_path/"source"
    entry=fixture(root)
    out=tmp_path/"delivery"
    mod.pack(root,[entry],out)
    blocked=out/"unreadable_extra"
    blocked.mkdir()
    (blocked/"unexpected.bin").write_bytes(b"unlisted synthetic bytes")
    original=mod.os.scandir
    def denied(path):
        if Path(path).resolve()==blocked.resolve():
            raise PermissionError("controlled test enumeration failure")
        return original(path)
    monkeypatch.setattr(mod.os,"scandir",denied)
    refused(lambda:mod.verify_delivery(out))


def test_reject_windows_console_device_names():
    for bad in ('CONIN$', 'CONOUT$', 'conin$.txt', 'CONOUT$.log'):
        refused(lambda: mod.safe_rel(bad))
