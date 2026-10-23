---
name: project-cards
description: "Sets up and runs a task-card board as plain files for any project: one markdown card per task, the folder is the status (now / waiting / later / done), with a tiny CLI to create, move, log and check cards, and a start-of-session / end-of-session routine. Use when the user wants a card system, kanban-in-folders, or a way to keep track of open work, waiting-on items and decisions across sessions in a project, or says 'set up cards for this project', 'what is open / waiting', 'make a card for this', or 'check the board'. NOT for notes about ideas or reading (use smart-notes for that)."
---

# Project cards (task board as files)

A card is one task in one file; the folder it sits in is its status. It makes open work, blocked work and decisions
visible in a 2-minute read at the start of a session, instead of being re-derived from chat or git history.

## Determine the mode
| User wants | Mode |
|---|---|
| "set up cards for this project" | **Set up** |
| "what's open / waiting / next?" | **Read the board** |
| "make a card for X", a new request that has none | **New card** |
| progress, a decision, a blocker | **Update** |
| "check the board" | **Check** |

## Set up
1. Decide the location with the user. Default suggestion: **outside the git repo** (cards are working state, not source).
   Examples: `~/.claude/projects/<project-dir>/cards/` or a sibling folder. If it must live in the repo, add it to `.gitignore`
   (the init script warns when the folder is inside a repo).
2. `python3 ~/.claude/skills/project-cards/scripts/init_cards.py <folder> --project <name> [--project <other>]`
   Safe to re-run; never overwrites. It creates `README.md` (the rhythm), `_templates/card.md`, `_tools/cards.py` (copied, so the
   board works without this skill) and `<project>/{now,waiting,later,done}/`.
3. Seed the board from what you already know is open - but only what you can state truthfully. Don't invent state: a card
   with an unknown next action says so.
4. Tell the user the three commands they will use: `list`, `new`, `move`.

## Read the board (start of every session)
`python3 <folder>/_tools/cards.py list` (add `--all` only if asked about later/done). Read the `now` and `waiting` cards; **never read
`done/` unless asked**. Then verify reality before acting on a card (git status, CI, the files it names) - and say if a card was stale.

## New card
Create the card *before* starting a request that has none: `cards.py new "Outcome as title" --project P --status now --next "..." --goal "done when ..."`.
Waiting cards need `--waiting-on "who/what"`. One card = one outcome that can be finished; split if it holds two.

## Update
- Milestones only: `cards.py log CARD "..."` (a dated line). Fill *State* by editing the file: done / verified / NOT verified.
- The moment the user decides something about a card: `cards.py ruling CARD "..."`.
- Status changes: `cards.py move CARD waiting --waiting-on "user: needs the board on Saturday"`; `... done` only when verified.
- End of a task: card current, in the right folder, then tell the user in 3-5 lines what is verified vs not and who is waited on.

## Check
`cards.py check` - errors: status differs from folder, waiting without `waiting_on`, project mismatch; warnings: no next action,
stale `now` cards (default 14 days), missing dates. Report in plain words and fix what's cheap.

## Guardrails
- A card never replaces a commit message or a decision record; it points to them.
- Don't move a card to `done` on "it compiles" - only on what was actually verified; otherwise leave it in `now`/`waiting` with the gap named.
- Don't touch other people's cards or files in the folder; use the CLI so frontmatter stays consistent.
- Optional with gbrain: durable facts that outlive a card go to gbrain/notes (one claim, project name as entity, a source); the card links to them.

Reference: `assets/README-protocol.md` is copied into every board as its README. After editing the tool run `scripts/test_cards.py`.
