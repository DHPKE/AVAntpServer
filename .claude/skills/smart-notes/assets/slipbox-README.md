# Slip-box

A card system after Sönke Ahrens ("How to Take Smart Notes", built on Luhmann's Zettelkasten). The point is not to store
information but to think on paper: every note you file is an idea you had to understand, put in your own words and connect.

## Where things go
| Folder | What | Lifetime |
|---|---|---|
| `inbox/` | **Fleeting notes** - quick captures, any form | process within a day or two, then discard (`new_note.py processed`) |
| `literature/` | **Literature notes** - one per source, `@citekey.md`, own words, only what matters, with the reference | kept |
| `permanent/` | **Permanent notes** - the slip-box itself. One idea per note, full sentences, own words, written so a stranger could follow. Named `ID-slug.md`, flat (no topic folders) | forever |
| `projects/<name>/` | **Project notes** - question, outline, gaps, draft for one piece of work | thrown away or archived when the project ends |
| `index.md` | **Entry points** - a few topics, each pointing to 1-3 good starting notes | maintained |

## The workflow
1. **Capture** anything that strikes you as a fleeting note - fast, no polish. `new_note.py fleeting "..."`
2. **Read with a pen**: for each source make one literature note - short, selective, in your own words, with page numbers.
3. **Think, then write permanent notes.** Go through fleeting and literature notes and ask: *how does this relate to what
   I already think? what does it support, contradict, extend, or give an example of?* Write the idea as one claim in full
   sentences. `new_note.py permanent "Claim as a sentence" --link ID:why --source @key`
4. **Connect.** A permanent note must link to at least one existing note, and gets linked from where it belongs. Links are
   written both ways by the tool. Prefer a link with a reason ("contradicts...", "example of...") over a bare one.
5. **Entry points.** Add the note to `index.md` only if you would start from it. `new_note.py index "Topic" ID`
6. **Develop topics bottom-up.** Don't pick a topic first: look where notes cluster, where links are dense, where there is a
   gap. Open a project (`new_note.py project NAME`), pull the relevant notes into its outline, and write the draft from them.
7. **Review** now and then: `python3 _tools/lint.py` (orphans, notes that hold several ideas, stale inbox, broken links).

## Rules of thumb
- Own words, always. Copying a quote is not understanding.
- One idea per note; the title states the idea ("Writing is the medium of thinking"), it does not name a topic ("Writing").
- No topic folders and no tag piles - the structure is the links.
- Don't hoard. Ten connected notes beat a hundred excerpts.
- Notes are addressed by ID (`[[202610221530]]`, optionally `[[202610221530|text]]`); literature by `[[@citekey]]`.

## Tools (`_tools/`, Python 3, no dependencies)
`new_note.py fleeting|literature|permanent|project|index|processed ...` and `lint.py`. Run with `--help` for options.
Templates in `_templates/` are yours to edit.
