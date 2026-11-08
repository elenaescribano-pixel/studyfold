"""Re-flow a portrait PDF into a dense two-column landscape PDF.

Each source page is cut into horizontal strips that are placed column after
column. Strips are vector copies (``show_pdf_page``), so text stays selectable
and images keep their resolution. Cuts are only made in blank gaps between
lines, images and drawings, so nothing is sliced in half.
"""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf as fitz

A4_LANDSCAPE = (842, 595)


@dataclass
class CompactResult:
    pages_in: int
    pages_out: int
    scale: float

    @property
    def body_pt(self) -> float:
        """Approximate body text size, assuming a 12 pt source."""
        return 12 * self.scale


def _analyze(page: fitz.Page, footer: float):
    """Content bounds of a page plus the vertical ranges that must not be cut."""
    protected, xs0, xs1, top, bot = [], [], [], 1e9, 0.0
    limit = page.rect.height - footer
    for b in page.get_text("dict")["blocks"]:
        if b["bbox"][1] > limit:
            continue
        for line in b.get("lines", []):
            r = fitz.Rect(line["bbox"])
            protected.append((r.y0 - 1, r.y1 + 1))
        r = fitz.Rect(b["bbox"])
        if b["type"] == 1:
            protected.append((r.y0 - 1, r.y1 + 1))
        xs0.append(r.x0); xs1.append(r.x1)
        top, bot = min(top, r.y0), max(bot, r.y1)
    for im in page.get_image_info():
        r = fitz.Rect(im["bbox"]) & page.rect
        if r.is_empty:
            continue
        protected.append((r.y0 - 1, r.y1 + 1))
        xs0.append(r.x0); xs1.append(r.x1)
        top, bot = min(top, r.y0), max(bot, r.y1)
    for d in page.get_drawings():
        r = d["rect"] & page.rect  # wide tables can draw far outside the page
        if r.is_empty or r.y0 > limit:
            continue
        if r.width > 100:  # tiny inline-code backgrounds report unreliable x spans
            xs0.append(r.x0); xs1.append(r.x1)
        top, bot = min(top, r.y0), max(bot, r.y1)
    if bot == 0:
        return None
    return dict(top=top - 2, bot=bot + 2, prot=protected, x0=min(xs0), x1=max(xs1))


def _safe_cut(protected, lo: float, hi: float) -> float:
    """Highest y <= hi that does not cross any protected range."""
    y = hi
    while y > lo + 20:
        hit = [a for a, b in protected if a < y < b]
        if not hit:
            return y
        y = min(hit) - 0.01
    return hi  # nothing safe: hard cut


def compact(src_path: str, out_path: str, cols: int = 2, margin: float = 14, gap: float = 16,
            footer: float = 57, join: float = 4, page_size=A4_LANDSCAPE,
            page_numbers: bool = True) -> CompactResult:
    """Write a compacted copy of ``src_path`` to ``out_path``.

    footer: height (pt) of the band at the bottom of each source page that is ignored
            (page numbers, running footers).
    """
    src = fitz.open(src_path)
    pw, ph = page_size
    infos = [_analyze(p, footer) for p in src]
    used_infos = [i for i in infos if i]
    if not used_infos:
        raise ValueError("the PDF has no visible content")
    x0 = min(i["x0"] for i in used_infos) - 2
    x1 = max(i["x1"] for i in used_infos) + 2
    colw = (pw - 2 * margin - (cols - 1) * gap) / cols
    scale = colw / (x1 - x0)
    colh = ph - 2 * margin

    out = fitz.open()
    state = dict(page=None, col=cols, used=0.0)

    def next_col():
        state["col"] += 1
        state["used"] = 0.0
        if state["col"] >= cols:
            state["page"] = out.new_page(width=pw, height=ph)
            state["col"] = 0

    next_col()
    for pno, info in enumerate(infos):
        if not info:
            continue
        y = info["top"]
        if state["used"] > 0:
            state["used"] += join
        while y < info["bot"] - 0.5:
            room = (colh - state["used"]) / scale
            if room < 30:
                next_col()
                continue
            if info["bot"] - y <= room:
                y1 = info["bot"]
            else:
                y1 = _safe_cut(info["prot"], y, y + room)
                forced = y1 == y + room and any(a < y1 < b for a, b in info["prot"])
                if (y1 <= y + 20 or forced) and state["used"] > 0:
                    next_col()
                    continue
            h = (y1 - y) * scale
            cx = margin + state["col"] * (colw + gap)
            state["page"].show_pdf_page(
                fitz.Rect(cx, margin + state["used"], cx + colw, margin + state["used"] + h),
                src, pno, clip=fitz.Rect(x0, y, x1, y1))
            state["used"] += h
            y = y1
            if y < info["bot"] - 0.5:
                next_col()

    if page_numbers:
        for i, p in enumerate(out):
            p.insert_text((pw - margin - 14, ph - 4), str(i + 1), fontsize=6, color=(0.5, 0.5, 0.5))
    out.save(out_path, garbage=4, deflate=True)
    return CompactResult(len(src), len(out), scale)
