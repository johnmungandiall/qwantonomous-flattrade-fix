# AGENT.md

Loaded every session. Keep it short — it points the agent at the knowledge
base in `kb/` instead of making it re-scan the whole repo.

## How to work here
1. Read the `kb/` notes first to get your bearings — they're an index into the
   code. Follow their `path#symbol` (or `path:line`) pointers to read the real source files.
2. When you change code, update the affected `kb/` note in the SAME turn.
3. Record user preferences in `kb/about-you.md`; durable decisions go to memory.
4. Save reusable how-tos as skills after finishing something non-trivial.
5. Fresh template notes in `kb/` mean the KB is unbuilt — NOT that the project
   is empty. Check the real files (repo_map / list) before describing the project.

## Knowledge base rules (keep notes cheap and trustworthy)
- One note per concern; there is no line cap. Use headings for long notes and fetch bodies on demand.
- One fact lives in ONE place; cross-link related notes with `[[other-note]]`.
- Deep-dives can live in subfolders — write them as `features/<name>` with
  kb_write and they land in `kb/features/<name>.md`.
- 🔴 NEVER cite a code LINE NUMBER in a note. Always `path#symbol` from the repo
  root (e.g. `kbcode/agent.py#Agent.run`) — the reader SEARCHES for the name, so
  `/kb-check` resolves the live line itself and a moved function needs NO note
  edit. `path:line` rots the moment anyone edits the file above it and is what
  makes KB upkeep expensive. (User rule, stated twice: *"never use code line
  numbers in a kb note again — use the searchable function name etc. instead"*.) For a spot with no name of its own, point at the ENCLOSING
  symbol and name the thing in prose ("the `_PROFILE_PORT_BASE` constant in
  `kbcode/browser_bridge.py#_profile_port`"), never `file.py:412`.
- Start each note with a one-line summary of what it covers.
- Release history lives only in a changelog note, never duplicated.

## Notes map
- `kb/overview.md` — what this project is and how to run it.
- `kb/architecture.md` — the main pieces and how they fit.
- `kb/conventions.md` — how code/notes here are structured.
- `kb/gotchas.md` — traps to know before editing.
- `kb/glossary.md` — project-specific terms.
- `kb/cheatsheet.md` — the commands/snippets you reach for most.
- `kb/changelog.md` — notable changes, newest first (the only place history lives).
- `kb/about-you.md` — the USER: style, tech, goals, rules.
