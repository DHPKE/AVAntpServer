#!/usr/bin/env python3
"""Create and file notes in the slip-box.

  new_note.py fleeting  "quick thought ..."
  new_note.py literature CITEKEY --title "Book title" --ref "Author (Year). Title. Publisher."
  new_note.py permanent "One claim, as a full sentence" --link 202610221530:supports --source @citekey --body "text"
  new_note.py project   NAME --question "What do I want to find out / produce?"
  new_note.py index     "Topic" 202610221530 [202610221611 ...]
  new_note.py processed inbox/<file>.md        (moves a handled fleeting note to inbox/.processed/)

A permanent note must link to an existing one (--link ID[:why], repeatable) - the first note of an empty box is exempt.
The link is written both ways: the target note gets a back-reference under its Connections.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slipbox import ID_RE, find_root, permanent_notes, render, slug  # noqa: E402

PLACEHOLDER_BODY = "(Write the idea in full sentences, in your own words, so that someone else could follow it. One idea only.)"


def now() -> dt.datetime:
    return dt.datetime.now().replace(second=0, microsecond=0)


def unique_id(root: Path) -> str:
    taken = set(permanent_notes(root))
    t = now()
    while t.strftime("%Y%m%d%H%M") in taken:
        t += dt.timedelta(minutes=1)
    return t.strftime("%Y%m%d%H%M")


def add_connection(path: Path, note_id: str, why: str) -> None:
    """Append '- [[id]] - why' under the note's '## Connections' (created if missing)."""
    text = path.read_text()
    line = f"- [[{note_id}]] - {why}" if why else f"- [[{note_id}]]"
    if re.search(rf"\[\[{note_id}(?:\|[^\]]*)?\]\]", text):
        return  # already linked
    if re.search(r"^## Connections\s*$", text, re.M):
        text = re.sub(r"(^## Connections\s*\n)((?:- .*\n?)*)", lambda m: m.group(1) + m.group(2).rstrip("\n") + ("\n" if m.group(2).strip() else "") + line + "\n", text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n\n## Connections\n{line}\n"
    path.write_text(text)


def cmd_fleeting(root: Path, a) -> None:
    t = dt.datetime.now()
    stem = f"{t:%Y%m%d-%H%M}-{slug(a.text, 40)}"
    path, n = root / "inbox" / f"{stem}.md", 2
    while path.exists():                       # two captures with the same words in the same minute
        path, n = root / "inbox" / f"{stem}-{n}.md", n + 1
    path.write_text(render(root, "fleeting", created=f"{t:%Y-%m-%d %H:%M}", body=a.text))
    print(path)


def cmd_literature(root: Path, a) -> None:
    key = a.key.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", key):
        sys.exit("Citekey: letters, digits, - and _ only (e.g. ahrens2017).")
    path = root / "literature" / f"@{key}.md"
    if path.exists():
        sys.exit(f"{path} already exists.")
    path.write_text(render(root, "literature", key=key, title=a.title or key, ref=a.ref or "(add the full reference)",
                           created=f"{now():%Y-%m-%d}"))
    print(path)


def cmd_permanent(root: Path, a) -> None:
    notes = permanent_notes(root)
    links = []
    for spec in a.link or []:
        lid, _, why = spec.partition(":")
        if not ID_RE.fullmatch(lid) or lid not in notes:
            sys.exit(f"--link {lid!r}: no permanent note with that id.")
        links.append((lid, why.strip()))
    if notes and not links:
        sys.exit("A permanent note must connect to at least one existing note: add --link ID[:why]. "
                 "(Ask: what does this support, contradict, extend, or give an example of?)")
    if len(a.title.split()) < 4:
        print("warning: the title should state the idea as a sentence, not name a topic.", file=sys.stderr)
    nid = unique_id(root)
    path = root / "permanent" / f"{nid}-{slug(a.title)}.md"
    connections = "\n".join(f"- [[{i}]] - {w}" if w else f"- [[{i}]]" for i, w in links) or "(first note - links will grow around it)"
    source = f"- [[{a.source if a.source.startswith('@') else '@' + a.source}]]" if a.source else "- own thought"
    path.write_text(render(root, "permanent", id=nid, title=a.title, body=a.body or PLACEHOLDER_BODY,
                           created=f"{now():%Y-%m-%d %H:%M}", connections=connections, source=source))
    for lid, _ in links:  # the link is written both ways
        add_connection(notes[lid][0], nid, "")
    print(path)
    print(f"link it with: [[{nid}]]")


def cmd_project(root: Path, a) -> None:
    d = root / "projects" / slug(a.name)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "README.md"
    if path.exists():
        sys.exit(f"{path} already exists.")
    path.write_text(render(root, "project", name=a.name, question=a.question or "(what do you want to find out or produce?)",
                           created=f"{now():%Y-%m-%d}"))
    print(path)


def cmd_index(root: Path, a) -> None:
    notes = permanent_notes(root)
    for i in a.ids:
        if i not in notes:
            sys.exit(f"{i}: no permanent note with that id.")
    path = root / "index.md"
    text = path.read_text() if path.exists() else "# Index - entry points\n\n## Entry points\n"
    if "## Entry points" not in text:
        text = text.rstrip("\n") + "\n\n## Entry points\n"
    line_re = re.compile(rf"^- \*\*{re.escape(a.topic)}\*\* .*$", re.M)
    m = line_re.search(text)
    if m:
        have = re.findall(r"\[\[(\d{12})\]\]", m.group(0))
        ids = have + [i for i in a.ids if i not in have]
        text = text[:m.start()] + f"- **{a.topic}** → " + ", ".join(f"[[{i}]]" for i in ids) + text[m.end():]
    else:
        text = text.rstrip("\n") + f"\n- **{a.topic}** → " + ", ".join(f"[[{i}]]" for i in a.ids) + "\n"
    path.write_text(text)
    print(f"index: {a.topic} -> {', '.join(a.ids)}")


def cmd_processed(root: Path, a) -> None:
    src = Path(a.file).expanduser()
    src = src if src.is_absolute() else (root / src)
    src = src.resolve()
    if src.parent != (root / "inbox").resolve() or not src.is_file():
        sys.exit("Only a file directly inside inbox/ can be marked as processed.")
    dest_dir = root / "inbox" / ".processed"
    dest_dir.mkdir(exist_ok=True)
    src.rename(dest_dir / src.name)
    print(f"moved to {dest_dir / src.name}  (fleeting notes are disposable once their idea lives in a permanent note)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="slip-box folder (default: the folder containing _tools)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("fleeting"); s.add_argument("text")
    s = sub.add_parser("literature"); s.add_argument("key"); s.add_argument("--title"); s.add_argument("--ref")
    s = sub.add_parser("permanent"); s.add_argument("title"); s.add_argument("--link", action="append")
    s.add_argument("--source"); s.add_argument("--body")
    s = sub.add_parser("project"); s.add_argument("name"); s.add_argument("--question")
    s = sub.add_parser("index"); s.add_argument("topic"); s.add_argument("ids", nargs="+")
    s = sub.add_parser("processed"); s.add_argument("file")
    a = p.parse_args()
    root = find_root(a.root)
    {"fleeting": cmd_fleeting, "literature": cmd_literature, "permanent": cmd_permanent,
     "project": cmd_project, "index": cmd_index, "processed": cmd_processed}[a.cmd](root, a)


if __name__ == "__main__":
    main()
