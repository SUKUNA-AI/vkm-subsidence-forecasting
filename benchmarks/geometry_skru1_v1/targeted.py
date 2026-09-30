"""Targeted native extraction of registered 197 figures and 252 tables, PRIVATE outputs only."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+"\n",encoding="utf-8",newline="\n")


def serial(value):
    if isinstance(value,dict): return {k:serial(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [serial(v) for v in value]
    if type(value).__module__ == "pymupdf": return list(value)
    return value


def extract(repo: Path,out: Path,resources: Path) -> list[dict]:
    import pymupdf as fitz
    ns={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    reg=list(csv.DictReader((resources/"00_registry/SOURCE_REGISTER.csv").open(encoding="utf-8-sig")))
    records={r["resource_id"]:r for r in reg}
    inventory=[]; receipt={"schema":"vkm.targeted_native/1","inputs":{},"outputs":{},"source_ids":["VKM-SRC-197","VKM-SRC-252"],"models_executed":0}
    r=records["VKM-SRC-197"]; source=resources/r["canonical_path"]
    if sha(source) != r["sha256"]: raise ValueError("source197 SHA mismatch")
    receipt["inputs"]["VKM-SRC-197"]={"sha256":r["sha256"],"registry":"PRIVATE 00_registry/SOURCE_REGISTER.csv"}
    doc=fitz.open(source)
    for page_index,page in enumerate(doc):
        candidates=[]
        groups=[]
        # This article stores each figure as many horizontal bitmap stripes.
        # An individual XObject is not necessarily the complete figure.
        for image in page.get_image_info(xrefs=True):
            box=fitz.Rect(image["bbox"])
            if box.width < 30 or box.height < .5: continue
            merged=False
            for group in groups:
                other=group["box"]
                common_width=min(box.x1,other.x1)-max(box.x0,other.x0)
                vertical_gap=max(box.y0-other.y1,other.y0-box.y1,0)
                if common_width >= .95*min(box.width,other.width) and vertical_gap <= .5:
                    group["box"] |= box; group["images"].append(image); merged=True; break
            if not merged: groups.append({"box":box,"images":[image]})
        for group in groups:
            if group["box"].width*group["box"].height >= 1000:
                candidates.append((group["box"],group["images"],"ADJACENT_XOBJECT_STRIPES"))
        # Vector-only figures with captions: preserve full-page paths as an explicit source layer,
        # not an invented semantic figure polygon.
        if not candidates and re.search(r"\bРис\.?\s*\d",page.get_text()) and page.get_drawings():
            candidates.append((page.rect,[],"PAGE_VECTOR_CONTEXT"))
        for index,(box,images,method) in enumerate(candidates):
            key=f"C197_p{page_index+1:04d}_{index}"
            render=out/"targeted/renders"/(key+".png"); render.parent.mkdir(parents=True,exist_ok=True)
            pix=page.get_pixmap(matrix=fitz.Matrix(4,4),clip=box,alpha=False)
            pix.save(render)
            native_box=box*page.derotation_matrix
            drawings=[d for d in page.get_drawings(extended=True)
                      if d["type"] in ("clip","group") or not (fitz.Rect(d["rect"]) & native_box).is_empty]
            payload={"key":key,"source_id":"VKM-SRC-197","source_sha256":r["sha256"],
                     "page_id":f"VKM-SRC-197:p{page_index+1:04d}","bbox_page_pt":list(box),"bbox_native_pt":list(native_box),
                     "coordinate_system":"PDF_UNROTATED_PT","rotation":page.rotation,"page_rect":list(page.rect),
                     "page_rotation_matrix":list(page.rotation_matrix),"page_derotation_matrix":list(page.derotation_matrix),
                     "width":pix.width,"height":pix.height,"drawings":serial(drawings),
                     "words":serial(page.get_text("words",clip=native_box)),"image_xobjects":[],"review_status":"AUTO_EXTRACTED_UNREVIEWED"}
            for subindex,image in enumerate(images):
                xref=image.get("xref")
                if not xref: continue
                raw=doc.extract_image(xref); embedded=out/"targeted/embedded"/(key+f"_stripe{subindex:03d}."+raw["ext"])
                embedded.parent.mkdir(parents=True,exist_ok=True); embedded.write_bytes(raw["image"])
                payload["image_xobjects"].append({"xref":xref,"file":embedded.relative_to(repo).as_posix(),"sha256":sha(embedded),
                                                   "bbox_page_pt":image["bbox"],"transform":serial(image.get("transform")),
                                                   "mask_xref":dict((i[0],i[1]) for i in page.get_images(full=True)).get(xref,0)})
            native=out/"targeted/native"/(key+".json"); write(native,payload)
            inventory.append({"key":key,"source_id":"VKM-SRC-197","source_sha256":r["sha256"],"page_id":payload["page_id"],
                              "source_image":render.relative_to(repo).as_posix(),"image_sha256":sha(render),
                              "native_object":native.relative_to(repo).as_posix(),"width":pix.width,"height":pix.height,
                              "rotation":page.rotation,"locator":{"page_id":payload["page_id"],"bbox_pt_tl":list(box),"xrefs":[i.get('xref') for i in images]},
                              "type":"UNKNOWN","mine_attribution":"SKRU1_FROM_SOURCE_NOT_PER_FIGURE_REVIEWED",
                              "scope":"UNREVIEWED_PER_FIGURE","method":method,
                              "image_to_page":[.25,0,0,.25,pix.x/4,pix.y/4],"represented_date":"UNKNOWN"})
    r=records["VKM-SRC-252"]; source=resources/r["canonical_path"]
    if sha(source) != r["sha256"]: raise ValueError("source252 SHA mismatch")
    receipt["inputs"]["VKM-SRC-252"]={"sha256":r["sha256"],"registry":"PRIVATE 00_registry/SOURCE_REGISTER.csv","source_role":"PLANNED"}
    with zipfile.ZipFile(source) as archive:
        xml=archive.read("word/document.xml"); root=ET.fromstring(xml)
        rels=archive.read("word/_rels/document.xml.rels") if "word/_rels/document.xml.rels" in archive.namelist() else b""
        tables=[]; previous=[]; index=0
        for child in root.find("w:body",ns):
            tag=child.tag.rsplit("}",1)[-1]
            if tag == "p":
                text="".join(n.text or "" for n in child.findall(".//w:t",ns)).strip()
                if text: previous.append(text)
            elif tag == "tbl":
                index+=1
                context="\n".join(previous[-8:])
                found=list(re.finditer(r"Таблица\s+(1\.[12])(?:\b|\s)",context,re.I))
                # Only the closest table caption controls selection; keep ordinal and native XML.
                last_any=list(re.finditer(r"Таблица\s+(\d+[.,]\d+)",context,re.I))
                chosen=found[-1].group(1) if found and (not last_any or last_any[-1].group(1).replace(",",".") in ("1.1","1.2")) else None
                if chosen:
                    rows=[]
                    for row_index,tr in enumerate(child.findall("w:tr",ns),1):
                        cells=[]; col=0
                        for cell_index,tc in enumerate(tr.findall("w:tc",ns),1):
                            gs=tc.find("w:tcPr/w:gridSpan",ns); vm=tc.find("w:tcPr/w:vMerge",ns)
                            span=int(gs.get("{"+ns["w"]+"}val","1")) if gs is not None else 1
                            text="\n".join("".join(t.text or "" for t in p.findall(".//w:t",ns)) for p in tc.findall("w:p",ns))
                            cells.append({"cell_index":cell_index,"grid_column_start":col,"grid_span":span,
                                          "vertical_merge":("restart" if vm is not None and vm.get("{"+ns["w"]+"}val") == "restart" else "continue") if vm is not None else None,
                                          "text":text,"locator":{"part":"word/document.xml","table_ordinal":index,"row":row_index,"cell":cell_index},
                                          "verification":"AUTO_EXTRACTED_UNREVIEWED","source_role":"PLANNED",
                                          "cell_xml_sha256":hashlib.sha256(ET.tostring(tc,encoding="utf-8")).hexdigest()})
                            col+=span
                        rows.append({"row":row_index,"cells":cells})
                    tables.append({"table_number":chosen,"table_ordinal":index,"caption_context":context,"rows":rows,
                                   "table_xml_sha256":hashlib.sha256(ET.tostring(child,encoding="utf-8")).hexdigest(),
                                   "source_role":"PLANNED","geometry":"UNKNOWN_NO_NATIVE_PLAN_GEOMETRY_IN_TABLE"})
                previous=[]
        output={"schema":"vkm.docx_native_tables/1","source_id":"VKM-SRC-252","source_sha256":r["sha256"],
                "part":"word/document.xml","part_sha256":hashlib.sha256(xml).hexdigest(),
                "relationships_sha256":hashlib.sha256(rels).hexdigest(),"table_count_in_document":index,
                "selected_tables":tables,"source_role":"PLANNED","verification":"AUTO_EXTRACTED_UNREVIEWED",
                "rule":"Native table cells and merge structure; no planned->executed promotion; no quotes in PUBLIC"}
        write(out/"targeted/VKM-SRC-252_tables_1_1_1_2.json",output)
    write(out/"additional_native_inventory.json",inventory)
    current={out/"targeted/VKM-SRC-252_tables_1_1_1_2.json"}
    for item in inventory:
        current.update((repo/item["source_image"],repo/item["native_object"]))
        native=json.loads((repo/item["native_object"]).read_text(encoding="utf-8"))
        current.update(repo/x["file"] for x in native["image_xobjects"])
    for path in sorted(current): receipt["outputs"][path.relative_to(repo).as_posix()]=sha(path)
    receipt["excluded_provisional_files"]=[p.relative_to(repo).as_posix() for p in sorted((out/"targeted").rglob("*")) if p.is_file() and p not in current]
    receipt["provisional_rule"]="Only current inventory objects and current receipt outputs are accepted inputs; leftover exploratory renders are excluded"
    receipt["outputs"][(out/"additional_native_inventory.json").relative_to(repo).as_posix()]=sha(out/"additional_native_inventory.json")
    receipt["selected_table_count"]=len(output["selected_tables"])
    receipt["figure_count_197"]=len(inventory)
    write(out/"targeted_receipt.json",receipt)
    return inventory
