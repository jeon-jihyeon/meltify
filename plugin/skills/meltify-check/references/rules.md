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
must_include = ["as of 2025-07-21"]
max_words = 300
```

Project rules apply to files. `--text` uses only its own flags.
