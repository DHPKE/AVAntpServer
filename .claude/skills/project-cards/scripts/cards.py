#!/usr/bin/env python3
"""Task cards: one file per task, the folder is the status (now / waiting / later / done). Python 3, standard library only.

  cards.py new "Title" --project P [--status later] [--next "..."] [--goal "..."] [--waiting-on "who/what"]
  cards.py move CARD STATUS [--waiting-on "who/what"] [--project P]      (CARD = file name without .md, or P/name)
  cards.py log CARD "what happened"            adds a dated line, newest first
  cards.py ruling CARD "what the user decided" records a decision on the card
  cards.py list [--project P] [--all]          the board (default: now + waiting; --all adds later + done)
  cards.py check [--stale-days 14]             status == folder, waiting has waiting_on, nothing stale; exit 1 on errors
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

STATUSES = ("now", "waiting", "later", "done")
FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def today() -> str:
    return dt.date.today().isoformat()


def find_root(arg: str | None) -> Path:
    if arg:
        root = Path(arg).expanduser().resolve()
    else:
        here = Path(__file__).resolve().parent
        if here.name != "_tools":
            sys.exit("Run this from <cards>/_tools, or pass --root <cards folder>.")
        root = here.parent
    if not root.is_dir():
        sys.exit(f"{root} does not exist. Create it with init_cards.py.")
    return root


def slug(text: str, limit: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "card"


def projects(root: Path) -> list[Path]:
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(("_", ".")) and any((d / s).is_dir() for s in STATUSES))


def all_cards(root: Path, project: str | None = None):
    for proj in projects(root):
        if project and proj.name != project:
            continue
        for st in STATUSES:
            for f in sorted((proj / st).glob("*.md")) if (proj / st).is_dir() else []:
                yield proj.name, st, f


def parse_front(text: str) -> dict[str, str]:
    m = FRONT.match(text)
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "#")):
                k, _, v = line.partition(":")
                out[k.strip()] = re.sub(r"\s+#.*$", "", v).strip() if k.strip() == "status" else v.strip()
    return out


def set_front(text: str, key: str, value: str | None) -> str:
    """Set/replace/remove (value None) one frontmatter line; keep the others untouched."""
    m = FRONT.match(text)
    if not m:
        sys.exit("card has no frontmatter")
    lines = m.group(1).splitlines()
    idx = next((i for i, l in enumerate(lines) if re.match(rf"{re.escape(key)}\s*:", l)), None)
    if value is None:
        if idx is not None:
            del lines[idx]
    elif idx is not None:
        lines[idx] = f"{key}: {value}"
    else:
        pos = next((i for i, l in enumerate(lines) if l.startswith("next:")), len(lines))
        lines.insert(pos, f"{key}: {value}")
    return "---\n" + "\n".join(lines) + "\n---\n" + text[m.end():]


def find_card(root: Path, name: str, project: str | None):
    if "/" in name and not project:
        project, name = name.split("/", 1)
    name = name.removesuffix(".md")
    hits = [(p, s, f) for p, s, f in all_cards(root, project) if f.stem == name]
    if not hits:
        sys.exit(f"no card named {name!r}" + (f" in project {project}" if project else ""))
    if len(hits) > 1:
        sys.exit(f"{name!r} exists in several places ({', '.join(f'{p}/{s}' for p, s, _ in hits)}); add --project")
    return hits[0]


def add_under(text: str, heading: str, line: str) -> str:
    """Insert a line right below '## <heading> ...' (newest first)."""
    pat = re.compile(rf"^(## {heading}\b.*)$", re.M)
    if pat.search(text):
        return pat.sub(lambda m: m.group(1) + "\n" + line, text, count=1)
    return text.rstrip("\n") + f"\n\n## {heading}\n{line}\n"


def cmd_new(root: Path, a) -> None:
    status = a.status
    if status == "waiting" and not a.waiting_on:
        sys.exit("a waiting card must say what it waits on: --waiting-on \"who/what\"")
    proj = root / slug(a.project)
    for s in STATUSES:
        (proj / s).mkdir(parents=True, exist_ok=True)
    path = proj / status / f"{slug(a.title)}.md"
    if any(f.stem == path.stem for _, _, f in all_cards(root, proj.name)):
        sys.exit(f"a card named {path.stem!r} already exists in {proj.name}")
    tpl_file = root / "_templates" / "card.md"
    from_tpl = tpl_file.read_text() if tpl_file.exists() else DEFAULT_CARD
    wait_line = f"waiting_on: {a.waiting_on}\n" if status == "waiting" else ""
    text = (from_tpl.replace("{{title}}", a.title).replace("{{project}}", proj.name).replace("{{status}}", status)
            .replace("{{waiting_on_line}}", wait_line).replace("{{next}}", a.next or "").replace("{{today}}", today())
            .replace("{{goal}}", a.goal or ""))
    path.write_text(text)
    print(path)


def cmd_move(root: Path, a) -> None:
    proj, old, f = find_card(root, a.card, a.project)
    if a.status == "waiting" and not (a.waiting_on or parse_front(f.read_text()).get("waiting_on")):
        sys.exit("moving to waiting needs --waiting-on \"who/what\"")
    text = f.read_text()
    text = set_front(text, "status", a.status)
    text = set_front(text, "waiting_on", a.waiting_on if a.status == "waiting" and a.waiting_on else (parse_front(text).get("waiting_on") if a.status == "waiting" else None))
    text = set_front(text, "updated", today())
    text = add_under(text, "Log", f"- {today()} moved {old} -> {a.status}" + (f" (waiting on {a.waiting_on})" if a.waiting_on and a.status == "waiting" else ""))
    dest = root / proj / a.status / f.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text)
    if dest != f:
        f.unlink()
    print(dest)


def cmd_log(root: Path, a) -> None:
    _, _, f = find_card(root, a.card, a.project)
    text = add_under(f.read_text(), "Log", f"- {today()} {a.text}")
    f.write_text(set_front(text, "updated", today()))
    print(f"logged on {f.name}")


def cmd_ruling(root: Path, a) -> None:
    _, _, f = find_card(root, a.card, a.project)
    text = add_under(f.read_text(), "Rulings", f"- {today()} {a.text}")
    f.write_text(set_front(text, "updated", today()))
    print(f"ruling recorded on {f.name}")


def cmd_list(root: Path, a) -> None:
    shown = STATUSES if a.all else ("now", "waiting")
    any_card = False
    for proj in projects(root):
        if a.project and proj.name != a.project:
            continue
        print(f"== {proj.name}")
        for st in shown:
            cards = list((proj / st).glob("*.md")) if (proj / st).is_dir() else []
            if not cards and st in ("later", "done"):
                continue
            print(f"  [{st}] ({len(cards)})")
            for f in sorted(cards):
                fm = parse_front(f.read_text())
                extra = ""
                if st == "waiting":
                    extra = f"  <- waiting on: {fm.get('waiting_on', '?')}"
                print(f"    - {f.stem}: {fm.get('card', f.stem)}{extra}")
                if st in ("now", "waiting") and fm.get("next"):
                    print(f"        next: {fm['next']}")
                any_card = True
    if not any_card:
        print("(no open cards)")


def cmd_check(root: Path, a) -> int:
    errors, warnings = [], []
    for proj, st, f in all_cards(root):
        fm = parse_front(f.read_text())
        rel = f"{proj}/{st}/{f.name}"
        if fm.get("status") != st:
            errors.append(f"{rel}: status is {fm.get('status')!r} but the card is in {st}/ (move it or fix the status)")
        if fm.get("project") not in (None, proj):
            errors.append(f"{rel}: project is {fm.get('project')!r} but the card is under {proj}/")
        if st == "waiting" and not fm.get("waiting_on"):
            errors.append(f"{rel}: waiting card without waiting_on")
        if st != "waiting" and fm.get("waiting_on"):
            warnings.append(f"{rel}: has waiting_on but is not in waiting/")
        if st in ("now", "waiting", "later") and not fm.get("next"):
            warnings.append(f"{rel}: no next action")
        try:
            age = (dt.date.today() - dt.date.fromisoformat(fm.get("updated", ""))).days
        except ValueError:
            warnings.append(f"{rel}: missing or invalid updated date")
            continue
        if st == "now" and age > a.stale_days:
            warnings.append(f"{rel}: in now/ but untouched for {age} days - finish it, park it in waiting/later, or log progress")
    for title, items in (("ERRORS", errors), ("WARNINGS", warnings)):
        if items:
            print(f"{title} ({len(items)})")
            for i in items:
                print("  -", i)
    counts = {s: sum(1 for _, st, _ in all_cards(root) if st == s) for s in STATUSES}
    print("BOARD  " + " | ".join(f"{k}: {v}" for k, v in counts.items()))
    print("OK" if not errors and not warnings else ("FAILED" if errors else "OK with warnings"))
    return 1 if errors else 0


DEFAULT_CARD = ("---\ncard: {{title}}\nproject: {{project}}\nstatus: {{status}}\n{{waiting_on_line}}next: {{next}}\nupdated: {{today}}\n---\n"
                "## Goal / done when\n{{goal}}\n\n## State (done, verified, NOT verified)\n\n## Rulings (user decisions on this card)\n\n"
                "## Log (newest first, 1 line each)\n- {{today}} created\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="the cards folder (default: the folder containing _tools)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new"); s.add_argument("title"); s.add_argument("--project", required=True)
    s.add_argument("--status", choices=STATUSES, default="later"); s.add_argument("--next"); s.add_argument("--goal"); s.add_argument("--waiting-on")
    s = sub.add_parser("move"); s.add_argument("card"); s.add_argument("status", choices=STATUSES); s.add_argument("--waiting-on"); s.add_argument("--project")
    s = sub.add_parser("log"); s.add_argument("card"); s.add_argument("text"); s.add_argument("--project")
    s = sub.add_parser("ruling"); s.add_argument("card"); s.add_argument("text"); s.add_argument("--project")
    s = sub.add_parser("list"); s.add_argument("--project"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("check"); s.add_argument("--stale-days", type=int, default=14)
    a = p.parse_args()
    root = find_root(a.root)
    fn = {"new": cmd_new, "move": cmd_move, "log": cmd_log, "ruling": cmd_ruling, "list": cmd_list, "check": cmd_check}[a.cmd]
    return fn(root, a) or 0


if __name__ == "__main__":
    sys.exit(main())
