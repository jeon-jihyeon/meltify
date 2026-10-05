---
name: meltify-ocr
description: Read text in images, screenshots, photos and scanned PDF pages with several OCR engines after upscaling, and list the numbers or words the engines disagree on, with their positions. Use whenever the user asks what an image says or wants text, numbers or a table taken from it, even if the image looks clear, and always when the text is small, blurry, low-contrast, Korean or in another non-Latin script. Never answer from a single reading of an image.
license: MIT
compatibility: Needs uv or meltify on PATH. Apple Vision runs on macOS. PaddleOCR needs the ocr-paddle extra. LLM engines need their API key.
metadata:
  version: "0.1.0"
  cli: meltify ocr
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Write
---

# meltify ocr

A single OCR pass misreads digits and Korean. This reads the same prepared image with several engines and flags every disagreement.

## Run

1. Run `meltify ocr menu.png` or `meltify ocr scan.pdf --pages 2`.
2. By default it uses whichever local engines are installed. Name paid engines only when the user agrees, such as `--engines vision,gemini`.
3. To add your own reading, open the prepared image under `meltify-out/ocr/` with Read, write what you see to a file, one line per line of text, and pass `--reading agent=FILE`. A reading covers exactly one image or page, so pair it with `--pages` for a PDF.
4. Add `--equalize` for faint or low-contrast text, and `--compare tokens` to compare words as well as numbers.
5. If `meltify` isn't on PATH, run this skill's `scripts/run ocr ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

See `references/engines.md` for choosing engines.

## Read the output

- `disputed` rows come first. Each shows the value and how many times each engine read it, with a cite such as `menu.png@px(120,40,380,72)` or `scan.pdf#p2@pt(72,95,140,103)`
- `line` rows are each engine's raw lines, positioned in the original image or in PDF points
- A value counts as agreed only when two or more engines read it the same number of times and at least one of them is local
- A warning tells you when only one engine ran, which means nothing was cross-checked

## Gotchas

- Positions in cites are in original image px or PDF pt. `prepared.png` is upscaled, so never measure positions on it.
- With one engine, every value is `unchecked`. That isn't agreement.
- On recent macOS, Apple Vision applies only the first language. Set `lang` to the language of the text, not the language of the task.

## Then

1. Resolve each disputed value by looking at that region of the prepared image yourself before you use it.
2. If a value stays uncertain, say so in the answer and give its cite instead of picking one.
3. Exit 3 means no engine is available. Use the meltify-doctor skill.
