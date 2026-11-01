"""Fail-closed data-only envelope before Gmsh's content-sniffing dispatcher.

Gmsh OpenFile.cpp (4.15.2), MergeFile, otherwise falls back to readGEO even
for a .msh filename. Only ASCII MSH 2.2/4.1 geometry sections are accepted.
This is a command-boundary check, not scientific mesh qualification.
"""
import math
import re
from pathlib import Path

from vkm_jobs.errors import ToolFailure

NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
NAME = re.compile(r'[0-3]\s+[0-9]+\s+"[^"\\\x00-\x1f]{0,200}"\Z')
ALLOWED = {"Entities", "PhysicalNames", "Nodes", "Elements"}


def validate_msh(path: Path) -> dict:
    def reject():
        raise ToolFailure("INVALID_ARGUMENT", "data-only ASCII MSH 2.2/4.1 required; invalid signature/section, binary, script or trailing content refused")

    sections = []
    try:
        with path.open("r", encoding="ascii") as stream:
            # Must be the actual first line: Gmsh routes by this header.
            if stream.readline().rstrip("\r\n") != "$MeshFormat":
                reject()
            header = stream.readline().split()
            if len(header) != 3 or header[0] not in {"2.2", "4.1"} or header[1:] != ["0", "8"]:
                reject()
            if stream.readline().strip() != "$EndMeshFormat":
                reject()
            for line in stream:
                token = line.strip()
                if not token:
                    continue
                if not token.startswith("$") or token[1:] not in ALLOWED or token[1:] in sections:
                    reject()
                section = token[1:]
                sections.append(section)
                ended = False
                count = 0
                expected_names = None
                for raw in stream:
                    text = raw.strip()
                    if text == "$End" + section:
                        ended = True
                        break
                    if not text:
                        continue
                    if section == "PhysicalNames":
                        if expected_names is None:
                            if not re.fullmatch(r"[0-9]+", text):
                                reject()
                            expected_names = int(text)
                        else:
                            if not NAME.fullmatch(text):
                                reject()
                            count += 1
                    else:
                        words = text.split()
                        if not all(NUMBER.fullmatch(word) and math.isfinite(float(word)) for word in words):
                            reject()
                        count += 1
                if not ended or (section == "PhysicalNames" and expected_names != count) or (section != "PhysicalNames" and not count):
                    reject()
            if "Nodes" not in sections or "Elements" not in sections or sections.index("Nodes") > sections.index("Elements"):
                reject()
    except (UnicodeError, ValueError, OverflowError):
        reject()
    return {"version": header[0], "encoding": "ASCII", "sections": sections, "data_only_envelope": True}
