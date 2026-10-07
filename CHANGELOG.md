# Changelog

All notable changes to meltify. Versions follow [semantic versioning](https://semver.org/), and while the major version is 0, a minor release may change behavior.

## 0.3.0

meltify is now about one job: melting files into cited text. `read` does all of it, and `doctor` checks what it needs.

### Changed

- `ocr`, `hidden` and `media` folded into `read`, with every flag they had. The old names still run as `read` with a warning and are removed in 0.4.0. `ocr` becomes `read --ocr-pages`, `hidden` becomes `read --hidden`, and `media` becomes `read --fps 1` with `--no-frames` as `--frames 0`
  - Images: `--upscale`, `--no-sharpen`, `--equalize`, `--compare` and `--reading NAME=FILE`, on top of the engines `read` already ran and compared. `--ocr-pages` OCRs PDF pages that have a text layer too, which `ocr` always did
  - PDFs: `--hidden` adds a row of kind `hidden` per hidden span, `--contrast` a row of kind `contrast` per page with the render's path, and `--pages` limits every PDF in the run
  - Recordings: `--start`, `--end`, `--scene`, `--fps`, `--keep-duplicates` and `--subs-only`. Each video's frames are kept under `attachments/` as rows of kind `frame`, OCR'd or not
- Per-engine `line` rows and the `prepared.png` files from `ocr` are gone. OCR lines live in the markdown with their positions, and disputed values keep their own rows
- `ocr.upscale` defaults to 0, which sizes each picture on its own as `read` always did, and `media.fps` to 0, so interval frames are taken only when asked for
- Every frame `read` keeps, scene or interval, counts toward `--frames`, spread over the video, with distinct frames ahead of kept duplicates. A warning says when the cap dropped some
- With `--hidden`, the summary says `0 hidden spans` when there were none, and warnings count the items it couldn't check and the text inside images it can't see
- A picture only one engine read is counted in the summary and warned about, since nothing in it was cross-checked
- Transcripts say where they came from, as `> from subtitles NAME` or `> transcribed by ENGINE`
- `meltify.read` keywords go through each flag's own checks, so `pages=2` or `compare="words"` is taken or refused the way the command line would
- Disputed rows carry `kind` set to `disputed`, without the old `type` key, and frame rows say why each frame was taken in `reasons`
- An OCR engine named only in a config file that isn't available leaves pictures listed in `needs` with a warning, instead of stopping the run after the text was written
- `--pages`, `--ocr-pages`, `--hidden` and `--contrast` apply to PDFs you pass or that a container holds, not to the PDFs meltify draws itself for other formats
- A URL that failed this run keeps the markdown it wrote before even when it had redirected, since the markdown now records the URL as given

### Removed

- `submit`, `check` and `brief`, with their skills and settings. They worked on answers rather than files. `jsonschema` is no longer a dependency

### Added

- A video or recording with a `.vtt` or `.srt` file of the same name beside it takes its transcript from that file, without a speech engine

## 0.2.4

### Fixed

- A failure inside a command no longer ends in a Python traceback. The envelope still prints, `--json` stays valid, and the error has code `failed`. `MELTIFY_DEBUG=1` prints the traceback too
- `hidden` on a file that isn't a PDF, and `ocr` on a file that isn't an image or a PDF, are usage errors instead of a crash or a silent clean result. A missing file or a folder says so in plain words, and `hidden` on a missing file exits 2 with `no such file`
- Error messages for bad input no longer start with a Python class name like `ValueError:`
- `media` checks the video URL it's given against private addresses before yt-dlp fetches it, and `read` does the same for the video links it finds. yt-dlp's own redirects and segment requests aren't checked one by one. `media --allow-private` lifts the check
- URL fetching refuses to run if the installed httpx can't pin the checked address, instead of fetching without the pin
- A rerun into the same `--out` folder removes the markdown and attachments of items the previous run wrote and this run didn't name at all, so a grep of the folder no longer finds stale items. An item that failed this run keeps its previous markdown, and files you put there yourself stay
- An item whose pictures and recordings were all read but held no text says `no text found` in `needs`, so a blank scan isn't mistaken for one nobody read
- A config key no default names, like a typo, is reported as a warning instead of being ignored
- A number flag set to 0, like `media --scene 0`, is used as 0 instead of falling back to the setting. `--fps 0` and `--upscale 0` are usage errors
- `doctor --install` outside the plugin launcher prints the `pip install` or `uv tool install` command for the extra, pinned to this version and keeping the extras already installed, instead of installing into a venv that command never runs. A missing extra found by the rendering fallback keeps its install hint
- `read --engines` or `--asr` naming an engine that isn't available fails before any markdown is written, instead of after the text files are already out
- `media --scene` outside 0 to 1 is a usage error, and `--fps 0` only matters when frames are taken

### Added

- `read --lang` and `ocr --lang` set the OCR text language for one run, and `meltify.read(lang=...)` does the same. The code is lowercased, and an unknown one is a usage error that lists the known codes: ko, en, ja, zh, de, fr and es

### Performance

- Scanned PDFs no longer decode and hash every page image to find repeated logos. A 20-page scan converts in 0.11 s instead of 3.56 s, with or without the OCR cache
- Comparing OCR engines reads each line once. Three engines with 1,600 lines each compare in 0.01 s instead of 9.7 s
- The hidden-text check skips pages with no text and reads drawings without wrapping every point in an object
- Items with nothing to OCR are written as soon as they're read, and archive members leave memory once they're saved, so a run no longer holds every converted file until the end. Three 200 MB text files peak at 290 MB instead of 771 MB
- The first PDF of a run converts in process, so a run with one PDF doesn't start a worker. Reading one small PDF takes 0.29 s instead of 0.47 s
- Temporary folders no longer slow down as a run creates thousands of them

### Docs

- README shows real output and documents the result envelope and row keys, and the extras table lists `all`
- SECURITY documents the launcher's lookup order and its environment variables

### Project

- PyPI metadata lists supported Python versions and platforms, and links issues and this changelog
- CI tests Python 3.11, 3.12 and 3.13 on macOS and Linux, and Dependabot keeps actions and dependencies current

## 0.2.3

- `meltify.read` is the public Python API. It takes the command's flags as keywords and returns the same envelope `--json` prints

## 0.2.2

- Parquet files and Safari `.webarchive` files

## 0.2.1

- Office macro, template and slideshow variants, `.xlsb`, Hancom `.cell` and `.show`, WordPerfect, XPS, FictionBook, MOBI, `.cbz`, EMF and WMF, and more image, audio and video types
- Charts become cited tables, and binaries no converter reads fall back to LibreOffice, Pillow or Quick Look
- Encrypted PDF, Office, HWP, iWork and archive files open with a password
- Faster and leaner on large inputs

## 0.2.0

- `read` OCRs and transcribes in the same pass, reads URLs, and covers about 20 more formats

## 0.1.0

- First release
