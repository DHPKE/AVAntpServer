# The method in detail (Ahrens, building on Luhmann) - in my own words

Use this to explain the system or to settle an edge case. It paraphrases the ideas of *How to Take Smart Notes*
(Sönke Ahrens, 2017); it is not a substitute for reading the book.

## Why it works
- **Writing is how you think, not what you do after thinking.** Making a note forces you to find out whether you understood.
- **Elaboration beats collecting.** An idea restated in your own words and tied to other ideas is remembered and usable; a
  highlighted passage is neither.
- **Small steps, every day.** The workflow breaks "write a paper" into notes you finish in minutes, so the work never stalls.
- **Bottom-up topics.** Interests and questions emerge from the notes instead of being chosen up front; you write about what the
  slip-box already has material for.
- **A trustworthy external memory** lets you stop holding everything in your head - which is what frees attention for thinking.

## The note types
| Type | Purpose | Form | Lifetime |
|---|---|---|---|
| Fleeting | catch a thought before it is gone | anything, in seconds | process within a day or two, then discard |
| Literature | record what a source says that matters *to you* | short, selective, own words, full reference, page numbers | kept (it is the trail back to the source) |
| Permanent | the slip-box proper: one idea, understandable on its own | full sentences, own words, as if written for someone else; states its context | forever |
| Project | everything for one piece of work (question, outline, gaps, draft) | free | ends with the project |
| Index | entry points into the web of notes | few topics, each pointing to 1-3 notes | maintained |

## Atomicity and addresses
- **One idea per note** - small enough to link precisely, big enough to make sense alone. The title states the idea.
- **Fixed addresses** (IDs) let links survive renaming and moving. Luhmann's numbering let a note branch directly behind the note it
  continues; here a timestamp ID (`YYYYMMDDHHMM`) plays the same role and the *links* carry the structure.
- **Links over folders.** There are no topic folders in `permanent/`: a note belongs to many contexts, which categories cannot express.
  A link says *why* two ideas belong together; a folder name does not.

## Connecting - the habit that makes it a slip-box
When filing a permanent note ask: where does this fit? what does it support, contradict, extend, exemplify? which existing note
should now point here? Every note needs at least one outgoing link, and should be reachable from another note or from the index.
The index stays small on purpose: it is a set of good *starting points*, not a table of contents or a keyword list.

## From notes to writing
1. Look for clusters of densely linked notes and for conspicuous gaps - that is your topic and your question.
2. Open a project note; collect the relevant permanent notes in an outline; arrange them as an argument.
3. Write the first draft from the notes (not from the sources). Gaps found while writing become new fleeting notes.
4. Edit, finish, archive the project notes. The permanent notes stay and keep growing.

## Common failure modes (what `lint.py` looks for)
- Collecting instead of thinking: long literature summaries, no permanent notes (stats show it).
- Notes holding several ideas, or titled with a topic word instead of a claim.
- Orphans: notes nothing leads to, so they will never be found again.
- A fleeting-note pile that is never processed.
- An index that turned into an alphabetical keyword list.
