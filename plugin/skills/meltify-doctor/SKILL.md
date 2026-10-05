---
name: meltify-doctor
description: Check which meltify engines, binaries and API keys are available on this machine. Use when a meltify command exits with code 3 or reports a missing engine, binary or key, and before relying on OCR, transcription or LLM engines for an answer.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux.
metadata:
  version: "0.1.0"
  cli: meltify doctor
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read
---

# meltify doctor

Report what meltify can use on this machine and how to fix what's missing.

## Run

1. Run `meltify doctor`.
2. If `meltify` isn't on PATH, run this skill's `scripts/run doctor` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

## Read the output

- Each row is one check with `ok`, a `detail`, and the commands that need it in `used_by`
- A failed row carries a `hint` with the install command
- API keys are only reported as set or unset. Never print or ask for their values
- `ocr *` and `asr *` rows say which engines are ready right now
- `--probe` makes one tiny paid call per configured key and reports the HTTP status

## Gotchas

- `--probe` spends one small paid request per configured key. Run it only when the user agrees.
- Exit 3 from doctor means the base install is broken. Exit 3 from any other command means a needed engine, key or binary is missing.

## Then

1. Tell the user which failed checks block the task at hand, and give the hint for each.
2. Don't install anything without the user's go-ahead. Once you have it, run `meltify doctor --install office`, `media`, `asr-mlx` or `ocr-paddle`. The extra goes into a venv in the data dir, which the launcher runs ahead of any other meltify on PATH.
3. Missing optional engines are fine when another engine covers the task.
