---
name: meltify-check
description: Check an answer file or a single string against its stated output format before giving or submitting it. Use whenever a task states a format, such as a JSON schema, an item count, unique ids, uppercase only, ASCII digits only, an exact or maximum word count or required phrases, and whenever the user asks whether an answer is valid, ready or fine to submit. Run it instead of judging the format by eye or with ad hoc scripts, because a mechanical check catches the case, width and count slips a reread misses.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux.
metadata:
  version: "0.2.2"
  cli: meltify check
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Write
---

# meltify check

Catch format mistakes mechanically instead of rereading the answer by eye.

## Run

1. Write the answer to a file such as `answers.json`, or keep it as one string.
2. Run one of these:
   - `meltify check answers.json --schema answer.schema.json --count 30 --unique id`
   - `meltify check answers.json --field '$.q2' --upper --field '$.q6' --digits`
   - `meltify check --text "ALPHA" --pattern '[A-Z]+'`
3. If `meltify` isn't on PATH, run this skill's `scripts/run check ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

Rules that repeat across runs belong in `meltify.toml`. See `references/rules.md`.

## Read the output

- Exit 0 prints `no violations`
- Exit 1 lists each violation with its `rule`, `message`, `value` and a `cite` such as `answers.json#$[3].reason`
- Hygiene rules run on every string by default: leading or trailing whitespace, double spaces, and fullwidth characters such as `１２`
- Exit 2 is a usage error. Text rules on a file need `--field`

## Gotchas

- `--text` ignores the rules in `meltify.toml` and uses only its own flags. `--schema`, `--count`, `--unique` and `--field` apply to files only, so combining them with `--text` is a usage error.
- `\d` in `--pattern` also matches fullwidth digits. Use `[0-9]` or `--digits`, and keep hygiene on.
- Paths support only `$`, `.key`, `[n]`, `[-1]` and `[*]`. Filters such as `[?(...)]` are a usage error.

## Then

1. Fix every violation in the answer itself, and rerun the check until it exits 0.
2. Never loosen a rule to make it pass. If a rule looks wrong, quote the task's wording to the user.
3. Turn the task's format sentence into flags before you write the answer, not after.
