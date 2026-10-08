# Security

## Reporting

Please report vulnerabilities through a private GitHub security advisory on this repository, not a public issue.

## What meltify sends where

The data directory below is `$CLAUDE_PLUGIN_DATA` when the plugin sets it, else `${XDG_DATA_HOME:-~/.local/share}/meltify`.

- `read --engines` with the `claude`, `gemini` or `openai` engine uploads each prepared image or its tiles to that provider
- `read --asr api` uploads the extracted audio to the configured transcription endpoint
- With `[ocr.endpoints.NAME]` or `[asr.endpoints.NAME]`, plain `read` sends images or audio to the servers you configured, and `doctor` asks each for its model list. meltify only accepts hosts that resolve to loopback or private addresses, so a hosted API never gets data you didn't name it for. The check runs when the run starts and isn't pinned per request, so use an IP or a host name you control
- `doctor --probe` makes one tiny request per configured key
- `read` with a URL fetches that URL, its redirects and the site's `robots.txt`. With `--render`, the headless browser also loads the page's scripts, styles and API calls
- `read` with a video URL downloads the video and its subtitles through yt-dlp
- The plugin launcher runs `uvx` when it can't find a local meltify of its version, and uv downloads the package and its dependencies
- `doctor --install` downloads the named extra and its dependencies into a venv in the data directory, plus Python 3.12 if uv doesn't have it yet. `--install render` also downloads Playwright's Chromium when it can't find Google Chrome. `--install archive` and `--install all` also download 7-Zip, and `--install libreoffice` downloads a portable LibreOffice. See [Downloaded programs](#downloaded-programs)
- Nothing else touches the network. Local engines and every other command run offline

## URLs in `read`

`read` treats every URL and everything it links to as untrusted:

- Only `http` and `https` are fetched
- Every address a host resolves to must be public, so loopback, private, link-local and reserved ranges are refused, including IPv4 hidden inside IPv6 forms
- The connection goes to an address that passed the check, and the peer address is checked again once connected, so DNS rebinding can't swap it
- Each of up to 5 redirects is checked the same way. With `--render`, so is every request the browser makes
- Proxy settings from the environment are ignored, since a proxy would hide the real peer
- `robots.txt` is honored for the `meltify` user agent, and a server error on it counts as a full disallow. `--ignore-robots` turns this off
- Downloads stop at 20 MB (`--max-bytes`), connections time out after 10 seconds and a whole fetch after 30, and requests to one host are spaced at least a second apart
- `--allow-private` lifts only the address check, for intranet pages you trust

A video URL gets the same scheme and address check before yt-dlp sees it. yt-dlp then makes its own requests, for manifests and media segments, which meltify doesn't check one by one. `--allow-private` lifts the address check there too.

## Archives and documents

Archives, mail and documents are melted without extracting anything outside `meltify-out/`:

- An archive with more than 10,000 entries is rejected outright
- A member over 256 MiB is skipped, checked against both its declared size and the bytes actually read
- An archive stops after 1 GiB of output in total, and a member over 1 MiB that expands more than 100:1 is skipped
- Archives and attachments nest at most 3 levels deep
- Member names lose `..`, leading slashes and drive letters before they reach a path or a cite. Symlinks, hard links and device files are skipped, and so are encrypted members unless you give a password
- XML with `<!ENTITY` declarations is refused, which stops entity expansion bombs and external entities
- Every XML part of a Word or PowerPoint package is checked against the 64 MiB part limit and the 100:1 ratio before any reader opens the file. Excel `.xlsx` packages skip this pre-check and go straight to openpyxl or calamine
- Pillow's decompression bomb limit is never raised, so images past it fail instead of filling memory
- A single file compressed with gz, bz2 or xz goes through the same size, ratio and total limits as an archive member
- Parquet rows come only from row groups that declare 256 MiB or less unpacked in total, and the row groups past that are listed in `needs`
- A picture over 64 MiB, or one over 1 MiB that expands more than 100:1, is skipped before it's read, and an EPUB's spine documents stop at 256 MiB in total
- The pictures one Office, ODF, HWP, RTF, EPUB, HTML or Outlook document unpacks share a 256 MiB total. A picture shown many times is held once, and pictures past the total are listed in `needs`
- A password HWP file decrypts at most 4 MiB of embedded pictures, since decryption is slow in Python. The body text is always read, and the pictures past that are listed in `needs`
- A password HWPX is decrypted under a budget of 100 times the file's size, at least 64 MiB and at most 512 MiB. Each part is inflated only up to the 64 MiB part limit, and a part past either stops the file with a `needs` entry
- A Safari web archive melts each distinct frame once, at most 100 of them, and never more frame data than twice the archive's own size. The rest are listed in `needs`
- A local HTML page reads pictures only from its own folder, symlinks resolved, and a fetched page reads only `data:` pictures. Remote pictures are listed in `needs` and never downloaded
- An image file contributes at most 50 frames

## Passwords

`read` opens encrypted PDF, Office, HWP, iWork, zip, 7z and RAR files only with a password you give it:

- `--password-file PATH` reads the first line of a file, and `MELTIFY_PASSWORD` reads the environment. `--password` works too, but other users on the machine can see it in the process list, so `read` warns when you use it
- When more than one is set, `--password-file` wins, then `--password`, then `MELTIFY_PASSWORD`. From Python, `meltify.read(..., password=...)` outranks all three
- A document that stays locked gets a row with only its `needs` entry, and no markdown is written for it
- The password never goes into argv. 7-Zip gets it on stdin, and every other format is decrypted in-process
- A decrypted copy of a PDF or Office file goes into a private temporary directory, removed right after conversion, or when the process exits if its pages still wait for OCR
- The password never appears in results, cites, markdown or the cache. A wrong one shows up only as `wrong password` in `needs`
- A password-protected WordPerfect file isn't opened, since `wpd2text` takes its password only on the command line
- DRM-protected HWP files are listed in `needs` and never opened

## Programs `read` starts

Some formats need a program outside Python. Each one runs with an argument list and no shell, on a file meltify already holds, and writes into a fresh temporary directory. Every program starts in its own session, so a timeout or an interrupt kills its whole process group, helpers included. Files reach it as absolute paths, so a file named like an option, such as `--accept=...`, stays a file:

- LibreOffice `soffice` renders PowerPoint 95, EMF and WMF pictures meltify can't draw itself, uncached charts, formula recalculation and fallback formats to PDF. It runs headless with a private profile per call, with a 120 to 180 second timeout
- 7-Zip `7zz`, `7z` or `7za` lists and extracts 7z and RAR members to stdout, with a 60 second listing and a 900 second extraction timeout. `bsdtar` is the last resort for archives libarchive and 7-Zip can't open
- `wpd2text` from libwpd reads WordPerfect files, with a 120 second timeout
- `ffprobe` checks whether a file no converter claims holds audio or video, with a 20 second timeout. It only runs on files whose first bytes look like an audio or video container
- On macOS, `mdls` checks that Spotlight treats a file as a document or picture (`public.content`), with a 10 second timeout, and only then does `qlmanage` draw a preview of it, killed after 30 seconds
- On macOS, an Office preview that Quick Look writes as HTML is laid out by WebKit through `osascript`, under a content security policy that blocks every remote load. It's killed after 120 seconds
- On macOS, `textutil` and `mdimport` read the text Spotlight indexes for a fallback file, with a 30 second timeout. `mdimport` only runs on files Spotlight counts as `public.content`
- The rendering fallback only runs for binary files no converter reads, and for Word, Excel and PowerPoint binaries, HWP and iWork files whose parser gives up. Compiled programs and files of one repeated byte never reach any of these programs, and encrypted and DRM-locked files are never rendered
- `read.fallback = false` turns the fallback off, and `render.quicklook = false` keeps Quick Look out of every format
- `--shallow` starts no work that only feeds OCR or charts: no formula recalculation, no EMF or WMF drawing, no renders of slides without text, no fallback and no Quick Look

`read` also runs its own work in parallel:

- With `--jobs` above 1 (8 by default), PDFs convert in worker processes started with `spawn`, since PyMuPDF isn't thread-safe. When you call `meltify.read` from your own script, PDFs stay in the calling process instead, since `spawn` would import that script again in every worker
- OCR and speech run up to 4 jobs at once. whisper.cpp runs one large model per call, so it takes one job at a time, while Apple Vision and every HTTP engine take jobs side by side

## Downloaded programs

`doctor --install libreoffice` and `doctor --install archive` download one pinned release each, LibreOffice 26.8.1 from the Document Foundation and 7-Zip 26.03 from its GitHub releases:

- Only over HTTPS, from the URL pinned in the source for this platform
- The file must match its pinned SHA-256 and byte size, or it's deleted before anything is unpacked. A download stops as soon as it runs past the pinned size, and a partial file never stays behind
- The macOS LibreOffice disk image is attached read-only without opening anything, and only `LibreOffice.app` is copied out. On Linux only the `opt/` tree of the `.deb` packages is unpacked, through Python's tar filter that refuses links and paths leaving the target
- From the 7-Zip archive only `7zz` and its license files are written, by fixed names
- Both land in `tools/` under the data directory and replace an earlier copy only once fully unpacked

## Install sources

The only official sources are the `meltify` package on PyPI and the GitHub repository `jeon-jihyeon/meltify`.

The plugin launcher picks the first meltify it finds, in this order:

1. A source checkout that holds the launcher, run through `uv run`
2. The venv `meltify doctor --install` created in the data directory, when it holds the launcher's version
3. A `meltify` on `PATH` that reports the launcher's version
4. `uvx` with `meltify==VERSION` from PyPI

Three environment variables change that:

| Variable | Effect |
|---|---|
| `MELTIFY_FROM_GIT=1` | step 4 fetches the `vVERSION` tag from the GitHub repository instead of PyPI |
| `MELTIFY_EXTRAS=office,archive` | step 4 installs those extras too, as `meltify[office,archive]` |
| `MELTIFY_ALLOW_PATH=1` | step 3 accepts a `meltify` on `PATH` of any version, so you're trusting whatever that is |

## Keys

API keys are read from the environment variables named in the config. Config files hold only the variable names. `doctor` reports whether a key is set and never prints its value.
