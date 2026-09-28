"""Pinned DOCX → PDF render with LibreOffice 24.2.x in a container (``infra/workstation/libreoffice``).

The container runs without network (``--network none``) on a copy of the DOCX; the profile (LibreOffice version,
image id, hash of the font list) goes into ``pagination_render_profile``. PDF bytes of LibreOffice carry a creation
date and a document id, so the render is done once and cached by its stage signature (DOCX sha256 + profile) – the
stored PDF (``DOCX_RENDERED_PDF``, KEEP_RAW) is the pagination basis of ``r`` pages.

Needs the ``docker`` CLI on the host (Windows with Docker Desktop); the rest of the pipeline only reads the cached
artifact, so the render may run on a different host than the extraction.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

RENDERER_ID = "libreoffice-docx-pdf"
DEFAULT_IMAGE = "vkm-libreoffice:24.2"


@dataclass
class DocxRender:
    pdf_bytes: bytes
    profile: dict[str, str]

    @property
    def profile_string(self) -> str:
        p = self.profile
        return f"libreoffice-{p.get('libreoffice_version')};image={p.get('image_id', '')[:19]};" \
               f"fonts={p.get('fonts_sha256', '')[:8]}"


def _docker() -> str:
    for name in ("docker", "docker.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("DECODER_MISSING: docker CLI not found (DOCX render needs the pinned LibreOffice container)")


def image_profile(image: str = DEFAULT_IMAGE, timeout: float = 120.0) -> dict[str, str]:
    docker = _docker()
    image_id = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", image], capture_output=True,
                              text=True, timeout=timeout, check=True).stdout.strip()
    probe = subprocess.run([docker, "run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c",
                            "cat /opt/vkm/soffice_version.txt /opt/vkm/fonts.sha256; wc -l < /opt/vkm/fonts.txt"],
                           capture_output=True, text=True, timeout=timeout, check=True).stdout.splitlines()
    version_line = probe[0].strip() if probe else ""
    version = version_line.split()[1] if len(version_line.split()) > 1 else "unknown"
    return {"renderer": RENDERER_ID, "image": image, "image_id": image_id, "libreoffice_version": version,
            "libreoffice_version_line": version_line, "fonts_sha256": probe[1].strip() if len(probe) > 1 else "",
            "fonts_count": probe[2].strip() if len(probe) > 2 else ""}


def render_docx(docx_path: Path, image: str = DEFAULT_IMAGE, timeout: float = 900.0) -> DocxRender:
    docker = _docker()
    profile = image_profile(image)
    with tempfile.TemporaryDirectory(prefix="vkm-docx-") as td:
        work = Path(td)
        (work / "in").mkdir()
        (work / "out").mkdir()
        src = work / "in" / "document.docx"
        shutil.copyfile(docx_path, src)
        cmd = [docker, "run", "--rm", "--network", "none", "-v", f"{work}:/work", image,
               "--convert-to", "pdf", "--outdir", "/work/out", "/work/in/document.docx"]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = work / "out" / "document.pdf"
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f"RENDER_FAILED: soffice exit {proc.returncode}: "
                               f"{proc.stderr.decode('utf-8', 'replace')[:400]}")
        return DocxRender(pdf_bytes=out.read_bytes(), profile=profile)


def main(argv: list[str] | None = None) -> int:
    """``python -m vkm_corpus.extract.docx_render <docx> <out.pdf>``: render and print the profile as JSON."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    res = render_docx(Path(args[0]), image=args[2] if len(args) > 2 else DEFAULT_IMAGE)
    Path(args[1]).write_bytes(res.pdf_bytes)
    print(json.dumps({**res.profile, "profile_string": res.profile_string, "pdf_bytes": len(res.pdf_bytes)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
