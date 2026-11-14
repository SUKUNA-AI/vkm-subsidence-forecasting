"""Explicit allowlist packer; no selection walk, overwrite, cleanup or deletion.

Geometry/admission statuses are data carried by selected files. Packing verifies
file integrity and portable names, never scientific acceptance or publication.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

SCHEMA="vkm.geometry-delivery/1"
MANIFEST="MANIFEST.json"
DEVICES={"CON","PRN","AUX","NUL","CLOCK$","CONIN$","CONOUT$"}|{p+n for p in ("COM","LPT") for n in "123456789¹²³"}


def safe_rel(value):
    if not isinstance(value,str) or not value or "\\" in value or value.startswith("/"):
        raise ValueError("use a nonempty POSIX relative path")
    parts=value.split("/")
    for part in parts:
        if not part or part in {".",".."} or part.endswith(("."," ")) or any(ord(c)<32 or c in '<>:"|?*' for c in part):
            raise ValueError("unsafe portable path component")
        folded=part.casefold()
        if part.split(".")[0].rstrip(" ").upper() in DEVICES:
            raise ValueError("reserved Windows device path")
        if folded in {".codex",".git",".env"} or folded.startswith(".env.") or folded.endswith(".env") or "secret" in folded:
            raise ValueError("secret/config path denied")
    return value


def validate_targets(targets):
    """Reject file collisions, prefix collisions and directory case aliases."""
    folded={}
    spelling={}
    for target in targets:
        key=target.casefold()
        if key in folded:
            raise ValueError("casefold target collision")
        folded[key]=target
        parts=target.split("/")
        for i in range(1,len(parts)+1):
            prefix="/".join(parts[:i])
            old=spelling.setdefault(prefix.casefold(),prefix)
            if old!=prefix:
                raise ValueError("directory component case alias")
    keys=set(folded)
    for key in keys:
        parts=key.split("/")
        if any("/".join(parts[:i]) in keys for i in range(1,len(parts))):
            raise ValueError("file/directory target collision")


def _deny_links(path):
    for item in reversed((path,*path.parents)):
        try:
            info=item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",0):
            raise ValueError("symlink/junction/reparse path denied")


def _hash(path):
    h,size=hashlib.sha256(),0
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            h.update(chunk)
            size+=len(chunk)
    return h.hexdigest(),size


def _entries(entries):
    if not isinstance(entries,list) or not entries:
        raise ValueError("nonempty explicit entries required")
    rows=[]
    for e in entries:
        if not isinstance(e,dict) or set(e)!={"source","target","sha256","role"}:
            raise ValueError("entry shape must be source/target/sha256/role")
        row=dict(e,source=safe_rel(e["source"]),target=safe_rel(e["target"]))
        if row["target"].casefold()==MANIFEST.casefold():
            raise ValueError("manifest target reserved")
        if not isinstance(row["sha256"],str) or not re.fullmatch("[0-9a-f]{64}",row["sha256"]):
            raise ValueError("lowercase SHA256 required")
        if not isinstance(row["role"],str) or not row["role"].strip() or len(row["role"])>128:
            raise ValueError("nonempty bounded role required")
        rows.append(row)
    validate_targets([r["target"] for r in rows])
    return sorted(rows,key=lambda r:r["target"])


def _encoded(manifest):
    return (json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n").encode("utf-8")


def _raise_walk_error(error):
    raise error


def verify_delivery(output,expected=None):
    output=Path(output).absolute()
    _deny_links(output)
    if not output.is_dir():
        raise ValueError("delivery directory missing")
    _deny_links(output/MANIFEST)
    raw=(output/MANIFEST).read_bytes()
    manifest=json.loads(raw)
    if not isinstance(manifest,dict) or set(manifest)!={"schema","entries"} or manifest["schema"]!=SCHEMA:
        raise ValueError("unsupported delivery manifest")
    rows=manifest["entries"]
    if not isinstance(rows,list) or not rows or any(not isinstance(r,dict) or set(r)!={"source","target","sha256","role","bytes"} or type(r["bytes"]) is not int or r["bytes"]<0 for r in rows):
        raise ValueError("invalid delivery entries")
    _entries([{k:v for k,v in r.items() if k!="bytes"} for r in rows])
    if raw!=_encoded(manifest) or rows!=sorted(rows,key=lambda r:r["target"]) or expected is not None and raw!=_encoded(expected):
        raise ValueError("manifest differs from deterministic expected bytes")
    found=set()
    for base,dirs,files in os.walk(output,followlinks=False,onerror=_raise_walk_error):
        for name in dirs+files:
            _deny_links(Path(base)/name)
        found.update((Path(base)/name).relative_to(output).as_posix() for name in files)
    if found!={MANIFEST}|{r["target"] for r in rows}:
        raise ValueError("missing/unexpected delivery files")
    for row in rows:
        target=output/row["target"]
        if not target.is_file() or _hash(target)!=(row["sha256"],row["bytes"]):
            raise ValueError("delivery hash/size mismatch")
    return manifest


def pack(source_root,entries,output,verify=False):
    root=Path(source_root).absolute()
    _deny_links(root)
    root=root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source root must be a directory")
    out=Path(output).absolute()
    _deny_links(out)
    out=out.resolve()
    if out==root or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError("source/output roots overlap")
    rows=_entries(entries)
    sources=[]
    for row in rows:
        source=root/row["source"]
        _deny_links(source)
        resolved=source.resolve(strict=True)
        if not resolved.is_relative_to(root) or not stat.S_ISREG(source.stat().st_mode):
            raise ValueError("source escapes root or is not a regular file")
        digest,size=_hash(source)
        if digest!=row["sha256"]:
            raise ValueError("source hash mismatch")
        row["bytes"]=size
        sources.append(source)
    manifest={"schema":SCHEMA,"entries":rows}
    if verify:
        return verify_delivery(out,manifest)
    out.mkdir(parents=True,exist_ok=False)
    for row,source in zip(rows,sources):
        _deny_links(source)
        target=out/row["target"]
        target.parent.mkdir(parents=True,exist_ok=True)
        _deny_links(target.parent)
        h,size=hashlib.sha256(),0
        with source.open("rb") as inp,target.open("xb") as dest:
            for chunk in iter(lambda:inp.read(1024*1024),b""):
                dest.write(chunk)
                h.update(chunk)
                size+=len(chunk)
        if (h.hexdigest(),size)!=(row["sha256"],row["bytes"]):
            raise ValueError("source changed while copying; partial output preserved")
    with (out/MANIFEST).open("xb") as stream:
        stream.write(_encoded(manifest))
    return verify_delivery(out,manifest)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-root")
    ap.add_argument("--plan")
    ap.add_argument("--output",required=True)
    ap.add_argument("--verify",action="store_true")
    args=ap.parse_args()
    if args.verify and args.source_root is None and args.plan is None:
        result=verify_delivery(args.output)
    else:
        if not args.source_root or not args.plan:
            ap.error("source-root and plan required for copying or allowlist-bound verification")
        plan=json.loads(Path(args.plan).read_bytes())
        if not isinstance(plan,dict) or set(plan)!={"entries"}:
            raise ValueError("plan must contain only explicit entries")
        result=pack(args.source_root,plan["entries"],args.output,args.verify)
    print(json.dumps({"schema":result["schema"],"files":len(result["entries"]),"verification":"PASS"}))


if __name__=="__main__":
    main()
