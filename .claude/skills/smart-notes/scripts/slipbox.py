"""Shared helpers for the smart-notes slip-box tools (Python 3 standard library only)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ID_RE = re.compile(r"\d{12}")                                   # YYYYMMDDHHMM - the note's permanent address
LINK_RE = re.compile(r"\[\[(\d{12}|@[A-Za-z0-9_\-]+)(?:\|[^\]]*)?\]\]")   # [[ID]] [[ID|text]] [[@citekey]]
FRONT_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
FOLDERS = ("inbox", "literature", "permanent", "projects")

DEFAULT_TEMPLATES = {
    "fleeting": "captured: {{created}}\n\n{{body}}\n",
    "literature": "---\ntype: literature\nkey: @{{key}}\ncreated: {{created}}\n---\n# @{{key}} - {{title}}\n\n"
                  "**Reference:** {{ref}}\n\n## Notes (own words, only what matters to my questions; add page numbers)\n-\n\n"
                  "## Ideas to develop (each becomes a permanent note)\n- [ ]\n",
    "permanent": "---\nid: {{id}}\ntype: permanent\ncreated: {{created}}\n---\n# {{title}}\n\n{{body}}\n\n"
                 "## Connections\n{{connections}}\n\n## Source\n{{source}}\n",
    "project": "---\ntype: project\nname: {{name}}\ncreated: {{created}}\n---\n# Project: {{name}}\n\n"
               "**Question / output:** {{question}}\n**Done when:**\n\n## Outline (built from permanent notes)\n- \n\n"
               "## Gaps (what the slip-box cannot answer yet)\n\n## Draft\n",
}


def find_root(arg: str | None = None) -> Path:
    if arg:
        root = Path(arg).expanduser().resolve()
    else:
        here = Path(__file__).resolve().parent
        if here.name != "_tools":
            sys.exit("Run this from <slip-box>/_tools, or pass --root <slip-box>.")
        root = here.parent
    missing = [d for d in FOLDERS if not (root / d).is_dir()]
    if missing:
        sys.exit(f"{root} is not a slip-box (missing: {', '.join(missing)}). Create one with init_slipbox.py.")
    return root


def render(root: Path, kind: str, **values: str) -> str:
    """Fill a template; a copy in <root>/_templates/ wins over the built-in default (so the user can edit them)."""
    tpl_file = root / "_templates" / f"{kind}.md"
    text = tpl_file.read_text() if tpl_file.exists() else DEFAULT_TEMPLATES[kind]
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return s or "note"


def strip_front(text: str) -> str:
    return FRONT_RE.sub("", text, count=1)


def first_heading(text: str) -> str:
    for line in strip_front(text).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def main_text(text: str) -> str:
    """The idea itself: everything after the title and before '## Connections' / '## Source'."""
    body = strip_front(text)
    body = re.split(r"^## (?:Connections|Source)\b", body, maxsplit=1, flags=re.M)[0]
    return "\n".join(l for l in body.splitlines() if not l.startswith("# ")).strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def links_in(text: str) -> list[str]:
    return LINK_RE.findall(strip_front(text))


def permanent_notes(root: Path) -> dict[str, list[Path]]:
    """id -> files (more than one file for an id means a duplicate)."""
    notes: dict[str, list[Path]] = {}
    for p in sorted((root / "permanent").glob("*.md")):
        if ID_RE.fullmatch(p.name[:12]):
            notes.setdefault(p.name[:12], []).append(p)
    return notes
