#!/usr/bin/env python3
"""Create a task-card board in a folder - safe to re-run, never overwrites.

  init_cards.py ~/path/to/cards --project my-project [--project another]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
STATUSES = ("now", "waiting", "later", "done")


def put(src: Path, dest: Path, created: list, kept: list) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        kept.append(dest)
    else:
        shutil.copy2(src, dest)
        created.append(dest)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root")
    p.add_argument("--project", action="append", default=[])
    a = p.parse_args()
    root = Path(a.root).expanduser().resolve()
    created, kept = [], []
    put(SKILL / "assets" / "README-protocol.md", root / "README.md", created, kept)
    put(SKILL / "assets" / "templates" / "card.md", root / "_templates" / "card.md", created, kept)
    put(SKILL / "scripts" / "cards.py", root / "_tools" / "cards.py", created, kept)
    for proj in a.project:
        name = "".join(c if c.isalnum() or c in "-_" else "-" for c in proj.lower()).strip("-") or "project"
        for s in STATUSES:
            d = root / name / s
            if not d.is_dir():
                d.mkdir(parents=True)
                created.append(d)
    print(f"card board at {root}")
    for c in created:
        print("  created", c.relative_to(root))
    for k in kept:
        print("  kept   ", k.relative_to(root), "(already there)")
    try:  # cards are working state, usually not something to commit
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if top.returncode == 0:
            print(f"\nnote: {root} is inside the git repo {top.stdout.strip()} - the cards would be committed.\n"
                  "      Add the folder to .gitignore, or keep the board outside the repo.")
    except FileNotFoundError:
        pass
    print(f"\nnext:  python3 {root}/_tools/cards.py new \"First task\" --project {a.project[0] if a.project else '<project>'} --status now --next \"...\"\n"
          f"board: python3 {root}/_tools/cards.py list")


if __name__ == "__main__":
    sys.exit(main())
