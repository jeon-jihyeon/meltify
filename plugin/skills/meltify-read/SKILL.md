---
name: meltify-read
description: Melt files, folders and URLs of mixed formats, such as PDF, Excel, Word, PowerPoint, HWP, mail with attachments, KakaoTalk and Slack exports, zip and other archives, web pages, text, images and recordings, into cited markdown, with images, scans and recordings read by local OCR and speech engines, and list which items still need OCR, transcription or a hidden-text check. Use whenever an answer, summary or document is built from such files or pages, even one or two named files or a single URL, instead of opening them yourself with Python, openpyxl, a PDF library, unzip or curl, so every fact can be quoted with its page, cell, line, attachment or timestamp.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux. Word, PowerPoint and Outlook files need the office extra, 7z and rar the archive extra, .ppt needs LibreOffice.
metadata:
  version: "0.2.0"
  cli: meltify read
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Grep
---

# meltify read

Turn every input into markdown where each block starts with a citation, then work from that markdown.

## Run

1. Run `meltify read <folders, files or URLs>`. Images, scanned pages, pictures inside documents and recordings are read in the same pass by local engines.
2. Add `--shallow` when you only need the text layer fast. It skips only OCR and speech recognition and lists those items in `needs`. Everything else melts as usual.
3. Raise `--budget SEC` (600 by default) for a folder with many images or long recordings. Rerunning the same command also works, since finished work is cached.
4. For a page that comes out nearly empty because it's built by JavaScript, add `--render`. Add `--whole` to keep the full page instead of just the article.
5. If `meltify` isn't on PATH, run this skill's `scripts/run read ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.
6. Output lands in `meltify-out/read/`: one `.md` per item, plus `index.jsonl`.

## Read the output

- Every block in the markdown starts with `## <cite>`. Some examples:
  - `report.pdf#p3`, `calendar.xlsx#Calendar!B7`, `notes.txt:201`
  - `mail.eml#att=cal.xlsx#Calendar` and `a.zip#att=docs/b.pdf#p3`: each `#att=` is one level of attachment or archive member
  - `memo.docx#para3#img1`: the first picture in paragraph 3, read by OCR
  - `a.hwp#s1:12`: section 1, line 12 of an HWP file
  - `https://example.com/guide#install:28`: line 28 under the heading with that anchor
  - `call.m4a@00:01:23.4-00:01:27.0`: a transcript span
- OCR lines start with their position, such as `@px(120,40,380,72)| 48,250` or `@pt(72,95,140,103)| ...`, so a value can be cited down to its box
- A block whose number two engines read differently ends with `> disputed 765: vision 0, paddle 1`. One with only one engine ends with `> unchecked: one engine`
- Attachments, archive members and downloaded files are saved and melted as items of their own
- The `needs` column lists follow-up work:
  - `ocr pages 2,5`, `ocr` or `media` alone: no local engine was available. Use the meltify-doctor skill
  - `ocr (budget)` or `media (budget)`: the time budget ran out. Rerun with a higher `--budget`
  - `ocr (failed)`: the engines couldn't read it. Run the meltify-ocr skill on that item
  - `hidden N spans` or `hidden sheets`: run the meltify-hidden skill, or open those sheets
  - `render`: the page looks script-built. Rerun with `--render`
  - `markitdown`, `archive extra`, `iwork extra` or `ppt (install LibreOffice)`: a part is missing. Install it once the user agrees
  - `... not read`, `encrypted` or `preview only`: that part of the item wasn't melted. Say so if it matters

## Gotchas

- In the melted markdown, PDF text includes hidden spans as if they were visible, and text placed off the page is dropped. When `needs` says `hidden`, run the meltify-hidden skill before quoting that page.
- A disputed or unchecked value isn't confirmed. Look at the image yourself or run the meltify-ocr skill on it before you rely on it.
- Linux has no Apple Vision, so with PaddleOCR alone every reading ends with `unchecked`.
- With `--shallow`, or when the budget runs out, an image or a scanned page melts to little or no text. Missing text there means unread, not empty.
- `.ppt` needs LibreOffice and `--render` needs the render extra. Without them the row says so in `needs` or `error`.
- URLs to private addresses, pages that `robots.txt` disallows and downloads over 20 MB are refused. Don't add `--allow-private` or `--ignore-robots` unless the user asks.
- Cites use the path exactly as given on the command line. Pass relative paths when the answer should cite short names.

## Then

1. Search the melted markdown with Grep instead of loading every file into context.
2. Quote each fact with the citation of the block or OCR line it came from.
3. Finish every item in `needs` and check every disputed value before answering from that item, or say which items weren't checked.
