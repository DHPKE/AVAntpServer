# Task cards - how work is organised here

One task = one file (a "card"). The folder a card sits in is its status, so `ls` is the board and moving a file changes its status.

```
<project>/now/       being worked on
<project>/waiting/   blocked on something outside my control - the card says what (waiting_on:)
<project>/later/     agreed, not started
<project>/done/      finished and verified - history only, not read unless asked
```

## Commands (`python3 _tools/cards.py ...`)
| | |
|---|---|
| `new "Title" --project P --status now --next "..."` | create a card (`waiting` needs `--waiting-on`) |
| `move CARD now\|waiting\|later\|done [--waiting-on "..."]` | change status (moves the file, updates the log) |
| `log CARD "what happened"` | one dated line, newest first |
| `ruling CARD "what was decided"` | record a decision on the card the moment it is made |
| `list [--project P] [--all]` | the board (open cards by default) |
| `check` | every status matches its folder, waiting cards say what they wait on, nothing in `now` is stale |

## Rhythm
**Start of a session:** `list`, read only the `now` and `waiting` cards, then check reality (git status, CI, the files a card names) -
a card is a claim from the past.
**During work:** a request with no card gets a card first (goal + "done when", 3 lines). Update *State* and *Log* at milestones, not
every step. Decisions go on the card under *Rulings* as they are given.
**End of a task or session:** card updated (state, next action, `updated:` date) and in the right folder; say plainly what is
verified and what is not, and what is waiting on whom.

## Writing a good card
- Title = the outcome ("Release v1.2 builds green"), not the activity ("work on release").
- *Done when* is something you can check. *Next* is the very next physical action.
- *State* separates done / verified / **not** verified. Never write "works" for something that was only compiled or unit-tested.
- A `waiting` card names who or what unblocks it, so nobody has to guess.

## Optional: durable facts in gbrain / notes
Cards hold work in progress. Facts that outlive a task (hardware findings, constants, decisions) belong in the project's memory
notes or gbrain, one claim each, with a source and the project name as the entity; the card links to them.
