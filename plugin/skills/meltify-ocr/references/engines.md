# OCR engines

| Engine | Runs | Positions | Notes |
|---|---|---|---|
| `vision` | macOS, local, free | per line | Apple Vision through ocrmac. The configured language goes first, because macOS 27 applies only the first language |
| `paddle` | local, free | per line | PaddleOCR 3. Needs `meltify doctor --install ocr-paddle`, and downloads its models on first use |
| `gemini` | paid API | per tile | Strongest on Korean documents in published benchmarks. Needs `GEMINI_API_KEY` |
| `claude` | paid API | per tile | Needs `ANTHROPIC_API_KEY` |
| `openai` | paid API | per tile | Any OpenAI-compatible endpoint through `llm.openai_base_url`. Needs `OPENAI_API_KEY` |
| `--reading NAME=FILE` | none | none | Lines read elsewhere, such as by an agent viewing the prepared image |

- `auto` picks the installed local engines and never a paid one
- For LLM engines, large images are cut into overlapping tiles of at most `ocr.tile_max` px, because vision APIs shrink anything bigger
- Models and key variable names come from `meltify.toml`

```toml
lang = "ko"

[ocr]
engines = "vision,gemini"
upscale = 3
dpi = 300

[llm]
gemini_model = "gemini-3-pro"
```
