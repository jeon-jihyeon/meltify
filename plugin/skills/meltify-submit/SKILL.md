---
name: meltify-submit
description: Submit candidate files to a scoring or feedback endpoint no faster than its rate limit, skip exact duplicates, retry on 429 and keep track of the best score. Use when an endpoint scores submissions and limits how often it can be called, such as one request per minute, or when trying many candidates against a grader.
license: MIT
compatibility: Needs uv or meltify on PATH. macOS or Linux.
metadata:
  version: "0.2.3"
  cli: meltify submit
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Write
---

# meltify submit

Don't waste a rate-limited slot on a duplicate or an early call, and always know the best attempt so far.

## Run

1. Read the endpoint's rules first: the URL, the minimum gap, the request field and where the score sits in the response. Put them in `meltify.toml` under `[submit]`, or pass them as flags.
2. One candidate: `meltify submit once candidate.png --url URL --gap 61 --score-path result.similarity`
   - It waits until the gap since the last recorded attempt has passed, then submits and returns the response
3. Many candidates: start `meltify submit run --until-score 0.95 --deadline 2026-10-31T14:50` in the background, then keep writing new candidates into `meltify-out/submit/queue/`
   - It stops on `--until-score`, `--max-attempts`, `--deadline`, `--idle-exit` seconds of empty queue, or repeated errors
4. `meltify submit status` lists every attempt and marks the best.
5. If `meltify` isn't on PATH, run this skill's `scripts/run submit ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

## Read the output

- Every attempt is logged in `meltify-out/submit/attempts.jsonl` with its time, file, sha256, status, score and raw response
- The gap is restored from that log, so a restart never fires early
- A file whose bytes were already submitted is skipped, not sent

## Gotchas

- Duplicates are detected by exact bytes. A re-saved or re-encoded copy of the same image counts as new and spends a slot.
- The gap and the duplicate record live in `attempts.jsonl` under the out dir. A different `--out` or a deleted log forgets earlier attempts and can fire too early.
- Never run two submitters against the same endpoint.

## Then

1. Read each response for feedback before you make the next candidate.
2. Report the best score with its file and the number of attempts used.
3. Respect the endpoint's terms.
