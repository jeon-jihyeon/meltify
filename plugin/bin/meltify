#!/bin/sh
# Find the meltify CLI for this version and run it with the given arguments, trying in order:
# 1. A source checkout that holds this script, so local edits run while developing
# 2. The venv that `meltify doctor --install` created in the data directory.
#    It comes before PATH, since doctor installs extras only into this venv
# 3. Another meltify on PATH that reports this version, or any version when MELTIFY_ALLOW_PATH is 1.
#    Other copies of this launcher on PATH are skipped, so uvx never runs twice
# 4. uvx with this version from PyPI, or from the GitHub tag when MELTIFY_FROM_GIT is 1
# scripts/sync_version.py writes VERSION and copies this file into the plugin and every skill
set -eu

VERSION="0.3.0"
REPO="jeon-jihyeon/meltify"
DATA="${CLAUDE_PLUGIN_DATA:-${XDG_DATA_HOME:-${HOME}/.local/share}/meltify}"
HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
dir="$HERE"
# Five levels reach the repo root from the deepest copy, plugin/skills/NAME/scripts
for _ in 1 2 3 4 5; do
  if [ -f "${dir}/pyproject.toml" ] && grep -q '^name = "meltify"' "${dir}/pyproject.toml" &&
    command -v uv >/dev/null 2>&1; then
    exec uv run --quiet --project "$dir" meltify "$@"
  fi
  dir=$(dirname -- "$dir")
done

# Tells `meltify doctor --install` that this launcher, which checks the data dir venv
# before PATH and uvx, will pick up an extra installed there. A source checkout above
# never reads that venv, so it doesn't get the flag
export MELTIFY_LAUNCHER=1

venv_bin="${DATA}/venv/bin/meltify"
# doctor writes the version it installed, so a venv left from an older release is passed over
if [ -x "$venv_bin" ] && [ "$(cat "${DATA}/venv/.meltify-version" 2>/dev/null || true)" = "$VERSION" ]; then
  exec "$venv_bin" "$@"
fi

# command -v may return this launcher or another copy of it when a plugin bin directory is on PATH.
# Asking that copy for --version would itself fall through to uvx
found=$(command -v meltify 2>/dev/null || true)
if [ -n "$found" ] && [ ! "$found" -ef "$0" ] && ! grep -q '^REPO="jeon-jihyeon/meltify"' "$found" 2>/dev/null; then
  have=$("$found" --version 2>/dev/null | awk '{ print $2 }' || true)
  if [ "$have" = "$VERSION" ] || [ "${MELTIFY_ALLOW_PATH:-}" = 1 ]; then
    exec "$found" "$@"
  fi
fi

if command -v uvx >/dev/null 2>&1; then
  spec="meltify"
  if [ -n "${MELTIFY_EXTRAS:-}" ]; then
    spec="meltify[${MELTIFY_EXTRAS}]"
  fi
  if [ "${MELTIFY_FROM_GIT:-}" = 1 ]; then
    exec uvx --quiet --from "${spec} @ git+https://github.com/${REPO}@v${VERSION}" meltify "$@"
  fi
  exec uvx --quiet --from "${spec}==${VERSION}" meltify "$@"
fi

echo "meltify: couldn't find meltify ${VERSION}, and uv isn't installed. Install uv from https://docs.astral.sh/uv/ and try again, or run pip install meltify==${VERSION}" >&2
exit 1
