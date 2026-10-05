# Security

## Reporting

Please report vulnerabilities through a private GitHub security advisory on this repository, not a public issue.

## What meltify sends where

- `ocr` with the `claude`, `gemini` or `openai` engine uploads the prepared image or its tiles to that provider
- `media --asr api` uploads the extracted audio to the configured transcription endpoint
- `submit` uploads candidate files to the URL you configure
- `doctor --probe` makes one tiny request per configured key
- `read` with a URL fetches that URL, its redirects and the site's `robots.txt`. With `--render`, the headless browser also loads the page's scripts, styles and API calls
- `media`, and `read` with a video URL, download the video and its subtitles through yt-dlp
- The plugin launcher runs `uvx` when it can't find a local meltify of its version, and uv downloads the package and its dependencies
- `doctor --install` downloads the named extra and its dependencies into a venv in the data directory, plus Python 3.12 if uv doesn't have it yet. `--install render` also downloads Playwright's Chromium when it can't find Google Chrome
- The local PaddleOCR and MLX Whisper engines download their model weights from their publishers the first time they run, which `read` triggers too when it meets an image or a recording. PaddleOCR keeps them under `~/.paddlex`, MLX Whisper in the Hugging Face cache
- Nothing else touches the network. Once the models are cached, local engines and every other command run offline

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

## Archives and documents

Archives, mail and documents are melted without extracting anything outside `meltify-out/`:

- An archive with more than 10,000 entries is rejected outright
- A member over 256 MiB is skipped, checked against both its declared size and the bytes actually read
- An archive stops after 1 GiB of output in total, and a member over 1 MiB that expands more than 100:1 is skipped
- Archives and attachments nest at most 3 levels deep
- Member names lose `..`, leading slashes and drive letters before they reach a path or a cite. Symlinks, hard links, device files and encrypted members are skipped
- XML with `<!ENTITY` declarations is refused, which stops entity expansion bombs and external entities
- Pillow's decompression bomb limit is never raised, so oversized images fail instead of filling memory

## Install sources

The only official sources are the `meltify` package on PyPI and the GitHub repository `jeon-jihyeon/meltify`. The launcher fetches `meltify==VERSION` from PyPI by name. Until this project registers that name, a third party could publish a package under it. Set `MELTIFY_FROM_GIT=1` to fetch the tagged release from GitHub instead, or install from the repository yourself.

## Keys

API keys are read from the environment variables named in the config. Config files hold only the variable names. `doctor` reports whether a key is set and never prints its value.

## Rate-limited endpoints

`submit` enforces a minimum gap between requests, honors `Retry-After` and refuses to resend identical bytes. Check the endpoint's terms before you submit, and never run two submitters against the same endpoint.
