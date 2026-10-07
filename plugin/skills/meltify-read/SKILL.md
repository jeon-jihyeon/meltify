---
name: meltify-read
description: Melt files, folders and URLs of mixed formats, such as PDF, Excel, Word, PowerPoint, HWP, mail with attachments, KakaoTalk and Slack exports, zip and other archives, web pages, text, images and recordings, into cited markdown, with images, scans and recordings read by local OCR and speech engines, and list which items still need OCR, transcription or a hidden-text check. Use whenever an answer, summary or document is built from such files or pages, even one or two named files or a single URL, instead of opening them yourself with Python, openpyxl, a PDF library, unzip or curl, so every fact can be quoted with its page, cell, line, attachment or timestamp.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux. Word, PowerPoint, Outlook and EPUB files, plus .xls, .xlsb, .doc and .ppt, need the office extra, 7z and rar the archive extra, Parquet the parquet extra, encrypted files the crypto extra. PowerPoint 95 and other binary formats need LibreOffice or, on macOS, Quick Look.
metadata:
  version: "0.2.4"
  cli: meltify read
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Grep
---

# meltify read

Turn every input into markdown where each block starts with a citation, then work from that markdown.

## Run

1. Run `meltify read <folders, files or URLs>`. Images, scanned pages, pictures inside documents and recordings are read in the same pass by local engines.
2. Add `--shallow` when you only need the text layer fast. It skips OCR, speech, and drawing or recalculation work, and lists what it skipped in `needs`. Native text melts as usual.
3. Raise `--budget SEC` (600 by default) for a folder with many images or long recordings. Rerunning the same command also works, since finished work is cached.
4. When the text in images or scans isn't Korean, add `--lang` with its code: `ko`, `en`, `ja`, `zh`, `de`, `fr` or `es`. OCR reads Korean by default, and the wrong language drops accents and garbles other scripts.
5. For a page that comes out nearly empty because it's built by JavaScript, add `--render`. Add `--whole` to keep the full page instead of just the article.
6. For encrypted files:
   - Ask the user for the password, and have them write it to a file only they can read
   - Add `--password-file PATH`, or have them set `MELTIFY_PASSWORD`
   - Never pass `--password`, which other users can see, and never repeat the password in your answer
7. If `meltify` isn't on PATH, run this skill's `scripts/run read ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.
8. Output lands in `meltify-out/read/`: one `.md` per item, plus `index.jsonl`. A rerun into the same folder removes the previous markdown of items this run didn't name at all. An item that failed this run keeps its previous markdown, so check its row for an `error` before you quote that file.

## Read the output

Every block in the markdown starts with `## <cite>`. Quote that cite, or narrow it down to a line or cell yourself:

- Text files melt in blocks of 200 lines, headed `## notes.txt:1`, `## notes.txt:201` and so on
  - Each line starts with its number, like `201| `, so cite that line as `notes.txt:201`
- Each sheet is one block headed like `## calendar.xlsx#Calendar`, holding a table
  - The `row` column holds the sheet's row numbers and the other headers are its column letters, so the value in row 7 under `B` is `calendar.xlsx#Calendar!B7`
  - Hidden sheets melt too, as blocks of their own like `## calendar.xlsx#Old`. If you quote one, say it comes from a hidden sheet
- Other cites look like this:
  - `report.pdf#p3`: page 3 of a PDF
  - `mail.eml#att=cal.xlsx#Calendar` and `a.zip#att=docs/b.pdf#p3`: each `#att=` is one level of attachment or archive member
  - `memo.docx:3`: paragraph 3 of a Word, ODT or Pages document, the number its line starts with
  - `memo.docx#para3#img1`: the first picture in paragraph 3 of a Word document, `.doc` included, read by OCR. A chart there is a table under the same kind of cite
  - `deck.pptx#slide4`: slide 4 of a PowerPoint, ODP or Keynote deck, notes included. A `.ppt` deck cites the same way, like `old.ppt#slide2`
  - `fax.tif#frame2`: page 2 of a multi-page TIFF or frame 2 of an animation
  - `report.pdf#p1@pt(300,300,316,316)`: a sticky note on page 1
  - `a.hwp#s1:12`: section 1, line 12 of an HWP file
  - `https://example.com/guide#install:28`: line 28 under the heading with that anchor
  - `call.m4a@00:01:23.4-00:01:27.0`: a transcript span
- OCR lines start with their position, such as `@px(120,40,380,72)| 48,250` or `@pt(72,95,140,103)| ...`, so a value can be cited down to its box
- A block whose number two engines read differently ends with `> disputed 765: vision 0, paddle 1`. One with only one engine ends with `> unchecked: one engine`
- Attachments, archive members, files attached to a PDF and downloaded files are saved and melted as items of their own
- A row of kind `rendered` was drawn by LibreOffice or Quick Look and then OCR'd, so its text is only as good as the OCR
  - It's a binary file no converter reads, or an Office binary, HWP or iWork file its parser gave up on
  - The same fallback can also return kind `image`, `media` or `text`
- An iWork bundle folder like `old.pages/` is one item, not a folder of files

## Follow up on needs

The `needs` column lists follow-up work:

- Unread pictures, pages or recordings:
  - `ocr pages 2,5`, `ocr`, `ocr 3 images` or `media`: no local engine was available, or `--shallow` skipped it. Use the meltify-doctor skill, or rerun without `--shallow`
  - `ocr (budget)` or `media (budget)`: the time budget ran out. Rerun with a higher `--budget`
  - `ocr (failed)`: the engines couldn't read it. Run the meltify-ocr skill on that item
  - `... not read (not drawn under --shallow)`: a picture or slide only OCR could read. Rerun without `--shallow`
  - `no text found`: the engines read every picture or recording of the item and found nothing. It may really be blank. If you expected text, check `--lang`, or look at the image yourself
- `hidden N spans`: a PDF holds text a reader can't see. Run the meltify-hidden skill before quoting that page
- `hidden sheets NAME`: those sheets are hidden in the workbook but already melted under their own cite. Say so if you quote them
- `render`: the page looks script-built. Rerun with `--render`
- A missing part. Install it once the user agrees:
  - `markitdown`, `olefile`, `legacy-doc` or `python-calamine`: the office extra (`meltify doctor --install office`)
  - `pyarrow`: the parquet extra. `yt-dlp`: the media extra
  - `archive extra (meltify doctor --install archive)`, `iwork extra (meltify doctor --install iwork)`, `encrypted, needs the crypto extra (meltify doctor --install crypto)` or `encrypted, needs 7-Zip and a password (meltify doctor --install archive)`
  - `... (install LibreOffice, or run meltify doctor --install libreoffice)`
- A locked file:
  - `encrypted, set MELTIFY_PASSWORD or --password-file`: ask the user for the password and rerun as in Run step 6
  - `wrong password`: the password didn't open it. Ask the user again instead of guessing
  - `DRM-protected, open it with the DRM client`, `encrypted for distribution, can't decrypt`, `encrypted, can't decrypt (...)` or `hwp password scheme N not supported`: meltify can't open it at all. Tell the user
- A part melted roughly or not at all. Say so if it matters:
  - `... not read`, such as `2 images not read (over the 256 MiB picture total)`, `iwork preview only`, `first page only, read from a Quick Look preview` or `text only, read by macOS Spotlight without layout or pictures`
  - `WordPerfect read by the built-in walker, ...`: formatting codes may show up in the text, and headers, footers and footnotes are missing
  - `N more rows not read`: a Parquet file showed only its first rows. Query the file itself if the answer needs the rest
  - Anything else starting `empty`, `no ... found` or `unreadable`, or saying `skipped`, `stopped`, `nested items beyond depth 3` or `archive rejected`: part of the item, often an attachment or archive member, wasn't melted
- `unsupported format`: nothing could read the file. Say so instead of guessing at its content

## Gotchas

- In the melted markdown, PDF text includes hidden spans as if they were visible, and text placed off the page is dropped. When `needs` says `hidden N spans`, run the meltify-hidden skill before quoting that page.
- A disputed or unchecked value isn't confirmed. Look at the image yourself or run the meltify-ocr skill on it before you rely on it.
- Linux has no Apple Vision, so with PaddleOCR alone every reading ends with `unchecked`.
- With `--shallow`, or when the budget runs out, an image or a scanned page melts to little or no text. Missing text there means unread, not empty. `--shallow` also skips the rendering fallback, so a file only it could read stays `unsupported format`.
- A file that stays encrypted gets no markdown at all, only its row with the `needs` line.
- PowerPoint 95 and fallback formats need LibreOffice or, on macOS, Quick Look, and `--render` needs the render extra. Without them the row says so in `needs` or `error`.
- URLs to private addresses, pages that `robots.txt` disallows and downloads over 20 MB are refused. Don't add `--allow-private` or `--ignore-robots` unless the user asks.
- Cites use the path exactly as given on the command line. Pass relative paths when the answer should cite short names.

## Then

1. Search the melted markdown with Grep instead of loading every file into context.
2. Quote each fact with the citation of the block or OCR line it came from.
3. Finish every item in `needs` and check every disputed value before answering from that item, or say which items weren't checked.
