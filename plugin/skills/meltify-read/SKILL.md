---
name: meltify-read
description: Melt files, folders and URLs of mixed formats, such as PDF, Excel, Word, PowerPoint, HWP, mail with attachments, KakaoTalk and Slack exports, zip and other archives, web pages, text, images, screenshots, scans, recordings and video URLs, into cited markdown. Images and scans can be cross-checked by several OCR engines, including your own reading, which flag the numbers they disagree on, recordings are transcribed with frames kept, and PDFs are checked for hidden text. Use whenever an answer, summary or document is built from such files or pages, even one named file, one image or a single URL, instead of opening them yourself with Python, openpyxl, a PDF library, unzip, ffmpeg or curl, so every fact can be quoted with its page, cell, line, box or timestamp. Never answer from a single reading of an image.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux. Word, PowerPoint, Outlook and EPUB files, plus .xls, .xlsb, .doc and .ppt, need the office extra, 7z and rar the archive extra, Parquet the parquet extra, encrypted files the crypto extra. PowerPoint 95 and other binary formats need LibreOffice or, on macOS, Quick Look. Recordings need ffmpeg, video URLs the media extra.
metadata:
  version: "0.4.0"
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

## Look closer
- A picture's block starts with a link like `![picture](attachments/memo.docx/pictures/1a2b3c4d5e6f.webp)`, relative to the markdown file. Open it when OCR text can't carry the meaning, like a chart, diagram, photo or layout, and still quote numbers from the cited text. It's scaled to fit 1568 px, so read small print from the text, not the picture

The same command takes a closer look when one image, PDF or video matters. Each option below adds rows or files to the same output.

- One image or scanned page:
  - `--engines vision,gemini` replaces the default engines, here adding a paid one. Name one only when the user agrees. On Linux, there's no `vision`, so use an endpoint the user serves. See `references/engines.md`
  - `--reading agent=FILE` compares your own reading as one more engine. Look at the image or PDF page yourself with Read, write what you see to FILE one line per line of text, and give exactly one image, or one PDF with `--pages N`
  - `--ocr-pages` OCRs PDF pages that have a text layer too. Without it only scanned pages and large pictures are OCR'd, so add it to check a text layer against the page, or before `--reading` on such a page
  - `--equalize` for faint or low-contrast text, `--upscale 3` for a fixed resize, and `--compare tokens` to compare words as well as numbers
  - A value counts as agreed only when every engine reads it the same number of times and at least one of them is local
- PDFs:
  - `--hidden` checks PDFs, and lists every hidden span as a row of kind `hidden`, with its `text`, the `reasons` it's hidden and a cite such as `doc.pdf#p2@pt(72,95,140,103)`. Reasons are render mode 3, opacity, size, off page, layer, color matches background, and covered by a later fill
  - `--contrast` also saves each page rendered in stretched contrast, as rows of kind `contrast` with the picture's `path`, for text drawn inside images
  - `--pages 1,3-5` limits every PDF in the run to those pages
- Recordings and videos:
  - Each video gets rows of kind `frame` with a WebP `path` under `attachments/`, the full frame under `--no-pictures`, and a cite with its time. Its `reasons` say `scene` for a scene change or `interval` for a regular sample. Up to `--frames N` (20) are kept, spread over the video, and OCR'd. `--frames 0` skips frames
  - Only scene changes are taken by default, so a slow change can slip by. `--fps 2` adds interval frames, and since they count toward `--frames` too, raise it with them. A warning says when the cap dropped frames
  - `--start 600 --end 780` narrows to a window, in seconds. Find it first from the transcript
  - `--scene 0.2` catches subtler scene changes, `--keep-duplicates` keeps near-identical frames, marked with the time they repeat, and `--asr NAME` names the speech engine
  - `--subs-only` takes the transcript from subtitles alone, from a URL or from a `.vtt` or `.srt` file beside the video, and skips speech recognition and frames

## Read the output

Every block in the markdown starts with `## <cite>`. Quote that cite, or narrow it down to a line or cell yourself. `references/output.md` lists every row key, row kind, cite format and exit code:

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
- A block whose number two engines read differently ends with `> disputed 765: vision 0, gemini 1`. One with only one engine ends with `> unchecked: one engine`
- Attachments, archive members, files attached to a PDF and downloaded files are saved and melted as items of their own
- A row of kind `rendered` was drawn by LibreOffice or Quick Look and then OCR'd, so its text is only as good as the OCR
  - It's a binary file no converter reads, or an Office binary, HWP or iWork file its parser gave up on
  - The same fallback can also return kind `image`, `media` or `text`
- An iWork bundle folder like `old.pages/` is one item, not a folder of files

## Follow up on needs

The `needs` column lists follow-up work:

- Unread pictures, pages or recordings:
  - `ocr pages 2,5`, `ocr`, `ocr 3 images` or `media`: no engine was available, an endpoint stopped answering mid-run, or `--shallow` skipped it. Use the meltify-doctor skill, or rerun without `--shallow`
  - `ocr (budget)` or `media (budget)`: the time budget ran out. Rerun with a higher `--budget`
  - `ocr (failed)`: the engines couldn't read it. Look at the image yourself, then rerun with `--reading` or another engine
  - `... not read (not drawn under --shallow)`: a picture or slide only OCR could read. Rerun without `--shallow`
  - `no text found`: the engines read every picture or recording of the item and found nothing. It may really be blank. If you expected text, check `--lang`, or open the picture its block links
- `hidden N spans`: a PDF holds text a reader can't see. Rerun with `--hidden` before quoting that page
- `hidden N texts`: an SVG holds text a viewer can't see, marked `[hidden]` in its markdown. Keep it apart from visible text
- `no subtitles`: `--subs-only` found no captions for that recording. Rerun without it to transcribe the speech
- `no pages in 7, it has 3`: `--pages` selected nothing in that PDF
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

- In the melted markdown, PDF text includes hidden spans as if they were visible, and text placed off the page is dropped. When `needs` says `hidden N spans`, rerun with `--hidden` before quoting that page.
- Text drawn inside images never shows up as a hidden span. Use `--contrast`, then look at the rendered pages.
- A span's background comes from a fill only when a single fill fully contains it. Otherwise white is assumed, so light text on a dark picture, or a shape that only partly covers text, can be missed or flagged wrongly. Check those pages in the `--contrast` render.
- A disputed or unchecked value isn't confirmed. Look at the image yourself, or rerun with `--reading`, before you rely on it.
- On macOS 27, Apple Vision applies only the first language. Pass `--lang` with the language of the text, like `--lang en`, not the language of the task.
- Consecutive frames with a near-identical layout and brightness are skipped. A small change, like one digit of a counter, can get skipped along with them. Add `--keep-duplicates` when you're counting or comparing small details.
- The speech engine detects the spoken language per file, since `asr.lang` is `auto` by default. If a transcript comes out in the wrong language, set `asr.lang` in `meltify.toml`, or `MELTIFY_ASR_LANG`, to the language spoken.
- Linux has no Apple Vision, so a value read only by LLM engines or endpoints ends with `unchecked` or `disputed`, never agreed.
- With `--shallow`, or when the budget runs out, an image or a scanned page melts to little or no text. Missing text there means unread, not empty. `--shallow` also skips the rendering fallback, so a file only it could read stays `unsupported format`.
- A file that stays encrypted gets no markdown at all, only its row with the `needs` line.
- PowerPoint 95 and fallback formats need LibreOffice or, on macOS, Quick Look, and `--render` needs the render extra. Without them the row says so in `needs` or `error`.
- URLs to private addresses, pages that `robots.txt` disallows and downloads over 20 MB are refused. Don't add `--allow-private` or `--ignore-robots` unless the user asks.
- Cites use the path exactly as given on the command line. Pass relative paths when the answer should cite short names.

## Then

1. Search the melted markdown with Grep instead of loading every file into context.
2. Quote each fact with the citation of the block, OCR line or frame it came from. Keep hidden text apart from visible text, with the reason it's hidden. If `--hidden` found nothing, say so, and say whether you used `--contrast`.
3. For a video, grep the transcript for when something is said, then open only the frames near that time with Read.
4. Finish every item in `needs` and check every disputed value before answering from that item, or say which items weren't checked. If a value stays uncertain, give its cite instead of picking one.
