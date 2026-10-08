# meltify output reference

The details behind the [README](https://github.com/jeon-jihyeon/meltify#readme): the Python API, what `read` returns, how it cites things, and how it handles files that need more than parsing.

## Python API

```python
import meltify

env = meltify.read("./inputs", out="melted", shallow=True)
for row in env.results:
    print(row["cite"], row["out"], row["needs"])
```

`meltify.read` takes the same flags as the command, as keywords, and returns the same result envelope that `--json` prints. Each value goes through the flag's own type and checks, so `password_file="pw.txt"` arrives as a path, `pages=2` as the page range `2`, and `fps=0` raises `ValueError` just like the command line would refuse it.

For encrypted files, pass `password=`. It outranks `password_file=` and never touches argv.

Only the names exported from `meltify` are public. Every other module is internal and can change in any release.

## Result envelope

`--json` prints the whole result, and `meltify.read` returns it as an `Envelope`:

| Field | Holds |
|---|---|
| `ok` | `false` when `errors` isn't empty |
| `results` | one row per item, keyed as below |
| `errors` | what stopped the run, each with `code` (`usage`, `missing` or `failed`), `message` and sometimes `hint` |
| `warnings` | what didn't stop it, like an item that failed or an engine that's missing |
| `summary`, `inputs`, `artifacts` | the closing line, what you passed, and files written such as `index.jsonl` |
| `command`, `version` | the command and the meltify version |
| `schema` | `meltify/v1`, only in `--json` and `to_dict()`, not an attribute of the `Envelope` |
| exit code | `0` to `3` as listed under [Exit codes](#exit-codes), or `exit_code` on the `Envelope` |

## Result rows

A `read` row has these keys:

| Key | Holds |
|---|---|
| `src`, `cite` | where the item came from, as fields and as one line |
| `kind` | how it was read, like `pdf`, `sheet`, `mail`, `image` or `rendered` |
| `out` | the markdown file written for it |
| `chars` | how many characters of text it melted into |
| `needs` | what's still unread and why, like `hidden sheets Old`, `ocr (budget)`, a password or a missing extra |
| `hidden` | how many PDF spans or SVG texts a reader can't see. `--hidden` lists the PDF ones and why |
| `error`, `hint` | on a failed item, what went wrong and what to try. An item a fallback read in place of a missing extra carries only the `hint` |

URL rows add `final_url`, `fetched_at`, `sha256` and `etag`.

Some rows point at a place inside an item instead of the item itself. They carry the item's markdown in `out`, and `kind` says what they are:

| Kind | When | Adds |
|---|---|---|
| `disputed` | always, for a number two OCR engines read differently | `value`, how many times each engine read it in `counts`, and both as `text` |
| `hidden` | with `--hidden`, for each PDF span a reader can't see | `text`, `reasons`, `color`, `background`, `size` and `opacity` |
| `contrast` | with `--contrast`, for each PDF page | `path` of the page rendered in stretched contrast |
| `frame` | for each video frame kept | `path` of the JPEG, and `reasons`: `scene` or `interval`, plus the time it repeats with `--keep-duplicates` |

A rerun into the same `--out` folder replaces the last run there. Markdown for an item the previous `index.jsonl` listed and this run didn't name at all is deleted, along with its saved attachments, so grepping the folder finds only this run's output. An item that failed this run keeps its previous markdown, and files you put there yourself stay.

## Exit codes

Every command prints a table by default, and the full result with `--json`. It exits with:

- `0`: success
- `1`: a failure inside the command
- `2`: usage error or bad input
- `3`: a needed engine, key, binary or base module is missing

An item `read` couldn't melt doesn't change the exit code. It shows up as a row with `error` and as a warning, and the rest of the run carries on. An unexpected failure still prints the envelope, with error code `failed`. Set `MELTIFY_DEBUG=1` to see its traceback too.

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

## Pictures and recordings

Images, scanned pages, pictures inside PDF, Word, PowerPoint, Excel, ODF, RTF, EPUB, HTML and mail, and the speech and scene frames of recordings all go through local OCR and speech engines in the same pass, and the text lands right where the picture was. Native text always comes first, and OCR only covers what parsing can't reach:

- Charts in Word, PowerPoint, Excel (`.xlsb` too) and ODF become cited tables from the values the file saved. A Word or PowerPoint chart with no cache reads the workbook embedded beside it, and an Excel chart built on formulas that were never calculated asks LibreOffice to calculate them. SmartArt text is read as an indented list
- EMF and WMF pictures, standalone or inside a document, give up their text records directly. Bitmaps and text they can't decode are drawn by LibreOffice, Quick Look on macOS or the pure-Python `metafile-render` and then OCR'd. With none of them, they're listed in `needs`
- `.ppt` slides, notes and pictures, and `.doc` inline and floating pictures, are read natively. Pages and Keynote text, tables and pictures are read from the document itself, older iWork '09 files and bundle folders included
- A scan under a visible header still gets OCR'd, PDF sticky notes become cited blocks, and multi-page TIFFs and animated images are read frame by frame up to 50 frames

How that pass behaves:

- Each unique picture or recording is read once, and every engine result is cached by content hash under `${XDG_CACHE_HOME:-~/.cache}/meltify`, so a rerun only pays for what changed
- `--budget SEC` caps the time spent on uncached work (600 seconds by default). Whatever's left shows up as `ocr (budget)` in `needs`, and the next run picks up where this one stopped
- `--jobs N` sets how many files convert at once (8 by default). PDFs convert in worker processes of their own, except the first PDF of a run. Under `meltify.read` called from your own script, every PDF stays in the calling process
- `--shallow` skips OCR, speech, and drawing or recalculation work, and lists what it skipped in `needs`. Native text melts as usual
- When two OCR engines disagree on a number, the block ends with a `> disputed` line. With only one engine, it ends with `> unchecked: one engine`
- When the engines read every picture and recording of an item and find no text at all, its row says `no text found`, so a blank scan isn't mistaken for one nobody read
- A recording with a `.vtt` or `.srt` file of the same name beside it takes its transcript from that file instead of a speech engine
- Each picture, and each kept video frame, is also saved as a WebP under `attachments/<item>/pictures/` or `frames/`, scaled down to fit 1568 px on the long side and 1.15 megapixels, about what a vision model reads without resizing again. The picture's block starts with `![picture](...)`, a path relative to the markdown, and the link doesn't count toward `chars`. `read.picture_side` and `read.picture_quality` (1568 and 50) set the size and quality, and `--no-pictures`, `read.picture_side = 0` or `--shallow` skip it. PDF pages aren't saved, since the PDF is there to open

## Files nothing else reads

Templates, macro-enabled and slideshow files read like the document they belong to, and a document zip under an unfamiliar name is still recognized by its content.

A binary file no converter reads gets one more try, in this order:

- Formats LibreOffice imports, such as Works, Lotus 1-2-3, Visio and Publisher, are rendered to PDF and cited by page. These rows show kind `rendered`
- Any other picture format Pillow decodes, like PCX or ICNS, goes to OCR as kind `image`
- Audio or video under an odd name is transcribed as kind `media`
- On macOS, a full Quick Look preview (kind `rendered`), the text Spotlight indexes (kind `text`) or a first-page thumbnail (kind `rendered`) is the last resort

Compiled programs and files of one repeated byte skip all of that and return `unsupported format` right away. Word, Excel and PowerPoint binaries, HWP and iWork files whose own parser gives up also get this fallback, unless they're encrypted or DRM-locked. `--shallow` skips this step, and `read.fallback = false` turns it off.

## Encrypted files

Encrypted files open with a password from `--password-file PATH` or `MELTIFY_PASSWORD`: PDF, Word, Excel and PowerPoint, HWP and HWPX, Pages, Keynote and Numbers, and zip, 7z and rar archives. HWP distribution documents, which only restrict copying and printing, open without one. `--password` works too, but other users on the machine can see it. If you set more than one, `--password-file` wins, then `--password`, then `MELTIFY_PASSWORD`.

Without a password, the row says `encrypted` in `needs` and no markdown file is written. A wrong one says `wrong password`. An archive still lists the members it could open.

## URLs

URLs work like files. `read` saves a web page's main text with its heading anchors (`--whole` keeps the full page, `--render` runs it in a headless browser first, using your Chrome or a downloaded Chromium), downloads documents and melts them by type, and downloads videos with their subtitles through yt-dlp. Downloads land under `meltify-out/read/web/` and are revalidated with ETags on the next run. By default it refuses private addresses, follows `robots.txt`, and stops at 20 MB or 30 seconds. [SECURITY.md](https://github.com/jeon-jihyeon/meltify/blob/main/SECURITY.md) has the details.

## Configuration search

The nearest `meltify.toml` is searched from the working directory up to the git root. Outside a git repository, only the working directory is searched, so a stray file in your home directory is never read. `read.parquet_rows` sets how many Parquet rows melt per file, and `read.fallback = false` turns off the fallback for unreadable files.

## More limits

- Speech recognition and OCR quality depend on the engine and the input
- Windows isn't supported: the plugin launcher is a POSIX shell script and `doctor --install` only downloads macOS and Linux builds
- Rendered rows, EMF bitmaps and Quick Look previews are only as good as the OCR that reads them, and Quick Look exists only on macOS
- Pages and Keynote charts are listed in `needs` instead of read, and a Pages or Keynote file with no text records falls back to its embedded preview image
- meltify can't read DRM-protected HWP files or password-protected WordPerfect files. It also can't read the binary Hancom formats: `.cell` and `.show` files from before 2014, and Hanshow `.hpt` files. Save those again as `.xlsx` or `.pptx`
- Parquet files show their first 200 rows (`read.parquet_rows`), and the rest are counted in `needs`
- Encrypted 7z and rar need 7-Zip, which `meltify doctor --install archive` downloads. Plain ones open with the system libarchive, a separate package on Linux, with 7-Zip, or with `bsdtar` on macOS
- `wpd2text` from libwpd reads WordPerfect best, then LibreOffice, and a built-in reader covers the body text without either
