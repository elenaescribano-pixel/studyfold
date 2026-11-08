"""Command-line interface: ``studyfold <command> …`` (run ``studyfold -h``)."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .compact import compact
from .layout import check, extract, load, render, reuse, save
from .pages import extract_section, find_heading, strip_bands
from .translators import ClaudeCLI, TranslatorUnavailable, export_batches, import_batches, pending


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def cmd_compact(a):
    out = a.output or os.path.join(os.path.dirname(a.pdf) or ".", f"{_stem(a.pdf)}_compact.pdf")
    r = compact(a.pdf, out, cols=a.cols, margin=a.margin, footer=a.footer)
    print(f"{r.pages_in} -> {r.pages_out} pages, body text ≈ {r.body_pt:.1f} pt  ->  {out}")


def cmd_section(a):
    src = a.pdf
    import pymupdf as fitz
    doc = fitz.open(src)
    first, last, top, bottom = a.first, a.last, a.top, a.bottom
    if a.start:
        hit = find_heading(doc, a.start)
        if not hit:
            sys.exit(f"heading not found: {a.start!r}")
        first, top = hit[0] + 1, max(hit[1] - 4, 0) or None
    if a.end:
        hit = find_heading(doc, a.end, start=(first or 1) - 1)
        if not hit:
            sys.exit(f"heading not found: {a.end!r}")
        last, bottom = hit[0] + 1, hit[1] - 4
    if not first or not last:
        sys.exit("give --pages FIRST-LAST or --start/--end headings")
    out = a.output or f"{_stem(src)}_p{first}-{last}.pdf"
    n = extract_section(src, out, first, last, top, bottom)
    print(f"pages {first}-{last} ({n} pages)  ->  {out}")


def cmd_strip(a):
    out = a.output or f"{_stem(a.pdf)}_clean.pdf"
    strip_bands(a.pdf, out, a.above, a.below)
    print(f"cleaned  ->  {out}")


def cmd_extract(a):
    work = a.work or f"{_stem(a.pdf)}.work.json"
    data = extract(a.pdf, work)
    for old in a.reuse or []:
        n = reuse(data, load(old))
        print(f"reused {n} translations from {old}")
    save(data, work)
    print(f"{len(data['groups'])} paragraphs  ->  {work}")


def cmd_translate(a):
    try:
        missing = ClaudeCLI(a.lang, a.subject, a.model, a.workers).run(a.work)
    except TranslatorUnavailable as e:
        sys.exit(f"translator unavailable: {e}\nProgress is saved; rerun later or use "
                 f"`studyfold export` for manual translation.")
    print(f"done, {missing} paragraphs still missing" if missing else "all paragraphs translated")
    sys.exit(1 if missing else 0)


def cmd_export(a):
    n = export_batches(a.work, a.folder, a.lang, a.subject)
    print(f"{n} batch files written to {a.folder}/ (see PROMPT.txt)")


def cmd_import(a):
    ok, bad = import_batches(a.work, a.folder)
    left = len(pending(load(a.work)))
    print(f"accepted {ok}, rejected {bad} (tags did not match), {left} still pending")


def cmd_render(a):
    out = a.output or f"{_stem(a.pdf)}_{a.lang}.pdf"
    try:
        unplaced = render(a.pdf, a.work, out)
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"rendered  ->  {out}" + (f"  ({unplaced} paragraphs did not fit)" if unplaced else ""))


def cmd_check(a):
    t, s, bad = check(a.pdf, a.lang)
    print(f"{a.lang} markers {t}, English markers {s}, paragraphs still in English: {len(bad)}")
    for pg, txt in bad[:20]:
        print(f"  p{pg}: {txt}")
    sys.exit(1 if bad else 0)


def cmd_run(a):
    """The whole pipeline for one PDF."""
    os.makedirs(a.outdir, exist_ok=True)
    stem = _stem(a.pdf)
    r = compact(a.pdf, os.path.join(a.outdir, f"{stem}_compact.pdf"))
    print(f"[1/5] compact: {r.pages_in} -> {r.pages_out} pages")
    work = os.path.join(a.outdir, f"{stem}.work.json")
    if os.path.exists(work):
        print("[2/5] extract: resuming existing work file")
    else:
        data = extract(a.pdf, work)
        print(f"[2/5] extract: {len(data['groups'])} paragraphs")
    try:
        missing = ClaudeCLI(a.lang, a.subject, a.model, a.workers).run(work, progress=lambda m: None)
    except TranslatorUnavailable as e:
        sys.exit(f"[3/5] translator unavailable: {e}\nRerun the same command later to resume.")
    if missing:
        sys.exit(f"[3/5] {missing} paragraphs could not be translated; rerun to retry them")
    print("[3/5] translate: done")
    full = os.path.join(a.outdir, f"{stem}_{a.lang}.pdf")
    unplaced = render(a.pdf, work, full)
    t, s, bad = check(full, a.lang)
    print(f"[4/5] render + check: {len(bad)} paragraphs still read as English"
          + (f", {unplaced} did not fit" if unplaced else ""))
    r2 = compact(full, os.path.join(a.outdir, f"{stem}_{a.lang}_compact.pdf"))
    print(f"[5/5] compact translation: {r2.pages_out} pages  ->  {a.outdir}/")


def main(argv=None):
    p = argparse.ArgumentParser(prog="studyfold", description=(
        "Compact study-note PDFs into dense two-column pages and translate them in place, "
        "keeping images, colours, code and layout."))
    p.add_argument("--version", action="version", version=f"studyfold {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    def lang_opts(sp):
        sp.add_argument("--lang", default="es", help="target language code (default: es)")
        sp.add_argument("--subject", default="", help="course subject, gives the translator context")

    def tr_opts(sp):
        lang_opts(sp)
        sp.add_argument("--model", default="sonnet", help="Claude model alias for `claude -p`")
        sp.add_argument("--workers", type=int, default=5, help="parallel requests (default: 5)")

    sp = add("run", cmd_run, "compact + translate + compact the translation, in one go")
    sp.add_argument("pdf"); sp.add_argument("-o", "--outdir", default="studyfold_out")
    tr_opts(sp)

    sp = add("compact", cmd_compact, "re-flow a PDF into two columns per landscape page")
    sp.add_argument("pdf"); sp.add_argument("-o", "--output")
    sp.add_argument("--cols", type=int, default=2)
    sp.add_argument("--margin", type=float, default=14, help="outer margin in pt")
    sp.add_argument("--footer", type=float, default=57, help="ignore this many pt at the bottom of each page")

    sp = add("section", cmd_section, "cut one section out of a long PDF")
    sp.add_argument("pdf"); sp.add_argument("-o", "--output")
    sp.add_argument("--pages", help="FIRST-LAST, 1-based")
    sp.add_argument("--top", type=float, help="drop content above this y on the first page")
    sp.add_argument("--bottom", type=float, help="drop content below this y on the last page")
    sp.add_argument("--start", help="begin at the first heading starting with this text")
    sp.add_argument("--end", help="stop right before the first later heading starting with this text")

    sp = add("strip", cmd_strip, "remove a band at the top/bottom of every page (stamps, watermarks)")
    sp.add_argument("pdf"); sp.add_argument("-o", "--output")
    sp.add_argument("--above", type=float, help="remove everything above this y")
    sp.add_argument("--below", type=float, help="remove everything below this y")

    sp = add("extract", cmd_extract, "split a PDF into paragraphs (writes a .work.json file)")
    sp.add_argument("pdf"); sp.add_argument("-w", "--work")
    sp.add_argument("--reuse", nargs="*", help="older work files whose translations can be reused")

    sp = add("translate", cmd_translate, "translate pending paragraphs with the Claude Code CLI")
    sp.add_argument("work"); tr_opts(sp)

    sp = add("export", cmd_export, "write pending paragraphs to JSON batches for manual translation")
    sp.add_argument("work"); sp.add_argument("folder"); lang_opts(sp)

    sp = add("import", cmd_import, "read translated batches (*.tr.json) back into the work file")
    sp.add_argument("work"); sp.add_argument("folder")

    sp = add("render", cmd_render, "write the translated PDF")
    sp.add_argument("pdf"); sp.add_argument("work"); sp.add_argument("-o", "--output")
    sp.add_argument("--lang", default="es")

    sp = add("check", cmd_check, "list paragraphs that still read as English")
    sp.add_argument("pdf"); sp.add_argument("--lang", default="es")

    a = p.parse_args(argv)
    if a.cmd == "section" and a.pages:
        a.first, a.last = (int(x) for x in a.pages.split("-"))
    elif a.cmd == "section":
        a.first = a.last = None
    a.fn(a)


if __name__ == "__main__":
    main()
