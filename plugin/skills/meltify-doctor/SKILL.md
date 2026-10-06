---
name: meltify-doctor
description: Check which meltify engines, binaries and API keys are available on this machine. Use when a meltify command exits with code 3 or reports a missing engine, binary or key, and before relying on OCR, transcription or LLM engines for an answer.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux.
metadata:
  version: "0.2.1"
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
- `bin soffice` covers PowerPoint 95, formula recalculation, EMF pictures meltify can't draw itself, uncached charts and binary formats like Works or Visio. A copy from `meltify doctor --install libreoffice` shows as `(downloaded)`
- `render quicklook`, on macOS only, draws what LibreOffice can't or isn't installed for. It fails when `render.quicklook` is off
- `bin 7zz` opens encrypted 7z and rar, `lib libarchive` plain 7z and rar, `bin wpd2text` WordPerfect, and `browser chrome` `read --render`
- `--probe` makes one tiny paid call per configured key and reports the HTTP status

## Gotchas

- `--probe` spends one small paid request per configured key. Run it only when the user agrees.
- Exit 3 from doctor means the base install is broken. Exit 3 from any other command means a needed engine, key or binary is missing.

## Then

1. Tell the user which failed checks block the task at hand, and give the hint for each.
2. Don't install anything without the user's go-ahead. Once you have it, run `meltify doctor --install NAME` with the extra the hint names: `office` for Word, PowerPoint, Outlook, EPUB, `.doc`, `.ppt`, `.xls`, `.xlsb`, faster `.xlsx` reading and EMF drawing, `archive` for 7z and rar, `crypto` for encrypted Office, zip, HWP and iWork files, `iwork` for Numbers, `render` for `read --render`, `media` for video URLs, `asr-mlx` or `ocr-paddle` for local engines. The extra goes into a venv in the data dir, which the launcher runs ahead of any other meltify on PATH. `render` also downloads Chromium when Chrome isn't installed, and `archive` also downloads a pinned 7-Zip.
3. For `bin soffice`, `meltify doctor --install libreoffice` downloads a pinned portable LibreOffice of 200 to 300 MB on macOS and Linux. Ask first, since it's large. `lib libarchive` and `bin wpd2text` are system packages, not extras. Give the user the hint instead of installing them yourself.
4. Missing optional engines are fine when another engine covers the task.
