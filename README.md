<h1 align="center">meltify</h1>

<p align="center">Melt files of any format into prompt-ready text where every block cites its source</p>

<p align="center">
  <a href="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml"><img alt="test" src="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/meltify/"><img alt="PyPI" src="https://img.shields.io/pypi/v/meltify"></a>
  <a href="https://pypi.org/project/meltify/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/meltify"></a>
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

Agents misread blurry digits, miss text a PDF hides, can't watch video, and paraphrase the one condition that mattered. meltify turns documents, spreadsheets, slides, mail, chat exports, archives, web pages, images, video and audio into markdown an agent can quote, puts a citation on every block, and checks answers before they go out.

## Quickstart

meltify runs on macOS and Linux with Python 3.11.9 or newer.

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

From Python, after `pip install meltify`:

```python
import meltify

env = meltify.read("./inputs", out="melted", shallow=True)
for row in env.results:
    print(row["cite"], row["out"], row["needs"])
```

`meltify.read` takes the same flags as the command as keywords and returns the same result envelope that `--json` prints. A string option gets the flag's own type, so `password_file="pw.txt"` arrives as a path and `budget="60"` as a number. For encrypted files, pass `password=`: it outranks `password_file=` and never touches argv. Only the names exported from `meltify` are public. Every other module is internal and can change in any release.

The skills call the `meltify` command. If it isn't installed, the plugin launcher runs it through `uvx`, so [uv](https://docs.astral.sh/uv/) is the only thing you need.

## What you get

A folder with a PDF and a mail that carries a spreadsheet:

```
$ meltify read inputs
kind   chars  needs              out                                         cite
mail   133                       meltify-out/read/handover.eml.md            inputs/handover.eml
sheet  151    hidden sheets Old  meltify-out/read/handover.eml__cal.xlsx.md  inputs/handover.eml#att=cal.xlsx
pdf    99                        meltify-out/read/report.pdf.md              inputs/report.pdf
artifact results: meltify-out/read/index.jsonl
3 items melted into meltify-out/read, 0 errors, 1 need more work
```

The attachment became an item of its own, and every block in its markdown starts with the cite to quote:

```markdown
<!-- meltify source: inputs/handover.eml#att=cal.xlsx -->
## inputs/handover.eml#att=cal.xlsx#Calendar
| row | A | B |
|---|---|---|
| 1 | date | event |
| 2 | 2025-07-21 | handover meeting |
| 7 |  | budget review |
```

`--json` prints the whole result, and `meltify.read` returns it as an `Envelope`:

| Field | Holds |
|---|---|
| `ok` | `false` when `errors` isn't empty |
| `results` | one row per item, keyed as below |
| `errors` | what stopped the run, each with `code` (`usage`, `missing`, `failed`, or `violation` from `check`), `message` and sometimes `hint` |
| `warnings` | what didn't stop it, like an item that failed or an engine that's missing |
| `summary`, `inputs`, `artifacts` | the closing line, what you passed, and files written such as `index.jsonl` |
| `command`, `version` | the command and the meltify version |
| `schema` | `meltify/v1`, only in `--json` and `to_dict()`, not an attribute of the `Envelope` |
| exit code | `0` to `3` as listed under [Exit codes](#exit-codes), or `exit_code` on the `Envelope` |

A `read` row has these keys:

| Key | Holds |
|---|---|
| `src`, `cite` | where the item came from, as fields and as one line |
| `kind` | how it was read, like `pdf`, `sheet`, `mail`, `image` or `rendered` |
| `out` | the markdown file written for it |
| `chars` | how many characters of text it melted into |
| `needs` | what's still unread and why, like `hidden sheets Old`, `ocr (budget)`, a password or a missing extra |
| `hidden` | PDF text spans a reader can't see. `meltify hidden` shows where and why |
| `error`, `hint` | on a failed item, what went wrong and what to try. An item a fallback read in place of a missing extra carries only the `hint` |

URL rows add `final_url`, `fetched_at`, `sha256` and `etag`. A number two OCR engines read differently gets a row of its own with `type` set to `disputed`, plus the `value` and how many times each engine read it in `counts`.

A rerun into the same `--out` folder replaces the last run there. Markdown for an item the previous `index.jsonl` listed and this run didn't name at all is deleted, along with its saved attachments, so grepping the folder finds only this run's output. An item that failed this run keeps its previous markdown, and files you put there yourself stay.

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
| Documents | PDF, Word `.docx` `.docm` `.dotx` `.dotm` `.doc` `.dot`, HWP and HWPX, RTF, ODT, Pages, WordPerfect, EPUB, XPS, FictionBook, MOBI, HTML and Safari `.webarchive`, Markdown and plain text | `office` for Word and EPUB |
| Spreadsheets | Excel `.xlsx` `.xlsm` `.xltx` `.xltm` `.xlsb` `.xls` `.xlt`, Hancom `.cell`, ODS, Numbers, Parquet, CSV and TSV | `office` for `.xlsb`, `.xls` and `.xlt`, `iwork` for Numbers, `parquet` for Parquet |
| Slides | PowerPoint `.pptx` `.pptm` `.potx` `.potm` `.ppsx` `.ppsm` `.ppt` `.pps` `.pot`, Hancom `.show`, ODP, Keynote | `office` for PowerPoint and `.show` |
| Drawings | ODG, SVG and `.svgz`, EMF and WMF with `.emz` and `.wmz`, comic book `.cbz` | |
| Mail and chat | `.eml`, `.msg`, mbox, KakaoTalk exports, Slack export zips | `office` for `.msg` |
| Archives | zip, tar, tgz, tbz2, txz, 7z, rar, and single files compressed with gz, bz2 or xz | `archive` for 7z and rar, `crypto` for AES zips |
| Images | PNG, JPEG, WebP, GIF, BMP, TIFF, HEIC, AVIF, JPEG 2000, PSD, ICO, TGA | an OCR engine: Apple Vision on macOS, `ocr-paddle` on Linux |
| Audio and video | mp3, wav, m4a, aac, flac, ogg, opus, wma, aiff, amr, mp4, m4v, mov, mkv, webm, avi, wmv, 3gp, mpg, flv, subtitles | ffmpeg and a speech engine: `asr-mlx` or whisper.cpp. Subtitles need neither |
| Web pages | any http(s) URL, video URLs included | `render` for JavaScript-heavy pages, `media` for video sites |
| Data | JSON, JSONL, XML, YAML, SQLite, Jupyter notebooks, vCard, iCalendar | |

Attachments, archive members, files attached to a PDF and downloaded files are melted again as items of their own, up to three levels deep.

Templates, macro-enabled and slideshow files read like the document they belong to, and a document zip under an unfamiliar name is still recognized by its content.

A binary file no converter reads gets one more try, in this order:

- Formats LibreOffice imports, such as Works, Lotus 1-2-3, Visio and Publisher, are rendered to PDF and cited by page. These rows show kind `rendered`
- Any other picture format Pillow decodes, like PCX or ICNS, goes to OCR as kind `image`
- Audio or video under an odd name is transcribed as kind `media`
- On macOS, a full Quick Look preview (kind `rendered`), the text Spotlight indexes (kind `text`) or a first-page thumbnail (kind `rendered`) is the last resort

Compiled programs and files of one repeated byte skip all of that and return `unsupported format` right away. Word, Excel and PowerPoint binaries, HWP and iWork files whose own parser gives up also get this fallback, unless they're encrypted or DRM-locked. `--shallow` skips this step, and `read.fallback = false` turns it off.

Pictures get read in the same pass. Images, scanned pages, pictures inside PDF, Word, PowerPoint, Excel, ODF, RTF, EPUB, HTML and mail, and the speech and scene frames of recordings all go through local OCR and speech engines, and the text lands right where the picture was. Native text always comes first, and OCR only covers what parsing can't reach:

- Charts in Word, PowerPoint, Excel (`.xlsb` too) and ODF become cited tables from the values the file saved. A Word or PowerPoint chart with no cache reads the workbook embedded beside it, and an Excel chart built on formulas that were never calculated asks LibreOffice to calculate them. SmartArt text is read as an indented list
- EMF and WMF pictures, standalone or inside a document, give up their text records directly. Bitmaps and text they can't decode are drawn by LibreOffice, Quick Look on macOS or the pure-Python `metafile-render` and then OCR'd. With none of them, they're listed in `needs`
- `.ppt` slides, notes and pictures, and `.doc` inline and floating pictures, are read natively. Pages and Keynote text, tables and pictures are read from the document itself, older iWork '09 files and bundle folders included
- A scan under a visible header still gets OCR'd, PDF sticky notes become cited blocks, and multi-page TIFFs and animated images are read frame by frame up to 50 frames

A few more things to know about that pass:

- Each unique picture or recording is read once, and every engine result is cached by content hash under `${XDG_CACHE_HOME:-~/.cache}/meltify`, so a rerun only pays for what changed
- `--budget SEC` caps the time spent on uncached work (600 seconds by default). Whatever's left shows up as `ocr (budget)` in `needs`, and the next run picks up where this one stopped
- `--jobs N` sets how many files convert at once (8 by default). PDFs convert in worker processes of their own, except the first PDF of a run. Under `meltify.read` called from your own script, every PDF stays in the calling process
- `--shallow` skips OCR, speech, and drawing or recalculation work, and lists what it skipped in `needs`. Native text melts as usual
- When two OCR engines disagree on a number, the block ends with a `> disputed` line. With only one engine, it ends with `> unchecked: one engine`
- When the engines read every picture and recording of an item and find no text at all, its row says `no text found`, so a blank scan isn't mistaken for one nobody read

Encrypted files open with a password from `--password-file PATH` or `MELTIFY_PASSWORD`: PDF, Word, Excel and PowerPoint, HWP and HWPX, Pages, Keynote and Numbers, and zip, 7z and rar archives. HWP distribution documents, which only restrict copying and printing, open without one. `--password` works too, but other users on the machine can see it. If you set more than one, `--password-file` wins, then `--password`, then `MELTIFY_PASSWORD`.

Without a password, the row says `encrypted` in `needs` and no markdown file is written. A wrong one says `wrong password`. An archive still lists the members it could open.

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
| Word, ODT or Pages paragraph | `memo.docx:3` |
| Picture in a document | `memo.docx#para3#img1` |
| Slide | `deck.pptx#slide4` |
| Chart in a sheet | `book.xlsx#Sales!D2#img1` |
| Image frame | `fax.tif#frame2` |
| PDF sticky note | `report.pdf#p1@pt(300,300,316,316)` |
| HWP section line | `notice.hwp#s1:12` |
| Web page line | `https://example.com/guide#install:28` |
| Text line | `notes.txt:42` |
| Media span | `call.m4a@00:01:23.4-00:01:27.0` |
| JSON value | `answers.json#$[3].reason` |

## Exit codes

Every command prints a table by default, and the full result with `--json`. It exits with:

- `0`: success
- `1`: violations, failed checks or other errors
- `2`: usage error or bad input
- `3`: a needed engine, key, binary or base module is missing

An item `read` couldn't melt doesn't change the exit code. It shows up as a row with `error` and as a warning, and the rest of the run carries on. An unexpected failure still prints the envelope, with error code `failed`. Set `MELTIFY_DEBUG=1` to see its traceback too.

## Engines

| Task | Local | Paid API |
|---|---|---|
| OCR | Apple Vision on macOS, PaddleOCR | Gemini, Claude, any OpenAI-compatible endpoint |
| Speech | MLX Whisper on Apple Silicon, whisper.cpp | any OpenAI-compatible transcription endpoint |

`auto` uses local engines only. Paid engines run only when you name them in `meltify.toml` or on the command line. An agent can also feed in what it reads in an image with `meltify ocr --reading agent=FILE`, so its own reading gets cross-checked against the local engines without any API key.

Optional parts install on request with `meltify doctor --install NAME`. Under the plugin launcher, it installs into the launcher's own venv. Anywhere else it installs nothing and prints the command for your setup, like `uv tool install 'meltify[office]'` or `pip install 'meltify[office]'`. With `uvx`, use `uvx --from 'meltify[office]' meltify ...`. The extras:

| Extra | Unlocks |
|---|---|
| `office` | Word, PowerPoint, Outlook `.msg` and EPUB, legacy `.doc`, `.ppt`, `.xls` and `.xlsb`, faster `.xlsx` reading through calamine, and drawing EMF pictures without LibreOffice |
| `archive` | 7z and rar through the system libarchive, plus a pinned 7-Zip download for encrypted ones |
| `iwork` | Numbers tables. Pages and Keynote need nothing extra |
| `parquet` | Parquet files, the first 200 rows and per-column min, max and null counts |
| `crypto` | encrypted Office files, AES zips, and password-protected HWP and iWork files |
| `render` | `read --render` for JavaScript-heavy pages, using your Chrome or a downloaded Chromium |
| `media` | video URLs through yt-dlp |
| `asr-mlx` | local speech recognition on Apple Silicon |
| `ocr-paddle` | PaddleOCR, the second local OCR engine |
| `all` | `office`, `archive`, `parquet`, `crypto`, `media` and `asr-mlx` together. `iwork`, `render` and `ocr-paddle` install on their own |

Recordings need ffmpeg and YouTube downloads need deno. PowerPoint 95, formula recalculation and the rendering fallback need LibreOffice, which `meltify doctor --install libreoffice` downloads as a portable copy on macOS and Linux. `wpd2text` from libwpd reads WordPerfect best, then LibreOffice, and a built-in reader covers the body text without either.

## Configuration

Settings are layered, lowest precedence first:

1. The packaged defaults
2. `$XDG_CONFIG_HOME/meltify/config.toml` (usually `~/.config/meltify/config.toml`)
3. The nearest `meltify.toml`, searched from the working directory up to the git root, or the file you pass with `--config`. Outside a git repository, only the working directory is searched
4. `MELTIFY_<KEY>` and `MELTIFY_<SECTION>_<KEY>` environment variables
5. Command-line flags

See [examples/meltify.toml](examples/meltify.toml) for a sample. A key no default names, like a typo, shows up as a warning instead of being ignored. A few keys worth knowing:

- `lang` is the language OCR expects (`ko` by default). `--lang` on `read` and `ocr` sets it for one run, and takes `ko`, `en`, `ja`, `zh`, `de`, `fr` or `es`
- `asr.lang` is the spoken language for speech engines. It's `auto` by default, separate from `lang`, so Whisper detects each recording's language instead of translating it
- `read.parquet_rows` sets how many Parquet rows melt per file
- `render.quicklook = false` keeps Quick Look and Spotlight out of every render

## Limits

- Text drawn inside an image is just pixels, so `hidden` finds it only through `--contrast` rendering and OCR
- OCR agreement means the engines read the same value, not that the value is right. A value read only by LLM engines is never marked agreed
- Speech recognition and OCR quality depend on the engine and the input
- Linux has no Apple Vision, so with PaddleOCR alone every reading is marked `unchecked`
- Rendered rows, EMF bitmaps and Quick Look previews are only as good as the OCR that reads them, and Quick Look exists only on macOS
- Pages and Keynote charts are listed in `needs` instead of read, and a Pages or Keynote file with no text records falls back to its embedded preview image
- meltify can't read DRM-protected HWP files or password-protected WordPerfect files. It also can't read the binary Hancom formats: `.cell` and `.show` files from before 2014, and Hanshow `.hpt` files. Save those again as `.xlsx` or `.pptx`
- Parquet files show their first 200 rows, and the rest are counted in `needs`
- Encrypted 7z and rar need 7-Zip, which `meltify doctor --install archive` downloads. Plain ones open with the system libarchive, a separate package on Linux, with 7-Zip, or with `bsdtar` on macOS
- The plugin launcher is a POSIX shell script and `doctor --install` only downloads macOS and Linux builds, so Windows isn't supported

## License

MIT. meltify depends on [PyMuPDF](https://github.com/pymupdf/PyMuPDF), which is AGPL-3.0, so distributing a product that bundles it comes with AGPL obligations. The `pi-heif` wheels bundle libheif and libde265 (LGPL-3.0), and the `iwork` extra pulls in `enum-tools` (LGPL-3.0) through `numbers-parser`. The programs `doctor --install` downloads keep their own licenses: LibreOffice is MPL-2.0, and 7-Zip is LGPL-2.1 with the unRAR restriction.
