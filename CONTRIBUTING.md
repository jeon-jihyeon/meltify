# Contributing

## Setup

```
uv sync
uv run pytest -m "not llm and not heavy"
uv run ruff check . && uv run ruff format --check .
```

After you change the version in `pyproject.toml` or the launcher template, run `uv run python scripts/sync_version.py`. CI fails when the plugin manifest, the launcher copies or a skill's metadata drift from it.

## Layout

- `src/meltify/commands/`: one module per command, each with `NAME`, `HELP`, `COLUMNS`, `add_arguments` and `run`
- `src/meltify/engines/`: OCR and speech engines behind one interface
- `src/meltify/converters/`: one converter per input format for `read`
- `plugin/skills/`: one skill per command. Frontmatter uses only the Agent Skills standard fields
- `scripts/launcher.sh`: the single source for the plugin launcher and every skill's `scripts/run`

## Rules

- Build every result row with `evidence.finding`, so it carries `src` and `cite`
- Keep heavy imports inside `run`, so `meltify --help` starts fast
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
