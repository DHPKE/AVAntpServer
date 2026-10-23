#!/usr/bin/env python3
"""Create a slip-box (Zettelkasten after Sönke Ahrens) in a folder - safe to re-run, never overwrites.

  init_slipbox.py ~/zettelkasten
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
FOLDERS = ["inbox", "literature", "permanent", "projects", "_templates", "_tools"]
INDEX = ("# Index - entry points\n\nFew entries, each pointing to one to three notes that are good places to start\n"
         "thinking about a topic. This is not a keyword list: a note belongs here only if you would start from it.\n\n"
         "## Entry points\n")


def put(src: Path, dest: Path, created: list, kept: list) -> None:
    if dest.exists():
        kept.append(dest)
    else:
        shutil.copy2(src, dest)
        created.append(dest)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root", help="where the slip-box should live, e.g. ~/zettelkasten")
    root = Path(p.parse_args().root).expanduser().resolve()
    created, kept = [], []
    for f in FOLDERS:
        (root / f).mkdir(parents=True, exist_ok=True)
    for tpl in sorted((SKILL / "assets" / "templates").glob("*.md")):
        put(tpl, root / "_templates" / tpl.name, created, kept)
    for tool in ("slipbox.py", "new_note.py", "lint.py"):
        put(SKILL / "scripts" / tool, root / "_tools" / tool, created, kept)
    put(SKILL / "assets" / "slipbox-README.md", root / "README.md", created, kept)
    if not (root / "index.md").exists():
        (root / "index.md").write_text(INDEX)
        created.append(root / "index.md")
    for d in ("inbox",):
        keep = root / d / ".gitkeep"
        if not keep.exists():
            keep.touch()
    print(f"slip-box at {root}")
    for c in created:
        print("  created", c.relative_to(root))
    for k in kept:
        print("  kept   ", k.relative_to(root), "(already there)")
    print(f"\nnext:  python3 {root}/_tools/new_note.py fleeting \"your first thought\"\n"
          f"check: python3 {root}/_tools/lint.py")


if __name__ == "__main__":
    sys.exit(main())
