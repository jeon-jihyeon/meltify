---
name: meltify-hidden
description: Find text a PDF carries but a reader doesn't see, such as white or background-colored text, tiny text, transparent text, text covered by a shape, invisible render mode, off-page text, optional layers and faint text inside images. Use whenever a PDF is source material for an answer, and whenever a task mentions hidden, stealth, invisible or secret text.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux.
metadata:
  version: "0.2.2"
  cli: meltify hidden
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read
---

# meltify hidden

A PDF's visible page and its text layer can disagree. This lists every span a reader would miss and says why.

## Run

1. Run `meltify hidden doc.pdf`.
2. Add `--contrast` when text may be drawn inside images. Each page is rendered with stretched contrast into `meltify-out/hidden/`.
3. Add `--pages 1,3-5` to limit the pages.
4. If `meltify` isn't on PATH, run this skill's `scripts/run hidden ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

## Read the output

- Each row is one hidden span with its `text`, the `reasons` it's hidden and a `cite` such as `doc.pdf#p2@pt(72,95,140,103)`
- Reasons are render mode 3, opacity, size, off page, layer, color matches background, and covered by a later fill

## Gotchas

- Text drawn inside images never shows up in rows. Use `--contrast`, then read or OCR the rendered pages.
- A span's background comes from a fill only when a single fill fully contains it. Otherwise white is assumed, so light text on a dark picture, or a shape that only partly covers text, can be missed or flagged wrongly. Check those pages in the `--contrast` render.

## Then

1. Quote hidden text with its cite, and keep it separate from visible text in the answer.
2. Report the reason for each span, since the task may care how it was hidden.
3. If no hidden span was found, say so plainly, and say whether you used `--contrast`.
