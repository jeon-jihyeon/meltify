---
name: meltify-brief
description: Quote every condition sentence of a written problem statement with its line number, such as counts, streaks, dates, time zones, output formats, priority rules, submission limits and prohibitions, and keep a board when working on several problems at once. Use at the start of any task that comes with a written statement or rules, before planning a solution.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux.
metadata:
  version: "0.2.4"
  cli: meltify brief
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Write Edit
---

# meltify brief

Most wrong answers come from a misread condition, like reading "10 wins in a row" as one win. This pulls those sentences out verbatim so you can check them one by one.

## Run

1. Save the statement as a text or markdown file, or melt it first with the meltify-read skill.
2. Run `meltify brief extract statement.md`.
3. For several problems, run `meltify brief new 4 "Parking" --from statement.md` once per problem, then `meltify brief board`.
4. Update progress with `meltify brief set 4 --state doing --points 30`.
5. If `meltify` isn't on PATH, run this skill's `scripts/run brief ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

## Read the output

- Each row quotes one condition sentence verbatim, as the whole line it sits on, with its `kinds` and a cite such as `statement.md:4`
- Kinds are count, date, timezone, format, priority, limit and prohibition
- `new` writes `problems/04-Parking/NOTES.md`, with the statement, the conditions as unchecked items and a checklist, plus `status.json`
- `board` shows each problem's state, points, checked items and minutes since it was last touched

## Gotchas

- Only sentences with a recognized kind of condition are listed. A condition worded without those keywords won't show up, so still read the whole statement once.
- Rows split on line breaks, not sentence ends. A sentence that wraps onto the next line is only partly quoted, so open the cited line and its neighbors.

## Then

1. Restate each condition in your plan using the quoted words, never a paraphrase that changes a number.
2. Tick a condition in NOTES.md only after the answer has been checked against it, for example with the meltify-check skill.
3. Before submitting, reread every row of the extract once more.
4. On the board, start with the problems that have the longest feedback loops.
