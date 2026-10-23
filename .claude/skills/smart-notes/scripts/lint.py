#!/usr/bin/env python3
"""Review the slip-box against the rules of the method (after Ahrens / Luhmann). Exit code 1 if there are errors.

ERRORS   broken links, duplicate ids, a permanent note that links to nothing, an index entry pointing nowhere
WARNINGS orphans (no note or index entry leads to it), too long (probably more than one idea), a title that names a
         topic instead of stating an idea, fleeting notes waiting too long, literature notes nothing was made from,
         an index that is turning into a keyword list
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slipbox import find_root, first_heading, links_in, main_text, permanent_notes, strip_front, word_count  # noqa: E402


def lint(root: Path, max_words: int, inbox_days: int, lit_days: int, max_per_topic: int):
    errors, warnings = [], []
    notes = permanent_notes(root)
    lit = {p.stem: p for p in (root / "literature").glob("@*.md")}      # '@key' -> file
    index_path = root / "index.md"
    index_text = index_path.read_text() if index_path.exists() else ""

    incoming: dict[str, set[str]] = {i: set() for i in notes}
    lit_used: set[str] = set()
    out_links: dict[str, int] = {}

    for nid, files in notes.items():
        if len(files) > 1:
            errors.append(f"duplicate id {nid}: " + ", ".join(f.name for f in files))
    for nid, files in notes.items():
        f = files[0]
        text = f.read_text()
        targets = links_in(text)
        perm_targets = [t for t in targets if not t.startswith("@")]
        out_links[nid] = len({t for t in perm_targets if t in notes and t != nid})   # only links that resolve connect a note
        for t in targets:
            if t.startswith("@"):
                lit_used.add(t)
                if t not in lit:
                    errors.append(f"{f.name}: link [[{t}]] has no literature note")
            elif t not in notes:
                errors.append(f"{f.name}: broken link [[{t}]]")
            elif t != nid:
                incoming[t].add(nid)
        if len(notes) > 1 and out_links[nid] == 0:
            errors.append(f"{f.name}: links to no other permanent note - connect it (what does it support, contradict, extend?)")
        words = word_count(main_text(text))
        if words > max_words:
            warnings.append(f"{f.name}: {words} words - probably more than one idea; split it")
        title = first_heading(text)
        if len(title.split()) < 4:
            warnings.append(f"{f.name}: title {title!r} names a topic; state the idea as a sentence")

    # index
    index_ids: set[str] = set()
    for line in index_text.splitlines():
        if not line.startswith("- **"):
            continue
        ids = re.findall(r"\[\[(\d{12})\]\]", line)
        index_ids.update(ids)
        for i in ids:
            if i not in notes:
                errors.append(f"index.md: entry points to missing note [[{i}]]")
        if len(ids) > max_per_topic:
            warnings.append(f"index.md: {len(ids)} notes under one entry ({line[:40]}...) - an entry point should be selective")

    for nid, files in notes.items():
        if len(notes) > 1 and not incoming[nid] and nid not in index_ids:
            warnings.append(f"{files[0].name}: orphan - no note or index entry leads here")

    # inbox and literature hygiene
    now = time.time()
    stale = [p for p in (root / "inbox").glob("*.md") if (now - p.stat().st_mtime) / 86400 > inbox_days]
    for p in sorted(stale):
        warnings.append(f"inbox/{p.name}: waiting {int((now - p.stat().st_mtime) / 86400)} days - process it or let it go")
    for key, p in sorted(lit.items()):
        if key not in lit_used and (now - p.stat().st_mtime) / 86400 > lit_days:
            warnings.append(f"literature/{p.name}: no permanent note was made from it yet")

    stats = {
        "inbox (fleeting)": len(list((root / "inbox").glob("*.md"))),
        "literature": len(lit),
        "permanent": len(notes),
        "projects": len([d for d in (root / "projects").iterdir() if d.is_dir()]),
        "index entries": len([l for l in index_text.splitlines() if l.startswith("- **")]),
        "avg links per permanent note": round(sum(out_links.values()) / len(out_links), 1) if out_links else 0,
    }
    return errors, warnings, stats


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root")
    p.add_argument("--max-words", type=int, default=250)
    p.add_argument("--inbox-days", type=int, default=3)
    p.add_argument("--lit-days", type=int, default=7)
    p.add_argument("--max-per-topic", type=int, default=7)
    a = p.parse_args()
    root = find_root(a.root)
    errors, warnings, stats = lint(root, a.max_words, a.inbox_days, a.lit_days, a.max_per_topic)
    for title, items in (("ERRORS", errors), ("WARNINGS", warnings)):
        if items:
            print(f"{title} ({len(items)})")
            for i in items:
                print("  -", i)
    print("STATS  " + " | ".join(f"{k}: {v}" for k, v in stats.items()))
    print("OK" if not errors and not warnings else ("FAILED" if errors else "OK with warnings"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
