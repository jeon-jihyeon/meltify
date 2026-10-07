"""Speech-to-text engines behind one interface"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from meltify import lang as langs
from meltify import llm
from meltify.safe import MissingTool, run


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


class Engine(Protocol):
    name: str

    def missing(self) -> str | None: ...

    def transcribe(self, audio: Path, lang: str) -> list[Segment]: ...


@dataclass
class Mlx:
    model: str
    name: str = "mlx"

    def missing(self) -> str | None:
        if sys.platform != "darwin" or platform.machine() != "arm64":
            return "MLX needs Apple Silicon"
        return (
            None if importlib.util.find_spec("mlx_whisper") else "meltify doctor --install asr-mlx"
        )

    def transcribe(self, audio: Path, lang: str) -> list[Segment]:
        import mlx_whisper

        r = mlx_whisper.transcribe(
            str(audio), path_or_hf_repo=self.model, language=langs.whisper(lang)
        )
        return [
            Segment(round(s["start"], 2), round(s["end"], 2), s["text"].strip())
            for s in r["segments"]
        ]


@dataclass
class WhisperCpp:
    model: str
    name: str = "whispercpp"

    def missing(self) -> str | None:
        if shutil.which("whisper-cli") is None:
            return "brew install whisper-cpp"
        return (
            None
            if self.model and Path(self.model).is_file()
            else "set asr.whispercpp_model to a ggml model file"
        )

    def transcribe(self, audio: Path, lang: str) -> list[Segment]:
        prefix = audio.with_suffix("")
        args = ["whisper-cli", "-m", self.model, "-f", str(audio), "-oj", "-of", str(prefix)]
        if (code := langs.whisper(lang)) is not None:
            args += ["-l", code]
        run(args)
        data = json.loads(prefix.with_suffix(".json").read_text("utf-8"))
        return [
            Segment(s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip())
            for s in data.get("transcription", [])
        ]


@dataclass
class Api:
    """Any OpenAI-compatible transcription endpoint"""

    model: str
    key_env: str
    base_url: str
    name: str = "api"

    def missing(self) -> str | None:
        return None if os.environ.get(self.key_env) else f"export {self.key_env}=..."

    def transcribe(self, audio: Path, lang: str) -> list[Segment]:
        import httpx

        data: dict[str, Any] = {"model": self.model, "response_format": "verbose_json"}
        if (code := langs.whisper(lang)) is not None:
            data["language"] = code
        with audio.open("rb") as f:
            r = httpx.post(
                f"{self.base_url.rstrip('/')}/audio/transcriptions",
                data=data,
                files={"file": (audio.name, f, "audio/wav")},
                headers={"Authorization": f"Bearer {llm.key(self.key_env)}"},
                timeout=600,
            )
        r.raise_for_status()
        body = r.json()
        segments = body.get("segments") or [{"start": 0, "end": 0, "text": body.get("text", "")}]
        return [Segment(float(s["start"]), float(s["end"]), s["text"].strip()) for s in segments]


def engines(settings: dict[str, Any]) -> dict[str, Engine]:
    conf = settings["asr"]
    llm_conf = settings["llm"]
    return {
        "mlx": Mlx(conf["model"]),
        "whispercpp": WhisperCpp(conf["whispercpp_model"]),
        "api": Api(conf["api_model"], llm_conf["openai_key_env"], llm_conf["openai_base_url"]),
    }


def select(spec: str, settings: dict[str, Any]) -> Engine:
    candidates = engines(settings)
    if spec != "auto":
        engine = candidates.get(spec)
        if engine is None:
            raise ValueError(f"unknown ASR engine {spec}, use mlx, whispercpp or api")
        if (why := engine.missing()) is not None:
            raise MissingTool(spec, why)
        return engine
    # Never pick the paid API automatically
    for name in ("mlx", "whispercpp"):
        if candidates[name].missing() is None:
            return candidates[name]
    raise MissingTool(
        "speech engine",
        "meltify doctor --install asr-mlx on Apple Silicon, brew install whisper-cpp, or --asr api",
    )
