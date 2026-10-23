---
name: smart-notes
description: "Builds and runs a slip-box (Zettelkasten) card system after Sönke Ahrens' 'How to Take Smart Notes' and Luhmann's method: scaffolds the folders, templates and small tools (fleeting notes in an inbox, literature notes, permanent notes with IDs and links, an index of entry points, project notes) and coaches capturing, processing, linking and reviewing notes. Use when the user wants to set up a Zettelkasten / slip-box / note-card system, take smart notes from reading or research, turn fleeting or literature notes into permanent notes, find where their notes connect, or review the health of their notes. Also use for 'card system for thinking/writing/studying'. NOT for task or project tracking (use project-cards for that)."
---

# Smart notes (slip-box after Ahrens)

A slip-box is not an archive. Its value comes from the thinking that filing a note forces: understanding an idea, putting it
in your own words, and deciding how it connects to what you already think. Everything here serves that.

**Your role:** coach and tool-runner. The user must do the thinking - don't write their permanent notes for them unless they
ask, and when you do, use *their* wording from their fleeting/literature notes and never add claims they didn't make.
Ask the connecting questions; let the tools enforce the rules.

## Determine the mode first
| User wants | Mode |
|---|---|
| "set up a Zettelkasten / card system / slip-box" | **Set up** |
| "note this", "remember this thought" | **Capture** |
| "I read X, here are my notes" | **Literature** |
| "process my inbox / turn these into permanent notes" | **Process** |
| "where does this fit", "add to the index" | **Connect & index** |
| "what should I write about", "start a project/article" | **Develop & write** |
| "check my notes", "how healthy is my slip-box" | **Review** |
| "how does the method work" | **Explain** (use `references/ahrens-method.md`) |

## Set up
1. Ask where it should live if not stated (suggest `~/zettelkasten`; one slip-box per person, not per project - ideas cross projects).
2. Run: `python3 ~/.claude/skills/smart-notes/scripts/init_slipbox.py <folder>` - safe to re-run, never overwrites.
3. It creates `inbox/ literature/ permanent/ projects/ index.md README.md _templates/ _tools/`. The tools are copied into
   `_tools/` so the slip-box works without this skill. Show the user the README's workflow in 3 lines, not the whole file.
4. Offer to capture a first fleeting note right away so the habit starts.

## The tools (from the slip-box folder: `python3 _tools/new_note.py ...`)
- `fleeting "text"` - into `inbox/`.
- `literature CITEKEY --title T --ref "Author (Year). Title."` - `literature/@citekey.md`.
- `permanent "Claim as a full sentence" --link ID:why [--link ...] --source @citekey --body "..."` - needs at least one `--link`
  (the first note is exempt); writes the link both ways. Prints `[[ID]]`.
- `index "Topic" ID [ID ...]` - entry points; `project NAME --question "..."`; `processed inbox/<file>` - retires a handled fleeting note.
- `lint.py` - review (exit 1 on errors). From outside the folder add `--root <slip-box>` to either script.

## Capture
Make it frictionless: one `fleeting` call per thought, no polishing. Never turn a capture into a permanent note on the spot.

## Literature
One note per source, short and selective: only what bears on the user's questions, in their own words, with page numbers.
Fill "Ideas to develop" - those become permanent notes in the next step. A long summary is a sign they're collecting, not thinking.

## Process (the core habit - do it within a day or two of capture)
For each fleeting/literature note, ask the user, one at a time:
1. What is the single idea here? Can you say it as one sentence a stranger would understand?
2. How does it relate to what you already think? Does it support, contradict, extend, or give an example of an existing note?
   (Search `permanent/` for the words; show the 2-3 closest notes.)
3. Where would you look for it in a year? (→ index entry only if the answer is "I'd start here".)
Then create the permanent note with `--link ID:why`. If it connects to nothing, say so: it is either a new branch (fine, but link
the nearest note anyway) or not yet understood. Finally mark the fleeting note `processed`.
One idea per note: if the user's text holds two claims, make two notes and link them.

## Connect & index
Prefer a link with a reason ("contradicts...", "is an example of...") to a bare link. Keep `index.md` short: one entry per topic,
1-3 notes each. A growing keyword list is the wrong direction - the structure comes from links, not categories or topic folders.

## Develop & write
Work bottom-up: don't pick a topic first. Look where notes cluster and where links are dense (`grep -c '\[\[' permanent/*.md`,
or follow the index), name the gaps, then open a `project` with the question and done-condition, pull the relevant notes into its
outline, and write the draft from them. Writing is the medium of thinking: gaps found while writing become new fleeting notes.

## Review
Run `lint.py`; report in the user's terms: errors first (broken links, notes linked to nothing, duplicate ids), then warnings
(orphans, notes that hold several ideas, stale inbox, literature nothing was made from, bloated index entries) and the stats
line. Suggest the one or two fixes with the most value; don't dump the list.

## Guardrails
- Don't copy-paste quotes or whole summaries into permanent notes; don't tag-dump; don't create topic folders under `permanent/`.
- Don't delete notes: `processed` moves fleeting notes to `inbox/.processed/`; permanent notes are edited, not discarded.
- Keep the user's slip-box private: don't send note contents to gbrain or any service unless they ask.
- This system is for ideas. For tasks use the `project-cards` skill; the two can share a project but never share files.

References: `references/ahrens-method.md` (the method in detail, for explaining or deciding edge cases),
`assets/templates/` (note templates), `scripts/test_slipbox.py` (run it after editing the tools).
