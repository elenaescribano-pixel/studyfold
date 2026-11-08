"""Build examples/demo_notes.pdf: a short, Notion-style study-notes PDF used by the
README screenshots and the tests. All content is written for this repo.

    python examples/make_demo_pdf.py [output.pdf]
"""
import sys

import pymupdf as fitz

SECTION = """
<h2>{n}. {title}</h2>
<p>{intro} Remember that <b>every value has a type</b>, and the type decides which
operations are allowed. When in doubt, check it with <code>type(x)</code> in the console.</p>
<div class="callout"><b>Key idea:</b> {key}</div>
<ul>
  <li>Names should describe what a value means, not how it is stored.</li>
  <li>Small functions are easier to test than long scripts.</li>
  <li>Read the error message from the bottom line upwards.</li>
</ul>
<pre>{code}</pre>
<p>In the exam, explain <i>why</i> your solution works, not only what it prints.
A short comment above a tricky line is often enough.</p>
<table>
  <tr><th>Concept</th><th>Example</th><th>Common mistake</th></tr>
  <tr><td>List</td><td><code>[1, 2, 3]</code></td><td>Changing it while looping over it</td></tr>
  <tr><td>Dictionary</td><td><code>{{"a": 1}}</code></td><td>Assuming the key always exists</td></tr>
</table>
"""

TOPICS = [
    ("Variables and types", "A variable is a name that points to a value.",
     "assignment creates a name; it never copies the object.",
     "price = 19.99\ncount = 3\ntotal = price * count\nprint(f\"Total: {total:.2f}\")"),
    ("Conditions", "Programs make decisions with <code>if</code>, <code>elif</code> and <code>else</code>.",
     "only the first matching branch runs.",
     "def grade(score):\n    if score >= 9:\n        return \"A\"\n    elif score >= 5:\n        return \"Pass\"\n    return \"Fail\""),
    ("Loops", "A loop repeats a block of code for each item of a sequence.",
     "use <code>for</code> when you know the items, <code>while</code> when you wait for a condition.",
     "words = [\"data\", \"cloud\", \"python\"]\nfor i, w in enumerate(words, start=1):\n    print(i, w.upper())"),
    ("Functions", "A function packs a task behind a name, with inputs and an output.",
     "a function without <code>return</code> gives back <code>None</code>.",
     "def average(values):\n    if not values:\n        return 0\n    return sum(values) / len(values)"),
    ("Dictionaries", "A dictionary maps keys to values, like a small database table.",
     "use <code>.get(key, default)</code> when a key may be missing.",
     "stock = {\"apples\": 4, \"pears\": 0}\nfor fruit, n in stock.items():\n    print(fruit, \"OK\" if n else \"empty\")"),
]

CSS = """
body { font-family: sans-serif; font-size: 11pt; color: #37352f; }
h1 { font-size: 22pt; margin-bottom: 4pt; }
h2 { font-size: 15pt; margin-top: 16pt; color: #1f1f1f; }
p { margin: 6pt 0; line-height: 1.45; }
code { font-family: monospace; color: #eb5757; background-color: #f2f1ee; }
pre { font-family: monospace; font-size: 9.5pt; background-color: #f7f6f3; padding: 8pt; color: #0b5394; }
.callout { background-color: #fbf3db; padding: 8pt; margin: 8pt 0; }
li { margin: 2pt 0; }
table { border-collapse: collapse; margin: 8pt 0; }
th, td { border: 1px solid #d9d9d6; padding: 3pt 6pt; font-size: 10pt; }
th { background-color: #f1f1ef; }
"""


def build(out: str):
    html = "<h1>Programming Basics · Study Notes</h1><p>Demo notes generated for studyfold.</p>"
    html += "".join(SECTION.format(n=i + 1, title=t, intro=a, key=k, code=c)
                    for i, (t, a, k, c) in enumerate(TOPICS))
    story = fitz.Story(html=html, user_css=CSS)
    writer = fitz.DocumentWriter(out)
    page_rect = fitz.paper_rect("a4")
    where = page_rect + (56, 56, -56, -70)
    more, pno = True, 0
    while more:
        dev = writer.begin_page(page_rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        pno += 1
    writer.close()
    # page numbers in the footer band, as Notion exports have
    doc = fitz.open(out)
    for i, p in enumerate(doc):
        p.insert_text((page_rect.width / 2 - 4, page_rect.height - 30), str(i + 1), fontsize=9, color=(0.5, 0.5, 0.5))
    doc.saveIncr()
    return len(doc)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "examples/demo_notes.pdf"
    print(f"{build(out)} pages -> {out}")
