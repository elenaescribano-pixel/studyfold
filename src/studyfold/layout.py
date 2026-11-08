"""Turn a PDF into translatable paragraphs, and write translations back in place.

The work file (JSON) holds, for every paragraph: its page, bounding box, font
size, the styles of its spans and its text as light markup:

    <r0>…</r0>   a run in another style (bold, colour, italic)
    <k0>…</k0>   a run of code (monospace) – never translated
    <br>         a hard line break
    <e0/>        an emoji, re-drawn as an image

Rendering removes the original text of each paragraph and fits the translation
into the same area (it may grow downwards into empty space, or shrink).
Paragraphs whose translation equals the source are left exactly as drawn, which
keeps formulas and code pixel-perfect.
"""
from __future__ import annotations

import html
import json
import os
import re
import tempfile

import pymupdf as fitz

FOOTER_MARGIN = 57
LETTER = re.compile(r"[A-Za-zÀ-ÿ]")
MARKER = re.compile(r"^\s*(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,4})[.)]\s*$|^\s*[•◦▪▸‣·\-–—]\s*$")
MONO = re.compile(r"Mono|Cascadia|Courier|Consol|Menlo|Code")
ITEM_START = re.compile(r"^\s*(?:[•◦▪▸‣·]|\d{1,3}[.)])\s+\S")
TAGS = re.compile(r"</?[rk]\d+>|<br>|<e\d+/>")


# --------------------------------------------------------------------------- fonts

def _weight_from_name(name: str) -> int:
    if "Black" in name or "Heavy" in name:
        return 900
    if "Bold" in name:
        return 700
    if "Medium" in name:
        return 500
    return 400


def font_info(doc: fitz.Document) -> dict:
    """xref -> (weight, mono, emoji, italic) for every font object in the document."""
    info = {}
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Type")[1] != "/Font":
                continue
        except Exception:
            continue
        fd = doc.xref_get_key(x, "FontDescriptor")[1]
        name, weight = doc.xref_get_key(x, "BaseFont")[1], "null"
        if fd != "null":
            n = int(fd.split()[0])
            name = doc.xref_get_key(n, "FontName")[1] or name
            weight = doc.xref_get_key(n, "FontWeight")[1]
        else:
            m = re.search(r"(\d+) 0 R", doc.xref_get_key(x, "DescendantFonts")[1])
            if m:
                fd2 = doc.xref_get_key(int(m.group(1)), "FontDescriptor")[1]
                if fd2 != "null":
                    n = int(fd2.split()[0])
                    name = doc.xref_get_key(n, "FontName")[1]
                    weight = doc.xref_get_key(n, "FontWeight")[1]
        # Subset fonts sometimes declare weight 400 although the name says Bold.
        w = max(int(weight) if weight.isdigit() else 400, _weight_from_name(name))
        info[x] = (w, bool(MONO.search(name)), "Emoji" in name, "Italic" in name or "Oblique" in name)
    return info


def span_font(span, finfo, byname):
    m = re.search(r"\((\d+) 0 R\)", span["font"])
    if m and int(m.group(1)) in finfo:
        return finfo[int(m.group(1))]
    n = span["font"]
    if n in byname:
        return byname[n]
    return (_weight_from_name(n), bool(MONO.search(n)), "Emoji" in n, "Italic" in n)


def _segclass(seg):
    """Dominant (bold?, mono?) of a line segment, weighted by characters."""
    cnt = {}
    for sp in seg["spans"]:
        if sp["t"].strip() and not sp.get("e"):
            k = (sp["w"] >= 600, sp["m"])
            cnt[k] = cnt.get(k, 0) + len(sp["t"])
    return max(cnt, key=cnt.get) if cnt else (False, False)


# --------------------------------------------------------------------------- extract

def extract(src: str, out: str) -> dict:
    """Group the text of ``src`` into paragraphs and write the work file ``out``."""
    doc = fitz.open(src)
    finfo = font_info(doc)
    byname = {}
    groups = []
    for page in doc:
        for f in page.get_fonts():
            byname.setdefault(f[3].split("+")[-1], finfo.get(f[0], (400, False, False, False)))
        open_groups = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                if abs(line["dir"][1]) > 0.01:  # rotated text: leave alone
                    continue
                segs, cur, seen_marker = [], None, False
                spans = [s for s in line["spans"] if s["text"]]
                for si, s in enumerate(spans):
                    w, mono, emoji, ital = span_font(s, finfo, byname)
                    sp = dict(t=s["text"], b=list(s["bbox"]), w=w, m=mono, i=ital,
                              c=s["color"], z=round(s["size"], 2))
                    if emoji:
                        nxt = spans[si + 1] if si + 1 < len(spans) else None
                        if cur is None and (nxt is None or nxt["bbox"][0] - s["bbox"][2] > 0.5 * nxt["size"]):
                            continue  # leading icon of a callout: left untouched
                        sp["e"] = True
                    marker = bool(MARKER.match(s["text"])) and cur is None
                    if cur is None or s["bbox"][0] - cur["b"][2] > 0.6 * s["size"] or cur.get("marker"):
                        cur = dict(spans=[sp], b=list(s["bbox"]), marker=marker, after=seen_marker)
                        seen_marker = seen_marker or marker
                        segs.append(cur)
                    else:
                        cur["spans"].append(sp)
                        cur["b"] = [min(cur["b"][0], sp["b"][0]), min(cur["b"][1], sp["b"][1]),
                                    max(cur["b"][2], sp["b"][2]), max(cur["b"][3], sp["b"][3])]
                for seg in segs:
                    size = max([sp["z"] for sp in seg["spans"] if not sp.get("e")] or [seg["spans"][0]["z"]])
                    seg["cls"] = _segclass(seg)
                    seg_text = "".join(sp["t"] for sp in seg["spans"])
                    starts_item = bool(ITEM_START.match(seg_text)) and not seg["marker"]
                    g = None
                    for og in open_groups:
                        last = og["lines"][-1]
                        # Join a line to the paragraph above only if it continues it:
                        # same left edge, size and style class, no list marker, not code.
                        if (not og["marker"] and not seg["after"] and not seg["cls"][1] and not starts_item
                                and last["cls"] == seg["cls"] and abs(last["b"][0] - seg["b"][0]) < 3
                                and abs(og["z"] - size) < 0.6
                                and 0 < seg["b"][1] - last["b"][1] < 1.75 * size and not seg["marker"]):
                            g = og
                            break
                    if g is None:
                        g = dict(page=page.number, z=size, lines=[], marker=seg["marker"])
                        open_groups.append(g)
                        groups.append(g)
                    g["lines"].append(seg)

    out_groups = [p for p in (_to_markup(g, len_hint=i) for i, g in enumerate(groups)) if p]
    for i, g in enumerate(out_groups):
        g["id"] = f"g{i}"
    data = dict(src=os.path.abspath(src), groups=out_groups, tr={})
    _save(data, out)
    return data


def _to_markup(g, len_hint=0):
    text = "".join(sp["t"] for ln in g["lines"] for sp in ln["spans"])
    if g["marker"] or not LETTER.search(text):
        return None
    maxx = max(ln["b"][2] for ln in g["lines"])
    cnt = {}
    for ln in g["lines"]:
        for sp in ln["spans"]:
            if sp["t"].strip() and not sp.get("e"):
                k = (sp["w"], sp["m"], sp["i"], sp["c"], round(sp["z"]))
                cnt[k] = cnt.get(k, 0) + len(sp["t"])
    if not cnt:
        return None
    base = max(cnt, key=cnt.get)
    styles, parts, emojis = [], [], []
    state = dict(k=None, t="")

    def flush():
        if not state["t"]:
            return
        if state["k"] == base or state["k"] is None:
            parts.append(html.escape(state["t"], quote=False))
        else:
            if state["k"] not in styles:
                styles.append(state["k"])
            n = styles.index(state["k"])
            tag = f"k{n}" if state["k"][1] else f"r{n}"
            parts.append(f"<{tag}>{html.escape(state['t'], quote=False)}</{tag}>")
        state["t"] = ""

    for li, ln in enumerate(g["lines"]):
        if li:
            prev = g["lines"][li - 1]
            first = next((sp for sp in ln["spans"] if sp["t"].strip()), ln["spans"][0])
            fw = first["b"][2] - first["b"][0]
            # A line that ended well before the right edge, although the next word would
            # have fitted, was a deliberate line break.
            hard = prev["b"][2] + fw + 0.35 * g["z"] < maxx - 1
            ptxt = "".join(sp["t"] for sp in prev["spans"]).rstrip()
            lastw = ([sp for sp in prev["spans"] if sp["t"].strip()] or prev["spans"])[-1]["w"]
            if re.search(r"[.!?:]$", ptxt) and first["w"] > base[0] and lastw <= base[0]:
                hard = True
            if hard:
                flush()
                parts.append("<br>")
            elif ptxt.endswith("-") and not ptxt.endswith(" -"):
                state["t"] = state["t"].rstrip()
            elif not (state["t"].endswith(" ") or ln["spans"][0]["t"].startswith(" ")):
                state["t"] += " "
        for sp in ln["spans"]:
            k = (sp["w"], sp["m"], sp["i"], sp["c"], round(sp["z"]))
            if sp.get("e"):
                flush()
                state["k"] = None
                parts.append(f"<e{len(emojis)}/>")
                emojis.append(sp["b"])
                continue
            if not sp["t"].strip():
                state["t"] += sp["t"]
                continue
            if k != state["k"]:
                flush()
                state["k"] = k
            state["t"] += sp["t"]
    flush()
    markup = re.sub(r"\s+", " ", "".join(parts)).strip()
    rect = [min(ln["b"][0] for ln in g["lines"]), min(ln["b"][1] for ln in g["lines"]),
            maxx, max(ln["b"][3] for ln in g["lines"])]
    return dict(page=g["page"], rect=rect, z=g["z"], base=base, styles=styles, src=markup,
                emojis=emojis, spans=[sp["b"] for ln in g["lines"] for sp in ln["spans"]])


def _save(data, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load(path):
    with open(path) as f:
        return json.load(f)


def save(data, path):
    _save(data, path)


def tags_match(src: str, tr: str) -> bool:
    return sorted(TAGS.findall(src)) == sorted(TAGS.findall(tr))


def reuse(data: dict, *older: dict) -> int:
    """Copy translations from older work files whose source text is identical."""
    memo = {}
    for d in older:
        for g in d["groups"]:
            if g["id"] in d["tr"]:
                memo[g["src"]] = d["tr"][g["id"]]
    n = 0
    for g in data["groups"]:
        if g["id"] not in data["tr"] and g["src"] in memo:
            data["tr"][g["id"]] = memo[g["src"]]
            n += 1
    return n


# --------------------------------------------------------------------------- render

def _font_setup():
    """Use Inter if installed (closest match to Notion exports), else PyMuPDF's built-in sans."""
    dirs = [os.environ.get("STUDYFOLD_FONTS", ""), os.path.expanduser("~/Library/Fonts"),
            os.path.expanduser("~/.fonts"), os.path.expanduser("~/.local/share/fonts"),
            "/usr/share/fonts/opentype/inter", "C:/Windows/Fonts"]
    for d in dirs:
        if d and all(os.path.exists(os.path.join(d, f"Inter-{n}.otf"))
                     for n in ("Regular", "Medium", "SemiBold", "Bold")):
            css = "".join(f"@font-face {{font-family: {f}; src: url(Inter-{n}.otf);}}\n"
                          for f, n in [("InterR", "Regular"), ("InterM", "Medium"),
                                       ("InterS", "SemiBold"), ("InterB", "Bold")])
            return d, css, {400: "InterR", 500: "InterM", 600: "InterS", 700: "InterB"}
    return None, "", None


def _style_css(k, families):
    w, mono, ital, color, _size = k
    if mono:
        s = "font-family:monospace;"
    elif families:
        s = f"font-family:{families[min(families, key=lambda x: abs(x - w))]};"
    else:
        s = f"font-family:sans-serif;font-weight:{'bold' if w >= 600 else 'normal'};"
    s += f"color:#{color:06x};"
    if ital:
        s += "font-style:italic;"
    return s


def render(src: str, work: str, out: str, footer: float = FOOTER_MARGIN) -> int:
    """Write ``out``: ``src`` with every translated paragraph replaced. Returns #unplaced."""
    data = load(work)
    missing = [g["id"] for g in data["groups"] if g["id"] not in data["tr"]]
    if missing:
        raise RuntimeError(f"{len(missing)} paragraphs are not translated yet (first: {missing[0]})")
    fontdir, font_css, families = _font_setup()
    doc = fitz.open(src)
    arch = fitz.Archive()
    if fontdir:
        arch.add(fontdir)
    edir = tempfile.mkdtemp()
    arch.add(edir)
    bypage = {}
    for g in data["groups"]:
        bypage.setdefault(g["page"], []).append(g)
    unplaced = 0
    for page in doc:
        gs = [g for g in bypage.get(page.number, []) if data["tr"][g["id"]] != g["src"]]
        if not gs:
            continue
        plan = _plan_page(page, gs, edir, footer)  # also removes the old text
        for g, rect, yb2 in plan:
            z = g["z"]
            body = data["tr"][g["id"]]
            for n, k in enumerate(g["styles"]):
                for t in ("r", "k"):
                    body = (body.replace(f"<{t}{n}>", f'<span style="{_style_css(k, families)}">')
                                .replace(f"</{t}{n}>", "</span>"))
            for n, eb in enumerate(g.get("emojis", [])):
                eh = fitz.Rect(eb).height * 0.8
                body = body.replace(f"<e{n}/>", f'<img src="{g["id"]}_{n}.png" width="{eh:.1f}" height="{eh:.1f}"/>')
            css = (font_css + f"* {{margin:0;padding:0;}} body {{font-size:{z}px;line-height:{1.5 * z}px;"
                   f"{_style_css(g['base'], families)}}}")
            box = fitz.Rect(rect.x0, rect.y0 - 0.2 * z, rect.x1 + 1, rect.y1 + 0.2 * z)
            # Try: natural size in the planned box, then grow into the fallback area, then shrink harder.
            for bottom, low in ((box.y1, 0.8), (yb2 + 0.2 * z, 0.8), (box.y1, 0.3)):
                r = page.insert_htmlbox(fitz.Rect(box.x0, box.y0, box.x1, bottom), f"<div>{body}</div>",
                                        css=css, archive=arch, scale_low=low)
                if r[0] >= 0:
                    break
            else:
                unplaced += 1
    doc.save(out, garbage=4, deflate=True)
    return unplaced


def _plan_page(page, gs, edir, footer):
    """For each paragraph: the box it may use, and mark its old text for removal."""
    foot = page.rect.height - footer
    draws = page.get_drawings()
    imgs = [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
    alltext = [fitz.Rect(s["bbox"]) for b in page.get_text("dict")["blocks"]
               for ln in b.get("lines", []) for s in ln["spans"] if s["text"].strip()]
    right_edge = max([r.x1 for r in alltext if r.y1 < foot] + [page.rect.width - 72])
    plan, span_rects, draw_rects = [], [], []
    for g in gs:
        for n, eb in enumerate(g.get("emojis", [])):
            page.get_pixmap(clip=fitz.Rect(eb), dpi=200).save(os.path.join(edir, f"{g['id']}_{n}.png"))
        R = fitz.Rect(g["rect"])
        own = [fitz.Rect(b) for b in g["spans"]]
        pitch = 1.5 * g["z"]
        boxes = [d["rect"] for d in draws if d.get("fill") and d["rect"].contains(R) and d["rect"].width > R.width + 2]
        box = min(boxes, key=lambda r: r.width * r.height) if boxes else None
        xr = (box.x1 - (R.x0 - box.x0)) if box else right_edge
        if R.y0 > foot:
            xr = page.rect.width - 27
        others = [r for r in alltext if not any(r.intersects(o) and abs(r.y0 - o.y0) < 1 for o in own)]
        for r in others + imgs:  # something to the right on the same lines
            if r.y1 > R.y0 + 2 and r.y0 < R.y1 - 2 and r.x0 >= R.x1 - 1:
                xr = min(xr, r.x0 - 6)
        xr = max(xr, R.x1)
        yb = R.y1 + 2 * pitch
        if box:
            yb = min(yb, box.y1 - 3)
        for r in others + imgs:
            if r.y0 >= R.y1 - 1 and r.x1 > R.x0 and r.x0 < xr:
                yb = min(yb, r.y0 - 2)
        for d in draws:
            r = d["rect"]
            if r.y0 >= R.y1 - 1 and r.x1 > R.x0 + 2 and r.x0 < xr - 2 and not (box and r == box):
                yb = min(yb, r.y0 - 1)
        yb = max(yb, R.y1)
        yb2 = R.y1 + 3 * pitch  # fallback: may cross table rules, but never text
        for r in others + imgs:
            if r.y0 >= R.y1 - 1 and r.x1 > R.x0 and r.x0 < xr:
                yb2 = min(yb2, r.y0 - 2)
        if box:
            yb2 = min(yb2, box.y1 - 3)
        plan.append((g, fitz.Rect(R.x0, R.y0, xr, yb), max(yb, yb2)))
        for b in own:
            h = b.height
            span_rects.append(fitz.Rect(b.x0, b.y0 + 0.2 * h, b.x1, b.y1 - 0.2 * h))
        for d in draws:  # link underlines / inline-code backgrounds inside the paragraph
            r = d["rect"]
            if (r.height < 1.6 * g["z"] and R.x0 - 1 <= r.x0 and r.x1 <= R.x1 + 1
                    and R.y0 - 2 <= r.y0 and r.y1 <= R.y1 + 3):
                draw_rects.append(r + (-0.3, -0.3, 0.3, 0.3))
    # Two passes: when text and line-art redactions overlap, MuPDF keeps the line art.
    for r in draw_rects:
        page.add_redact_annot(r, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                          text=fitz.PDF_REDACT_TEXT_NONE)
    for r in span_rects:
        page.add_redact_annot(r, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                          graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    return plan


# --------------------------------------------------------------------------- check

MARKERS = {
    "en": "the of and to in is that for with this are as on it be by an or from which not",
    "es": "el la los las de del que y en un una para con por se es no como más su sus al lo este esta",
    "fr": "le la les de des du et en un une pour avec par est que qui dans sur ce cette pas",
    "it": "il lo la gli le di del della e in un una per con che è non sono come questo",
    "de": "der die das und ist nicht mit von zu den dem ein eine für auf sich auch",
    "pt": "o a os as de do da que e em um uma para com por se é não como mais",
}


def check(pdf: str, lang: str = "es", source_lang: str = "en"):
    """Paragraphs of the finished PDF that still read as the source language.

    Returns (target_markers, source_markers, [(page, text), ...]).
    """
    tgt = set(MARKERS.get(lang, "").split())
    srcw = set(MARKERS[source_lang].split())
    tot_t = tot_s = 0
    bad = []
    for p in fitz.open(pdf):
        for b in p.get_text("blocks"):
            if b[6]:
                continue
            w = re.findall(r"[A-Za-zÀ-ÿ]+", b[4].lower())
            t, s = sum(x in tgt for x in w), sum(x in srcw for x in w)
            tot_t += t
            tot_s += s
            if s >= 3 and s > t:
                bad.append((p.number + 1, b[4][:100].replace("\n", " ")))
    return tot_t, tot_s, bad
