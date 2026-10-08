<h1 align="center">meltify</h1>

<p align="center">Turn files of any format into prompt-ready text, with a citation on every block</p>

<p align="center">
  <a href="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml"><img alt="test" src="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml/badge.svg"></a>
  <a href="https://pypi.org/project/meltify/"><img alt="PyPI" src="https://img.shields.io/pypi/v/meltify"></a>
  <a href="https://pypi.org/project/meltify/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/meltify"></a>
  <a href="https://github.com/jeon-jihyeon/meltify/blob/main/LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

Agents misread blurry digits, miss text a PDF hides, and can't watch video. meltify turns documents, spreadsheets, slides, mail, chat exports, archives, web pages, images, video, and audio into markdown your agent can quote, and every block says where it came from.

- Images and scans can be cross-checked by more than one OCR engine, your agent's own reading included, and any number they disagree on gets flagged
- PDFs are checked for text a reader can't see
- Videos keep their key frames next to the transcript
- Everything runs locally by default. Paid APIs only run when you ask for them

## Quickstart

meltify runs on macOS and Linux with Python 3.11.9 or newer. Windows isn't supported, since the plugin launcher is a POSIX shell script.

Claude Code:

```
/plugin marketplace add jeon-jihyeon/meltify
/plugin install meltify@meltify
```

Codex, Gemini CLI, Cursor, and other agents that read Agent Skills:

```
npx skills add jeon-jihyeon/meltify
```

Command line:

```
uvx meltify read ./inputs
```

Python, after `pip install meltify`:

```python
import meltify

env = meltify.read("./inputs", out="melted")
for row in env.results:
    print(row["cite"], row["out"], row["needs"])
```

The plugin comes with two skills: `meltify-read` tells your agent when to melt files and how to use the result, and `meltify-doctor` checks what's installed. If the `meltify` command isn't on your path, the plugin runs it through `uvx`, so [uv](https://docs.astral.sh/uv/) is all you need.

## What you get

Point `read` at a folder with a PDF and an email that carries a spreadsheet:

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

The `needs` column tells you what's still unread and why, like a hidden sheet, a password, or OCR that ran out of time. Add `--json` for the full result. The [reference](https://github.com/jeon-jihyeon/meltify/blob/main/plugin/skills/meltify-read/references/output.md) covers every field, every cite format, and the exit codes.

Cites look like this:

| Input | Cite |
|---|---|
| PDF text | `report.pdf#p3@pt(72,95,140,103)` |
| Image region | `menu.png@px(120,40,380,72)` |
| Spreadsheet cell | `calendar.xlsx#Calendar!B7` |
| Archive member | `bundle.zip#att=docs/b.pdf#p3` |
| Slide | `deck.pptx#slide4` |
| Recording | `call.m4a@00:01:23.4-00:01:27.0` |

## Taking a closer look

`read` handles most files with no flags at all. When one image, PDF, or video needs more attention:

| Input | Flags |
|---|---|
| Images and scans | `--engines vision,gemini` to pick engines, `--reading NAME=FILE` to cross-check your own reading, `--ocr-pages` to OCR PDF pages that already have a text layer, plus `--upscale`, `--equalize`, `--no-sharpen`, and `--compare tokens` |
| PDFs | `--hidden` for a row per hidden span, `--contrast` to save pages in stretched contrast, which shows text drawn inside images that `--hidden` can't find, and `--pages 1,3-5` |
| Recordings and videos | `--start` and `--end` in seconds, `--fps` for interval frames, `--scene`, `--keep-duplicates`, `--frames N`, and `--subs-only` for captions alone |

## Supported formats

| Group | Formats | Needs |
|---|---|---|
| Documents | PDF, Word `.docx` `.docm` `.dotx` `.dotm` `.doc` `.dot`, HWP and HWPX, RTF, ODT, Pages, WordPerfect, EPUB, XPS, FictionBook, MOBI, HTML and Safari `.webarchive`, Markdown and plain text | `office` for Word and EPUB |
| Spreadsheets | Excel `.xlsx` `.xlsm` `.xltx` `.xltm` `.xlsb` `.xls` `.xlt`, Hancom `.cell`, ODS, Numbers, Parquet, CSV and TSV | `office` for `.xlsb`, `.xls` and `.xlt`, `iwork` for Numbers, `parquet` for Parquet |
| Slides | PowerPoint `.pptx` `.pptm` `.potx` `.potm` `.ppsx` `.ppsm` `.ppt` `.pps` `.pot`, Hancom `.show`, ODP, Keynote | `office` for PowerPoint and `.show` |
| Drawings | ODG, SVG and `.svgz`, EMF and WMF with `.emz` and `.wmz`, comic book `.cbz` | |
| Mail and chat | `.eml`, `.msg`, mbox, KakaoTalk exports, Slack export zips | `office` for `.msg` |
| Archives | zip, tar, tgz, tbz2, txz, 7z, rar, and single files compressed with gz, bz2 or xz | `archive` for 7z and rar, `crypto` for AES zips |
| Images | PNG, JPEG, WebP, GIF, BMP, TIFF, HEIC, AVIF, JPEG 2000, PSD, ICO, TGA | an OCR engine: Apple Vision on macOS, or a [model you serve](#engines) |
| Audio and video | mp3, wav, m4a, aac, flac, ogg, opus, wma, aiff, amr, mp4, m4v, mov, mkv, webm, avi, wmv, 3gp, mpg, flv, subtitles | ffmpeg and a speech engine: whisper.cpp with `asr.whispercpp_model` set, or a [model you serve](#engines). Subtitles need neither |
| Web pages | any http(s) URL, video URLs included | `render` for JavaScript-heavy pages, `media` for video sites |
| Data | JSON, JSONL, XML, YAML, SQLite, Jupyter notebooks, vCard, iCalendar | |

A few things that happen along the way:

- Attachments, archive members, and downloads become items of their own, up to three levels deep
- Pictures inside documents are OCR'd in place, and charts turn into tables built from the values the file saved
- Each picture is also saved as a small WebP beside the markdown and linked from its block, so you can look at a chart or photo the text can't capture. `--no-pictures` skips it
- A file no converter reads gets another shot through LibreOffice, Pillow, ffprobe, or Quick Look on macOS
- Engine results are cached by content hash, so a rerun only pays for what changed. `--budget` caps new OCR and speech work at 600 seconds by default, and `--shallow` skips it entirely
- Encrypted files open with `--password-file PATH` or `MELTIFY_PASSWORD`. DRM-protected HWP files and pre-2014 binary Hancom files can't be read, and the [reference](https://github.com/jeon-jihyeon/meltify/blob/main/plugin/skills/meltify-read/references/output.md#more-limits) lists the other gaps
- URLs get the page's main text, or the document or video behind them. Private addresses are refused and `robots.txt` is respected. See [SECURITY.md](https://github.com/jeon-jihyeon/meltify/blob/main/SECURITY.md)

## Engines

| Task | Local | Paid API |
|---|---|---|
| OCR | Apple Vision on macOS, or a model you serve | Gemini, Claude, any OpenAI-compatible endpoint |
| Speech | whisper.cpp, or a model you serve | any OpenAI-compatible transcription endpoint |

`auto` only uses local engines and endpoints you serve. Paid engines run when you name them in `meltify.toml` or on the command line. Your agent can also hand in its own reading of an image with `meltify read IMAGE --reading agent=FILE`, and meltify cross-checks it against the local engines, no API key needed.

meltify doesn't bundle or download models. To add one, serve it behind an OpenAI-compatible endpoint and point meltify at it. For example, [PaddleOCR-VL](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) on Apple Silicon:

```
uvx --from mlx-vlm mlx_vlm.server --model PaddlePaddle/PaddleOCR-VL-1.6 --port 8111
```

```toml
[ocr.endpoints.paddle-vl]
base_url = "http://127.0.0.1:8111/v1"
model = "PaddlePaddle/PaddleOCR-VL-1.6"
```

Each endpoint is an engine named after its table, and `auto` runs it next to Apple Vision with no key. It's still a vision LLM that can guess, so a value it reads counts as agreed only when a local engine like Apple Vision reads it too. Only loopback and private addresses qualify, even when you name the endpoint, so a hosted API runs only as the `claude`, `gemini`, or `openai` engine. `key_env`, `timeout`, and `prompt` are there when a server needs them, `[asr.endpoints.NAME]` does the same for speech without `prompt`, and `meltify doctor` checks that each one answers.

Agreement means the engines read the same value, not that the value is right, and a value only LLM engines read is never marked agreed. With a single engine every reading is marked `unchecked`, and on Linux, where there's no Apple Vision, LLM readings stay unchecked or disputed.

Run `meltify doctor` to see what's available. `meltify doctor --install NAME` adds an optional part: under the plugin, it installs into the plugin's own venv, and anywhere else it prints the right command for your setup, like `uv tool install 'meltify[office]'`. With `uvx`, use `uvx --from 'meltify[office]' meltify ...`.

| Extra | Unlocks |
|---|---|
| `office` | Word, PowerPoint, Outlook `.msg`, EPUB, legacy `.doc`, `.ppt`, `.xls` and `.xlsb`, faster `.xlsx`, and EMF drawing without LibreOffice |
| `archive` | 7z and rar through the system libarchive, plus a pinned 7-Zip download for encrypted ones |
| `crypto` | encrypted Office files, AES zips, and password-protected HWP and iWork files |
| `iwork`, `parquet` | Numbers tables, and Parquet files (first 200 rows plus per-column stats) |
| `render`, `media` | `read --render` for JavaScript-heavy pages, and video URLs through yt-dlp |

`all` bundles `office`, `archive`, `parquet`, `crypto`, and `media`. Recordings need ffmpeg, and YouTube downloads need deno. LibreOffice covers PowerPoint 95, formula recalculation, and the fallback renderer, and `meltify doctor --install libreoffice` downloads a portable copy.

## Configuration

Settings stack up from lowest to highest precedence:

1. Built-in defaults
2. `~/.config/meltify/config.toml` (or under `$XDG_CONFIG_HOME`)
3. The nearest `meltify.toml`, from the working directory up to the git root, or the file passed with `--config`
4. `MELTIFY_<KEY>` and `MELTIFY_<SECTION>_<KEY>` environment variables
5. Command-line flags

[examples/meltify.toml](https://github.com/jeon-jihyeon/meltify/blob/main/examples/meltify.toml) shows the settings you're most likely to change, and [defaults.toml](https://github.com/jeon-jihyeon/meltify/blob/main/src/meltify/data/defaults.toml) lists every one. Misspelled keys show up as warnings. A few worth knowing:

- `lang`: the language OCR expects, `ko` by default. `--lang` overrides it for one run (`ko`, `en`, `ja`, `zh`, `de`, `fr`, or `es`)
- `asr.lang`: the spoken language, `auto` by default so Whisper detects it per recording instead of translating
- `render.quicklook = false`: keeps Quick Look and Spotlight out of every render

## License

MIT. meltify depends on [PyMuPDF](https://github.com/pymupdf/PyMuPDF), which is AGPL-3.0, so distributing a product that bundles it comes with AGPL obligations. The `pi-heif` wheels bundle libheif and libde265 (LGPL-3.0), and the `iwork` extra pulls in `enum-tools` (LGPL-3.0) through `numbers-parser`. The programs `doctor --install` downloads keep their own licenses: LibreOffice is MPL-2.0, and 7-Zip is LGPL-2.1 with the unRAR restriction.
