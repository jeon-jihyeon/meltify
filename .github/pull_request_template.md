## What changed

## Why

## Checks

- [ ] `uv run pytest -m "not llm and not heavy"` passes
- [ ] `uv run ruff check . && uv run ruff format --check .` passes
- [ ] Every new result row comes from `evidence.finding`, so it has `src` and `cite`
- [ ] README, SECURITY.md or a skill is updated if behavior changed
- [ ] CHANGELOG.md has a line for any change a user would notice
