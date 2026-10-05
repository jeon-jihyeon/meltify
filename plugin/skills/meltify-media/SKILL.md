---
name: meltify-media
description: Turn a video, screen recording, phone call recording or video URL into timestamped full-resolution frames and a timestamped transcript from subtitles or local speech recognition. Use whenever an answer depends on what's shown, said or changed in a video or recording, such as on-screen details, counts, scene changes, exact quotes or when something happens, instead of running ffmpeg or ffprobe by hand.
license: MIT
compatibility: Needs uv or meltify on PATH and ffmpeg. URLs need the media extra and deno for YouTube. Speech recognition needs the asr-mlx extra on Apple Silicon, whisper-cpp or an API key.
metadata:
  version: "0.2.0"
  cli: meltify media
allowed-tools: Bash(meltify *) Bash(${CLAUDE_SKILL_DIR}/scripts/run *) Read Grep
---

# meltify media

You can't watch video. This turns it into frames and text, with a time on every item.

When recordings sit in a folder with other files, the meltify-read skill already transcribes them and OCRs up to 20 scene frames each. Use this skill for focused work on one video: full-resolution frames to look at, a time window, denser frames or captions only.

## Run

1. Run `meltify media clip.mp4` or `meltify media "https://www.youtube.com/watch?v=..."`.
2. Narrow long videos with `--start 600 --end 780`, in seconds. Find the window first from the transcript or a lower `--fps`.
3. Use `--no-frames` for speech only, `--subs-only` for captions only, `--fps 2` for denser frames, and `--asr mlx` to force local speech recognition.
4. If `meltify` isn't on PATH, run this skill's `scripts/run media ...` by its full path. In Claude Code that's `${CLAUDE_SKILL_DIR}/scripts/run`.

## Read the output

- `speech` rows carry the text and a cite such as `clip.mp4@00:01:23.4-00:01:27.0`, and say whether it came from subtitles or the speech engine
- `frame` rows carry a JPEG path at the original resolution and a cite with its time. `scene` marks a scene change and `interval` a regular sample
- Near-identical consecutive frames are skipped and counted in the summary. Add `--keep-duplicates` to list them
- `transcript.txt` holds the whole transcript with times

## Gotchas

- Consecutive frames with a near-identical layout and brightness are skipped. A small change, like one digit of a counter, can get skipped along with them. Add `--keep-duplicates` when you're counting or comparing small details.
- At the default 1 fps, frames miss anything shorter than a second. For fast events, narrow the window and raise `--fps`.

## Then

1. Grep the transcript to find when something is said, then open only the frames near that time with Read.
2. For text in a frame, run the meltify-ocr skill on that frame instead of reading it once by eye.
3. Cite the time for every fact you take from the media.
