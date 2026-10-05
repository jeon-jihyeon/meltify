<h1 align="center">meltify</h1>

<p align="center">Melt files of any format into prompt-ready text where every line cites its source</p>

<p align="center">
  <a href="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml"><img alt="test" src="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

Agents misread blurry digits, miss text a PDF hides, can't watch video, and paraphrase the one condition that mattered. meltify turns documents, spreadsheets, slides, mail, chat exports, archives, web pages, images, video and audio into markdown an agent can quote, puts a citation on every block, and checks answers before they go out.

## Quickstart

Claude Code:

```
/plugin marketplace add jeon-jihyeon/meltify
/plugin install meltify@meltify
```

Codex, Gemini CLI, Cursor and other agents that read Agent Skills:

```
npx skills add jeon-jihyeon/meltify
```

Command line only:

```
uvx meltify read ./inputs
```

The skills call the `meltify` command. If it isn't installed, the plugin launcher runs it through `uvx`, so [uv](https://docs.astral.sh/uv/) is the only thing you need.

## What it does

| Command | Input | Output |
|---|---|---|
| `read` | files, folders and URLs of mixed formats | one cited markdown file per item, with images, scans and recordings read by local engines, plus a list of what still needs work |
| `ocr` | images and scanned PDF pages | each engine's lines with positions, and the values the engines disagree on |
| `hidden` | PDFs | text a reader doesn't see and why, with page and position |
| `media` | video, audio or a video URL | timestamped full-resolution frames and a timestamped transcript |
| `submit` | candidate files and a scoring endpoint | rate-limited submissions that skip duplicates and keep the best score |
| `check` | an answer file or a string | violations of a schema, counts, unique keys and text rules |
| `brief` | a problem statement | every condition line quoted with its line number, plus a board for juggling several problems |
| `doctor` | nothing | which engines, binaries and keys are available, and how to add the rest |

Each command has a skill of the same name, such as `meltify-ocr`, that tells the agent when to run it and what to do with the result.

## What `read` handles

| Group | Formats | Needs |
|---|---|---|
| Documents | PDF, Word `.docx` and `.doc`, HWP and HWPX, RTF, ODT, Pages, EPUB, HTML, Markdown and plain text | `office` for `.docx`, `.doc`, EPUB and HTML files |
| Spreadsheets | Excel `.xlsx` and `.xls`, ODS, Numbers, CSV and TSV | `office` for `.xls`, `iwork` for Numbers |
| Slides | PowerPoint `.pptx` and `.ppt`, ODP, Keynote | `office` for `.pptx`, LibreOffice for `.ppt` |
| Mail and chat | `.eml`, `.msg`, mbox, KakaoTalk exports, Slack export zips | `office` for `.msg` |
| Archives | zip, tar, tgz, tbz2, txz, 7z, rar | `archive` for 7z and rar |
| Images | PNG, JPEG, WebP, GIF, BMP, TIFF, HEIC, AVIF, SVG | |
| Audio and video | mp3, wav, m4a, flac, ogg, mp4, mov, mkv, webm, subtitles | ffmpeg |
| Web pages | any http(s) URL, video URLs included | `render` for JavaScript-heavy pages, `media` for video sites |
| Data | JSON, JSONL, XML, YAML, SQLite, Jupyter notebooks, vCard, iCalendar | |

Attachments, archive members and downloaded files are melted again as items of their own, up to three levels deep.

Pictures get read in the same pass. Images, scanned pages, pictures inside PDF, Word, PowerPoint, Excel and mail, and the speech and scene frames of recordings all go through local OCR and speech engines, and the text lands right where the picture was:

- Each unique picture or recording is read once, and every engine result is cached by content hash under `${XDG_CACHE_HOME:-~/.cache}/meltify`, so a rerun only pays for what changed
- `--budget SEC` caps the time spent on uncached work (600 seconds by default). Whatever's left shows up as `ocr (budget)` in `needs`, and the next run picks up where this one stopped
- `--shallow` skips OCR and speech recognition and just lists what needs them. Everything else melts as usual
- When two OCR engines disagree on a number, the block ends with a `> disputed` line. With only one engine, it ends with `> unchecked: one engine`

URLs work like files. `read` saves a web page's main text with its heading anchors (`--whole` keeps the full page, `--render` runs it in a headless browser first), downloads documents and melts them by type, and sends video links through the `media` pipeline. Downloads land under `meltify-out/read/web/` and are revalidated with ETags on the next run. By default it refuses private addresses, follows `robots.txt` and stops at 20 MB or 30 seconds. See [SECURITY.md](SECURITY.md) for the details.

## Citations

Every result carries `src` and a one-line `cite`:

| Input | Cite |
|---|---|
| PDF text | `report.pdf#p3@pt(72,95,140,103)` |
| Image region | `menu.png@px(120,40,380,72)` |
| Spreadsheet cell | `calendar.xlsx#Calendar!B7` |
| Mail attachment | `handover.eml#att=cal.xlsx#Calendar` |
| Archive member | `bundle.zip#att=docs/b.pdf#p3` |
| Picture in a document | `memo.docx#para3#img1` |
| HWP section line | `notice.hwp#s1:12` |
| Web page line | `https://example.com/guide#install:28` |
| Text line | `notes.txt:42` |
| Media span | `call.m4a@00:01:23.4-00:01:27.0` |
| JSON value | `answers.json#$[3].reason` |

Every command prints a table by default, and the full result with `--json`. Exit codes:

- `0`: success
- `1`: violations, failed checks or other errors
- `2`: usage error or bad input
- `3`: a needed engine, key, binary or base module is missing

## Engines

| Task | Local | Paid API |
|---|---|---|
| OCR | Apple Vision on macOS, PaddleOCR | Gemini, Claude, any OpenAI-compatible endpoint |
| Speech | MLX Whisper on Apple Silicon, whisper.cpp | any OpenAI-compatible transcription endpoint |

`auto` uses local engines only. Paid engines run only when you name them in `meltify.toml` or on the command line. An agent can also feed in what it reads in an image with `meltify ocr --reading agent=FILE`, so its own reading gets cross-checked against the local engines without any API key.

Optional parts install on request with `meltify doctor --install NAME`:

| Extra | Unlocks |
|---|---|
| `office` | Word, PowerPoint, Outlook `.msg`, EPUB and HTML files, plus legacy `.doc` and `.xls` |
| `archive` | 7z and rar, through the system libarchive |
| `iwork` | Numbers tables |
| `render` | `read --render` for JavaScript-heavy pages, using your Chrome or a downloaded Chromium |
| `media` | video URLs through yt-dlp |
| `asr-mlx` | local speech recognition on Apple Silicon |
| `ocr-paddle` | PaddleOCR, the second local OCR engine |

Recordings need ffmpeg, YouTube downloads need deno, and `.ppt` needs LibreOffice.

## Configuration

Settings are layered, lowest precedence first:

1. The packaged defaults
2. `$XDG_CONFIG_HOME/meltify/config.toml` (usually `~/.config/meltify/config.toml`)
3. The nearest `meltify.toml`, searched from the working directory up to the git root, or the file you pass with `--config`. Outside a git repository, only the working directory is searched
4. `MELTIFY_<KEY>` and `MELTIFY_<SECTION>_<KEY>` environment variables
5. Command-line flags

See [examples/meltify.toml](examples/meltify.toml) for a sample.

## Limits

- Text drawn inside an image is just pixels, so `hidden` finds it only through `--contrast` rendering and OCR
- OCR agreement means the engines read the same value, not that the value is right. A value read only by LLM engines is never marked agreed
- Speech recognition and OCR quality depend on the engine and the input
- Linux has no Apple Vision, so with PaddleOCR alone every reading is marked `unchecked`
- `.ppt` slides need [LibreOffice](https://www.libreoffice.org/). Without it, the row says so in `needs`
- 7z and rar need the system libarchive, a separate package on Linux
- Pages and Keynote files are read only through the preview image they embed, and encrypted HWP and Numbers files aren't read at all
- The plugin launcher is a POSIX shell script, so Windows isn't supported

## License

MIT. meltify depends on [PyMuPDF](https://github.com/pymupdf/PyMuPDF), which is AGPL-3.0, so distributing a product that bundles it comes with AGPL obligations. The `pi-heif` wheels bundle libheif and libde265 (LGPL-3.0), and the `iwork` extra pulls in `enum-tools` (LGPL-3.0) through `numbers-parser`.
