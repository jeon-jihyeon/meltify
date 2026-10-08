"""Speech-to-text engines behind one interface"""

from __future__ import annotations

import json
import os
import shutil
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
    """Any OpenAI-compatible transcription endpoint, hosted or served yourself"""

    model: str
    key_env: str
    base_url: str
    name: str = "api"
    # Set for a model you serve yourself, which auto may pick
    endpoint: llm.Endpoint | None = None

    def missing(self) -> str | None:
        if self.endpoint is not None:
            return self.endpoint.missing()
        return None if os.environ.get(self.key_env) else f"export {self.key_env}=..."

    def transcribe(self, audio: Path, lang: str) -> list[Segment]:
        data: dict[str, Any] = {"model": self.model, "response_format": "verbose_json"}
        if (code := langs.whisper(lang)) is not None:
            data["language"] = code
        ep = self.endpoint
        api_key = llm.key(self.key_env) if ep is None else ep.api_key
        with audio.open("rb") as f:
            r = llm.post(
                f"{self.base_url.rstrip('/')}/audio/transcriptions",
                data=data,
                files={"file": (audio.name, f, "audio/wav")},
                headers=llm.bearer(api_key),
                timeout=SPEECH_TIMEOUT if ep is None else ep.timeout,
            )
        body = r.json()
        segments = body.get("segments") or [{"start": 0, "end": 0, "text": body.get("text", "")}]
        return [Segment(float(s["start"]), float(s["end"]), s["text"].strip()) for s in segments]


# A long recording takes minutes to transcribe in one request
SPEECH_TIMEOUT = 600.0
BUILT_IN = ("whispercpp", "api")
INSTALL_HINT = (
    "brew install whisper-cpp, serve a speech model behind an OpenAI-compatible endpoint "
    "and add it under [asr.endpoints.NAME], or --asr api"
)


def endpoints(settings: dict[str, Any]) -> dict[str, llm.Endpoint]:
    """The speech models you serve yourself, by the name you gave each"""
    # A transcription request has no prompt to set
    fields = {k: v for k, v in llm.FIELDS.items() if k != "prompt"}
    return llm.endpoints("asr", settings["asr"]["endpoints"], set(BUILT_IN), SPEECH_TIMEOUT, fields)


def engines(settings: dict[str, Any]) -> dict[str, Engine]:
    conf = settings["asr"]
    llm_conf = settings["llm"]
    found: dict[str, Engine] = {
        "whispercpp": WhisperCpp(conf["whispercpp_model"]),
        "api": Api(conf["api_model"], llm_conf["openai_key_env"], llm_conf["openai_base_url"]),
    }
    for name, ep in endpoints(settings).items():
        found[name] = Api(ep.model, ep.key_env, ep.base_url, name, ep)
    return found


def select(spec: str, settings: dict[str, Any], down: frozenset[str] = frozenset()) -> Engine:
    """The engine `spec` names, or for auto the first one ready, passing over those `down`"""
    candidates = engines(settings)
    if spec != "auto":
        engine = candidates.get(spec)
        if engine is None:
            raise ValueError(f"unknown ASR engine {spec}, use {', '.join(candidates)}")
        if spec in down:
            raise MissingTool(spec, "its server stopped answering earlier in this run")
        if (why := engine.missing()) is not None:
            raise MissingTool(spec, why)
        return engine
    # Never pick the paid API automatically. A model you serve yourself was set up on purpose,
    # so endpoints go first, in the order you listed them
    for name in (*endpoints(settings), "whispercpp"):
        if name not in down and candidates[name].missing() is None:
            return candidates[name]
    raise MissingTool("speech engine", INSTALL_HINT)
