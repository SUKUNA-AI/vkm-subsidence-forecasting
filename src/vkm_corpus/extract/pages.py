"""Expected number of page units by two independent methods (guard against silent page loss, design §5.2).

| format | method | check method |
|---|---|---|
| PDF | PyMuPDF page tree | pypdfium2 |
| DjVu | ``djvused -e n`` | IFF walk: count of FORM:DJVU components (DJVI dictionaries are not pages) |
| EPUB | spine itemrefs (lxml) | spine itemrefs (stdlib ElementTree) |
| DOCX | pages of the pinned render (PyMuPDF) | the same PDF counted by pypdfium2 |

A disagreement never picks a winner silently: the caller marks the document NEEDS_REVIEW with PAGECOUNT_MISMATCH and
emits page rows for the primary count.
"""
from __future__ import annotations

from pathlib import Path

from vkm_corpus.extract.model import Pagination


def paginate_pdf(path: Path) -> Pagination:
    from vkm_corpus.extract import pdf_native

    doc = pdf_native.open_pdf(path)
    try:
        n = doc.page_count
    finally:
        doc.close()
    try:
        check = pdf_native.count_pages_pdfium(path)
    except Exception:  # noqa: BLE001 - a failed second method is recorded as None
        check = None
    return Pagination(unit="p", page_kind="PDF_PAGE", basis="PDF_PAGE_TREE", count=n, method="PYMUPDF_PAGE_TREE",
                      check_count=check, check_method="PDFIUM_PAGE_COUNT")


def paginate_djvu(path: Path) -> Pagination:
    from vkm_corpus.extract import djvu

    struct = djvu.iff_structure(path)
    try:
        n = djvu.page_count_cli(path)
        method = "DJVUSED_N"
    except djvu.DjvuToolMissing:
        n = len(struct.pages)
        method = "IFF_FORM_DJVU_COUNT"
        return Pagination(unit="p", page_kind="DJVU_PAGE", basis="DJVU_PAGE_ORDER", count=n, method=method,
                          check_count=None, check_method=None,
                          extra={"decoder_missing": True, "shared_component_count": struct.shared_components,
                                 "dirm_files": struct.dirm_files})
    return Pagination(unit="p", page_kind="DJVU_PAGE", basis="DJVU_PAGE_ORDER", count=n, method=method,
                      check_count=len(struct.pages), check_method="IFF_FORM_DJVU_COUNT",
                      extra={"shared_component_count": struct.shared_components, "dirm_files": struct.dirm_files,
                             "bundled": struct.bundled})


def paginate_epub(path: Path) -> Pagination:
    import zipfile

    from vkm_corpus.extract import epub

    with zipfile.ZipFile(path) as z:
        _, _, _, spine = epub.read_structure(z)
    return Pagination(unit="s", page_kind="EPUB_SPINE_ITEM", basis="EPUB_SPINE", count=len(spine),
                      method="OPF_SPINE_LXML", check_count=epub.count_spine_stdlib(path),
                      check_method="OPF_SPINE_ETREE",
                      extra={"nonlinear": sum(1 for _, lin in spine if not lin)})


def paginate_rendered_pdf(pdf_path: Path, profile: str) -> Pagination:
    p = paginate_pdf(pdf_path)
    return Pagination(unit="r", page_kind="DOCX_RENDERED_PAGE", basis="DOCX_PINNED_RENDER", count=p.count,
                      method="PYMUPDF_PAGE_TREE_OF_RENDER", check_count=p.check_count,
                      check_method="PDFIUM_PAGE_COUNT_OF_RENDER", extra={"render_profile": profile})
