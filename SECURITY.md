# Security

## Reporting

Please report vulnerabilities through a private GitHub security advisory on this repository, not a public issue.

## What meltify sends where

- `ocr` with the `claude`, `gemini` or `openai` engine uploads the prepared image or its tiles to that provider
- `media --asr api` uploads the extracted audio to the configured transcription endpoint
- `submit` uploads candidate files to the URL you configure
- `doctor --probe` makes one tiny request per configured key
- `media` with a URL downloads it
- The plugin launcher runs `uvx` when it can't find a local meltify of its version, and uv downloads the package and its dependencies
- `doctor --install` downloads the named extra and its dependencies into a venv in the data directory, plus Python 3.12 if uv doesn't have it yet
- The local PaddleOCR and MLX Whisper engines download their model weights the first time they run
- Nothing else touches the network. Once the models are cached, local engines and every other command run offline

## Install sources

The only official sources are the `meltify` package on PyPI and the GitHub repository `jeon-jihyeon/meltify`. The launcher fetches `meltify==VERSION` from PyPI by name. Until this project registers that name, a third party could publish a package under it. Set `MELTIFY_FROM_GIT=1` to fetch the tagged release from GitHub instead, or install from the repository yourself.

## Keys

API keys are read from the environment variables named in the config. Config files hold only the variable names. `doctor` reports whether a key is set and never prints its value.

## Rate-limited endpoints

`submit` enforces a minimum gap between requests, honors `Retry-After` and refuses to resend identical bytes. Check the endpoint's terms before you submit, and never run two submitters against the same endpoint.
