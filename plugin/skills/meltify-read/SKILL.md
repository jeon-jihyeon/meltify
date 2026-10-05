---
name: meltify-read
description: Melt a folder or files of mixed formats, such as PDF, Excel, Word, PowerPoint, mail with attachments, text, images and recordings, into cited markdown, and list which items still need OCR, transcription or a hidden-text check. Use whenever an answer, summary or document is built from such files, even one or two named files, instead of opening them yourself with Python, openpyxl or a PDF library, so every fact can be quoted with its page, cell or attachment.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux. Word, PowerPoint and Outlook files need the office extra.
metadata:
  version: "0.1.0"
  cli: meltify read
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Grep
---

# meltify read

Turn every input into markdown where each block starts with a citation, then work from that markdown.

## Run

1. Run `meltify read <folder or files>`.
2. If `meltify` isn't on PATH, run this skill's `scripts/run read ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.
3. Output lands in `meltify-out/read/`: one `.md` per item, plus `index.jsonl`.

## Read the output

- Every block in the markdown starts with `## <cite>`, such as `report.pdf#p3`, `calendar.xlsx#Calendar`, `notes.txt:201` or `mail.eml#att=cal.xlsx#Calendar`
- Spreadsheets keep row numbers and column letters, so a cell is cited as `calendar.xlsx#Calendar!B7`
- Text files keep their line numbers
- Mail attachments are saved and melted as items of their own
- The `needs` column lists follow-up work:
  - `ocr pages 2,5` or `ocr`: run the meltify-ocr skill on those pages or images
  - `hidden N spans` or `hidden sheets`: run the meltify-hidden skill, or open those sheets
  - `media`: run the meltify-media skill
  - `markitdown`: the office extra is missing. Run `meltify doctor --install office` once the user agrees

## Gotchas

- In the melted markdown, PDF text includes hidden spans as if they were visible, and text placed off the page is dropped. When `needs` says `hidden`, run the meltify-hidden skill before quoting that page.
- An image or a scanned page melts to little or no text. Missing text there means unread, not empty.
- Cites use the path exactly as given on the command line. Pass relative paths when the answer should cite short names.

## Then

1. Search the melted markdown with Grep instead of loading every file into context.
2. Quote each fact with the citation of the block it came from.
3. Finish every item in `needs` before answering from that item, or say which items weren't checked.
