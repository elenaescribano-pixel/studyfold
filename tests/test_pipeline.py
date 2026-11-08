"""End-to-end tests on the generated demo PDF. They need no network and no translator:
translations are produced by a fake that rewrites words but keeps the markup tags."""
import json
import os
import re
import subprocess
import sys

import pymupdf as fitz
import pytest

from studyfold import compact, extract, render
from studyfold.layout import TAGS, check, load, reuse, save, tags_match
from studyfold.pages import extract_section, find_heading, strip_bands
from studyfold.translators import accept, export_batches, import_batches, pending

ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session")
def demo(tmp_path_factory):
    out = tmp_path_factory.mktemp("demo") / "demo_notes.pdf"
    subprocess.run([sys.executable, os.path.join(ROOT, "examples", "make_demo_pdf.py"), str(out)], check=True)
    return str(out)


def fake_translate(markup: str) -> str:
    """'Translate' text outside tags and outside <k> code runs: prefix every word with 'es'."""
    out, i = [], 0
    for m in re.finditer(r"<k\d+>.*?</k\d+>|</?[rk]\d+>|<br>|<e\d+/>", markup):
        out.append(re.sub(r"[A-Za-z]+", lambda w: "es" + w.group(0), markup[i:m.start()]))
        out.append(m.group(0))
        i = m.end()
    out.append(re.sub(r"[A-Za-z]+", lambda w: "es" + w.group(0), markup[i:]))
    return "".join(out)


def text_of(pdf):
    return " ".join(p.get_text() for p in fitz.open(pdf))


def test_compact_keeps_text_and_saves_pages(demo, tmp_path):
    out = tmp_path / "c.pdf"
    r = compact(demo, str(out))
    assert r.pages_out < r.pages_in
    src_words = set(re.findall(r"[A-Za-z]{4,}", text_of(demo)))
    out_words = set(re.findall(r"[A-Za-z]{4,}", text_of(out)))
    assert src_words <= out_words  # nothing lost, nothing cut in half


def test_extract_finds_paragraphs_and_code(demo, tmp_path):
    data = extract(demo, str(tmp_path / "w.json"))
    srcs = [g["src"] for g in data["groups"]]
    assert any("Variables and types" in s for s in srcs)
    # headings are not glued to the paragraph that follows them
    assert not any("Variables and types A variable" in s for s in srcs)
    # inline code is marked so it is never translated
    assert any(re.search(r"<k\d+>type\(x\)</k\d+>", s) for s in srcs)


def test_render_replaces_text_and_keeps_code(demo, tmp_path):
    work = str(tmp_path / "w.json")
    data = extract(demo, work)
    # Whole code blocks (monospace base style) are sent too, so comments can be translated;
    # a real translator leaves the code itself alone, which the fake mimics here.
    data["tr"] = {g["id"]: g["src"] if g["base"][1] else fake_translate(g["src"]) for g in data["groups"]}
    save(data, work)
    out = str(tmp_path / "es.pdf")
    assert render(demo, work, out) == 0
    txt = text_of(out)
    assert "esVariables" in txt and "Variables and types" not in txt
    assert "type(x)" in txt                 # inline code untouched
    assert "total = price * count" in txt   # code block untouched
    assert len(fitz.open(out)) == len(fitz.open(demo))


def test_render_refuses_untranslated(demo, tmp_path):
    work = str(tmp_path / "w.json")
    extract(demo, work)
    with pytest.raises(RuntimeError):
        render(demo, work, str(tmp_path / "x.pdf"))


def test_tag_validation():
    src = "Use <k0>print()</k0> to <r1>show</r1> it<br>now"
    assert tags_match(src, "Usa <k0>print()</k0> para <r1>mostrarlo</r1><br>ya")
    assert not tags_match(src, "Usa print() para mostrarlo ya")
    assert accept({"g1": src}, {"g1": "Usa print()", "g9": "x"}) == {}


def test_manual_round_trip(demo, tmp_path):
    work = str(tmp_path / "w.json")
    extract(demo, work)
    folder = tmp_path / "batches"
    n = export_batches(work, str(folder))
    assert n >= 1 and (folder / "PROMPT.txt").exists()
    for f in folder.glob("batch_*.json"):
        batch = json.loads(f.read_text())
        answer = {k: fake_translate(v) for k, v in batch.items()}
        f.with_suffix(".tr.json").write_text(json.dumps(answer, ensure_ascii=False))
    ok, bad = import_batches(work, str(folder))
    assert bad == 0 and pending(load(work)) == {}


def test_reuse_copies_identical_paragraphs(demo, tmp_path):
    a = extract(demo, str(tmp_path / "a.json"))
    a["tr"] = {g["id"]: "X" for g in a["groups"]}
    b = extract(demo, str(tmp_path / "b.json"))
    assert reuse(b, a) == len(b["groups"])


def test_section_and_strip(demo, tmp_path):
    doc = fitz.open(demo)
    pno, y = find_heading(doc, "2. Conditions")
    end = find_heading(doc, "4. Functions")
    out = str(tmp_path / "s.pdf")
    extract_section(demo, out, pno + 1, end[0] + 1, top=y - 4, bottom=end[1] - 4)
    txt = text_of(out)
    assert "2. Conditions" in txt and "3. Loops" in txt
    assert "1. Variables" not in txt and "4. Functions" not in txt
    clean = str(tmp_path / "clean.pdf")
    strip_bands(demo, clean, below=doc[0].rect.height - 50)
    assert all(not re.search(r"^\s*\d+\s*$", p.get_text(clip=fitz.Rect(0, p.rect.height - 50, p.rect.width, p.rect.height)))
               for p in fitz.open(clean))


def test_check_flags_english(demo, tmp_path):
    _t, _s, bad = check(demo, "es")
    assert bad  # the untranslated demo is English


def test_bullets_become_separate_paragraphs(demo, tmp_path):
    data = extract(demo, str(tmp_path / "w.json"))
    items = [g["src"] for g in data["groups"] if g["src"].startswith("•")]
    assert items and all(s.count("•") == 1 for s in items)


def test_inline_code_background_is_removed(demo, tmp_path):
    work = str(tmp_path / "w.json")
    data = extract(demo, work)
    data["tr"] = {g["id"]: g["src"] if g["base"][1] else fake_translate(g["src"]) for g in data["groups"]}
    save(data, work)
    out = str(tmp_path / "es.pdf")
    render(demo, work, out)
    grey = (0.949, 0.945, 0.933)  # inline-code background colour of the demo
    def n_grey(pdf):
        return sum(1 for d in fitz.open(pdf)[0].get_drawings()
                   if d.get("fill") and all(abs(a - b) < 0.01 for a, b in zip(d["fill"], grey)))
    # each translated paragraph redraws its own inline-code backgrounds, never more than before
    assert n_grey(out) <= n_grey(demo)
