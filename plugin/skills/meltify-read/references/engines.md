# OCR engines

| Engine | Runs | Positions | Notes |
|---|---|---|---|
| `vision` | macOS, local, free | per line | Apple Vision through ocrmac. The configured language goes first, because Apple Vision on macOS 27 applies only the first language |
| `gemini` | paid API | per tile | Needs `GEMINI_API_KEY` |
| `claude` | paid API | per tile | Needs `ANTHROPIC_API_KEY` |
| `openai` | paid API | per tile | Any OpenAI-compatible endpoint through `llm.openai_base_url`. Needs `OPENAI_API_KEY` |
| endpoint `NAME` | served by the user, free | per tile | A model the user serves, from `[ocr.endpoints.NAME]` with `base_url` and `model`. Only loopback and private addresses qualify. Counts as an LLM, so it needs a local engine to agree with |
| `--reading NAME=FILE` | none | none | Lines read elsewhere, such as by an agent viewing the image itself |

- `auto` picks the installed local engines and every endpoint, and never a paid one
- For LLM engines, large images are cut into overlapping tiles of at most `ocr.tile_max` px, because vision APIs shrink anything bigger
- Models and key variable names come from `meltify.toml`

```toml
lang = "ko"

[ocr]
engines = "vision,gemini"
# 0 sizes each picture on its own
upscale = 0
dpi = 300

[llm]
gemini_model = "gemini-3-pro"
```
