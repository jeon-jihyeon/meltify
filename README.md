<h1 align="center">meltify</h1>

<p align="center">Melt files of any format into prompt-ready text where every line cites its source</p>

<p align="center">
  <a href="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml"><img alt="test" src="https://github.com/jeon-jihyeon/meltify/actions/workflows/test.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-blue"></a>
</p>

Agents misread blurry digits, miss text a PDF hides, can't watch video, and paraphrase the one condition that mattered. meltify turns images, PDFs, office files, mail, video and audio into markdown an agent can quote, puts a citation on every block, and checks answers before they go out.

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
| `read` | files and folders of mixed formats | one cited markdown file per item, plus a list of what still needs OCR, transcription or a hidden-text check |
| `ocr` | images and scanned PDF pages | each engine's lines with positions, and the values the engines disagree on |
| `hidden` | PDFs | text a reader doesn't see and why, with page and position |
| `media` | video, audio or a video URL | timestamped full-resolution frames and a timestamped transcript |
| `submit` | candidate files and a scoring endpoint | rate-limited submissions that skip duplicates and keep the best score |
| `check` | an answer file or a string | violations of a schema, counts, unique keys and text rules |
| `brief` | a problem statement | every condition line quoted with its line number, plus a board for juggling several problems |
| `doctor` | nothing | which engines, binaries and keys are available, and how to add the rest |

Each command has a skill of the same name, such as `meltify-ocr`, that tells the agent when to run it and what to do with the result.

## Citations

Every result carries `src` and a one-line `cite`:

| Input | Cite |
|---|---|
| PDF text | `report.pdf#p3@pt(72,95,140,103)` |
| Image region | `menu.png@px(120,40,380,72)` |
| Spreadsheet cell | `calendar.xlsx#Calendar!B7` |
| Mail attachment | `handover.eml#att=cal.xlsx#Calendar` |
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

Optional parts install on request:

```
meltify doctor --install office      # docx, pptx and Outlook msg
meltify doctor --install media       # video URLs through yt-dlp
meltify doctor --install asr-mlx     # local speech on Apple Silicon
meltify doctor --install ocr-paddle  # PaddleOCR
```

`media` needs ffmpeg, and YouTube downloads need deno.

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
- The plugin launcher is a POSIX shell script, so Windows isn't supported

## License

MIT. meltify depends on [PyMuPDF](https://github.com/pymupdf/PyMuPDF), which is AGPL-3.0, so distributing a product that bundles it comes with AGPL obligations.
