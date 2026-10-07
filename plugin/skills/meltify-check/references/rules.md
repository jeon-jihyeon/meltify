# Check rules

## Flags

| Flag | Meaning |
|---|---|
| `--schema FILE` | JSON Schema, draft 2020-12 |
| `--count N` | exact number of top-level items |
| `--unique KEY` | key that must not repeat across items, repeatable |
| `--field PATH` | JSONPath the text rules apply to, repeatable |
| `--pattern RE` | regex the whole value must match |
| `--words N` | exact word count |
| `--max-words N` | maximum word count |
| `--upper` | no lowercase letters |
| `--digits` | ASCII digits only |
| `--include TEXT` | phrase the value must contain, repeatable |
| `--no-hygiene` | skip whitespace and width checks |

JSONPath supports `$`, `.key`, `[n]`, `[-1]` and `[*]`.

Answers can be JSON, JSONL (one item per line) or CSV (one item per row, keyed by the header), so `--count`, `--unique` and paths like `$[*].id` work the same on all three.

## Cross-field rules

Write them in the schema with `if` and `then`:

```json
{
  "items": {
    "if": {"properties": {"answer": {"const": "Approve"}}},
    "then": {"properties": {"reason": {"type": "null"}}}
  }
}
```

## meltify.toml

```toml
[check]
schema = "answer.schema.json"
count = 30
unique = ["id"]

[[check.field]]
path = "$.q2"
pattern = "[A-Z]+"

[[check.field]]
path = "$.summary"
include = ["as of 2025-07-21"]
max_words = 300
```

Each `[[check.field]]` table takes the same rules as the flags:

| Key | Same as |
|---|---|
| `path` | `--field`, `$` when left out |
| `pattern` | `--pattern` |
| `words` | `--words` |
| `max_words` | `--max-words` |
| `upper` | `--upper` |
| `digits` | `--digits` |
| `include` | `--include`, as a list |

A misspelled key inside a field table is ignored without a warning, so a rule you meant to set may never run. Check the spelling against this table.

Project rules apply to files. `--text` uses only its own flags.
