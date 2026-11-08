"""Page-level helpers: pull out one section of a long PDF, or wipe repeated bands."""
from __future__ import annotations

import pymupdf as fitz

_REDACT = dict(images=fitz.PDF_REDACT_IMAGE_REMOVE,
               graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)


def find_heading(doc: fitz.Document, text: str, start: int = 0):
    """(page_index, y) of the first text block starting with ``text``, or None."""
    for pno in range(start, len(doc)):
        for b in doc[pno].get_text("blocks"):
            if b[4].strip().startswith(text):
                return pno, b[1]
    return None


def extract_section(src_path: str, out_path: str, first: int, last: int,
                    top: float | None = None, bottom: float | None = None) -> int:
    """Copy pages ``first``..``last`` (1-based, inclusive).

    top:    on the first page, drop everything above this y (pt).
    bottom: on the last page, drop everything below this y (pt).
    Returns the number of pages written.
    """
    src = fitz.open(src_path)
    if not 1 <= first <= last <= len(src):
        raise ValueError(f"page range {first}-{last} is outside 1-{len(src)}")
    doc = fitz.open()
    doc.insert_pdf(src, from_page=first - 1, to_page=last - 1)
    if top:
        p = doc[0]
        p.add_redact_annot(fitz.Rect(0, 0, p.rect.width, top))
        p.apply_redactions(**_REDACT)
    if bottom:
        p = doc[-1]
        p.add_redact_annot(fitz.Rect(0, bottom, p.rect.width, p.rect.height))
        p.apply_redactions(**_REDACT)
    doc.save(out_path, garbage=3, deflate=True)
    return len(doc)


def strip_bands(src_path: str, out_path: str, above: float | None = None,
                below: float | None = None) -> int:
    """Remove a band at the top (y < above) and/or bottom (y > below) of every page.

    Handy for watermarks, download stamps or QR-code footers added by note-sharing sites.
    """
    doc = fitz.open(src_path)
    for p in doc:
        if above:
            p.add_redact_annot(fitz.Rect(0, 0, p.rect.width, above))
        if below:
            p.add_redact_annot(fitz.Rect(0, below, p.rect.width, p.rect.height))
        p.apply_redactions(**_REDACT)
    doc.save(out_path, garbage=3, deflate=True)
    return len(doc)
