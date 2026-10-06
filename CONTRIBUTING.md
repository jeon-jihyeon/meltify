# Contributing

## Setup

```
uv sync
uv run pytest -m "not llm and not heavy"
uv run ruff check . && uv run ruff format --check .
```

Run the full suite before you send a change, not just the tests next to your edit. Converters share limits, picture handling and run state, so a fix in one often shows up in another.

CI runs the suite a second time with the common extras, so their converters get covered too. Do the same before you touch one:

```
uv sync --extra office --extra archive --extra parquet --extra crypto
uv run pytest -m "not llm and not heavy"
```

After you change the version in `pyproject.toml` or the launcher template, run `uv run python scripts/sync_version.py`. CI fails when the plugin manifest, the launcher copies or a skill's metadata drift from it.

## Layout

- `src/meltify/commands/`: one module per command, each with `NAME`, `HELP`, `COLUMNS`, `add_arguments` and `run`
- `src/meltify/engines/`: OCR and speech engines behind one interface
- `src/meltify/converters/`: one converter per input format for `read`, plus the pieces they share:
  - `run`: what one `read` run decides, like its password, `--shallow` and the fallback switch
  - `limits` and `xmlsafe`: size, ratio and entity limits for untrusted containers
  - `embeds`: pictures, charts and diagrams a document embeds, under one picture total
  - `ooxml` and `ooxml_charts`: OOXML packages, and their charts and SmartArt as cited text
  - `blips`: the drawing records and picture store Word and PowerPoint binaries share, read by `doc` and `ppt`
  - `tables`: markdown tables a reader can cite cell by cell
  - `fallback`: the last resort for binary files no converter reads
- `src/meltify/needs.py` and `src/meltify/passwords.py`: the wording of `needs` lines, so every converter reports the same gap the same way
- `src/meltify/safe.py`: the subprocess runner that kills a whole process group on timeout
- `tests/fixtures/`: generators for the test inputs. `make_binary.py` builds compound files, WMF pictures and encrypted workbooks, since the libraries meltify uses only read them
- `plugin/skills/`: one skill per command. Frontmatter uses only the Agent Skills standard fields
- `scripts/launcher.sh`: the single source for the plugin launcher and every skill's `scripts/run`

## Rules

- Build every result row with `evidence.finding`, so it carries `src` and `cite`
- Keep heavy imports inside `run`, so `meltify --help` starts fast
- Converters read run state through `run.current()`, never module globals. Each worker thread runs in a copy of the run's context, so two runs in one process never see each other's state
- Reuse the wording in `needs.py` and `passwords.py` for any gap they already cover, like a missing LibreOffice or a locked file
- Paid engines never run unless they're named
- Mark tests that call a paid API as `llm`. They run only with `MELTIFY_TEST_LLM=1`
- Write commit messages as `type: what changed`, where type is feat, fix, docs, refactor, test or chore

## Writing style

Write comments, docs, skills and messages the way you'd explain things to a teammate: plain, direct American English.

- Comments explain why or a constraint, never what the next line does. One-line comments skip the trailing period
- Use normal punctuation. Commas, hyphens (`prompt-ready`, `rate-limited`) and contractions are welcome. Skip em-dashes
- In docs, talk to the reader as "you". In skills, give the agent short imperatives ("Run", "Quote", "Don't guess")
- Help strings and errors start lowercase, end without a period and say what to try next
- When a skill's `description` changes, keep every "use when" condition, since those decide when the skill triggers
