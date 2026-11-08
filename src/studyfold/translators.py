"""Translation backends.

* ``claude``  – sends batches to the Claude Code CLI (``claude -p``), in parallel.
* ``manual``  – writes batches to JSON files you can translate anywhere (another
                tool, a chat, by hand) and imports the answers back.

Every answer is validated: a translation is only accepted if it keeps exactly the
same markup tags as its source, so styles, code and emojis cannot get lost.
"""
from __future__ import annotations

import concurrent.futures as cf
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading

from .layout import load, save, tags_match

LANG_NAMES = {"es": "Spanish (Spain)", "fr": "French", "it": "Italian", "de": "German",
              "pt": "Portuguese", "en": "English"}

PROMPT = """You are translating university study notes{subject} into {lang}.
Input: a JSON object mapping ids to text fragments. Output: ONLY a JSON object with exactly the same keys, each value translated into natural, clear {lang}. Every fragment MUST come back translated.
Rules:
- Keep every tag exactly as written (<r0>…</r0>, <k0>…</k0>, <br>, <e0/>); translate the text inside <r…> tags, place each tag pair around the corresponding translated words, never add or drop tags.
- Never translate text inside <k…> tags (code, commands, identifiers).
- Keep product, company and library names, acronyms and code unchanged. Keep English technical terms only where speakers of {lang} really use them as-is.
- Fragments may be headings, table cells or list items; translate each one on its own, short and faithful. Do not add explanations.
- Escape inner double quotes so the output is valid JSON.
"""


class TranslatorUnavailable(RuntimeError):
    """The backend itself is failing (not logged in, usage limit…): stop instead of retrying."""


def pending(data: dict) -> dict:
    return {g["id"]: g["src"] for g in data["groups"] if g["id"] not in data["tr"]}


def batches(todo: dict, max_chars: int = 5000) -> list[dict]:
    out, cur, n = [], {}, 0
    for k, v in todo.items():
        cur[k] = v
        n += len(v)
        if n > max_chars:
            out.append(cur)
            cur, n = {}, 0
    if cur:
        out.append(cur)
    return out


def accept(batch: dict, answer: dict) -> dict:
    """Keep only answers for known ids whose tags match their source."""
    return {k: v for k, v in answer.items()
            if k in batch and isinstance(v, str) and tags_match(batch[k], v)}


def parse_json_object(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


# --------------------------------------------------------------------------- claude CLI

class ClaudeCLI:
    def __init__(self, lang: str = "es", subject: str = "", model: str = "sonnet", workers: int = 5):
        if not shutil.which("claude"):
            raise TranslatorUnavailable("the `claude` command was not found; install Claude Code "
                                        "or use --backend manual")
        self.prompt = PROMPT.format(lang=LANG_NAMES.get(lang, lang),
                                    subject=f" (a {subject} course)" if subject else "")
        self.model, self.workers = model, workers

    def _ask(self, batch: dict) -> dict:
        try:
            r = subprocess.run(["claude", "-p", "--model", self.model],
                               input=self.prompt + "\nINPUT:\n" + json.dumps(batch, ensure_ascii=False),
                               capture_output=True, text=True, timeout=600, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            return {}
        out = r.stdout.strip()
        if r.returncode != 0 or re.search(r"usage limit|rate limit|limit reached|credit balance",
                                          out[:300], re.I):
            raise TranslatorUnavailable((out or r.stderr)[:300])
        return accept(batch, parse_json_object(out))

    def _batch(self, batch: dict) -> dict:
        """Retry once, then split, so one bad fragment cannot sink its neighbours."""
        done = {}
        for _ in range(2):
            todo = {k: v for k, v in batch.items() if k not in done}
            if not todo:
                return done
            done.update(self._ask(todo))
        todo = {k: v for k, v in batch.items() if k not in done}
        if len(todo) > 1:
            keys = list(todo)
            for part in (keys[:len(keys) // 2], keys[len(keys) // 2:]):
                done.update(self._batch({k: todo[k] for k in part}))
        return done

    def run(self, work: str, progress=print) -> int:
        """Translate every pending paragraph of ``work``; saves after each batch. Returns #missing."""
        data = load(work)
        bs = batches(pending(data))
        progress(f"{sum(map(len, bs))} paragraphs to translate in {len(bs)} batches")
        lock, failure = threading.Lock(), None
        with cf.ThreadPoolExecutor(self.workers) as ex:
            futs = [ex.submit(self._batch, b) for b in bs]
            for f in cf.as_completed(futs):
                try:
                    res = f.result()
                except cf.CancelledError:
                    continue
                except TranslatorUnavailable as e:
                    failure = e
                    for other in futs:
                        other.cancel()
                    continue
                with lock:
                    data["tr"].update(res)
                    save(data, work)
                    progress(f"  {len(data['tr'])}/{len(data['groups'])}")
        if failure:
            raise failure
        return len(pending(data))


# --------------------------------------------------------------------------- manual

def export_batches(work: str, folder: str, lang: str = "es", subject: str = "",
                   max_chars: int = 5000) -> int:
    """Write ``batch_NNN.json`` files (plus PROMPT.txt) with every pending paragraph."""
    os.makedirs(folder, exist_ok=True)
    bs = batches(pending(load(work)), max_chars)
    for i, b in enumerate(bs):
        with open(os.path.join(folder, f"batch_{i:03d}.json"), "w") as f:
            json.dump(b, f, ensure_ascii=False, indent=1)
    with open(os.path.join(folder, "PROMPT.txt"), "w") as f:
        f.write(PROMPT.format(lang=LANG_NAMES.get(lang, lang),
                              subject=f" (a {subject} course)" if subject else ""))
        f.write("\nSave each answer as batch_NNN.tr.json next to its batch file.\n")
    return len(bs)


def import_batches(work: str, folder: str) -> tuple[int, int]:
    """Read every ``*.tr.json`` in ``folder``. Returns (accepted, rejected)."""
    data = load(work)
    todo = pending(data)
    ok = bad = 0
    for path in sorted(glob.glob(os.path.join(folder, "*.tr.json"))):
        with open(path) as f:
            answer = parse_json_object(f.read())
        good = accept(todo, answer)
        bad += len([k for k in answer if k in todo and k not in good])
        data["tr"].update(good)
        ok += len(good)
    save(data, work)
    return ok, bad
