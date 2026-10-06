from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import mimetypes
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.evidence import USAGE, Envelope, Src, finding
from meltify.needs import error_note
from meltify.output import append_jsonl, read_jsonl

NAME = "submit"
HELP = "submit candidates to a scoring endpoint no faster than its rate limit and keep the best"
COLUMNS = ["ts", "status", "score", "best", "cite"]
RETRY_429 = 10
DEADLINE = "deadline passed"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["once", "run", "status"])
    p.add_argument("file", nargs="?", type=Path, help="candidate for once")
    p.add_argument(
        "--queue", type=Path, help="folder that run watches (default: meltify-out/submit/queue)"
    )
    p.add_argument("--url", help="scoring endpoint")
    p.add_argument("--gap", type=float, help="minimum seconds between submissions")
    p.add_argument("--mode", choices=["multipart", "json", "raw"], help="request body shape")
    p.add_argument("--field", help="multipart field name")
    p.add_argument("--score-path", help="dotted path of the score in the JSON response")
    p.add_argument("--goal", choices=["max", "min"], help="whether a higher score is better")
    p.add_argument("--until-score", type=float, help="stop run once the best reaches this")
    p.add_argument("--max-attempts", type=int, help="stop run after this many submissions")
    p.add_argument("--deadline", help="stop run at this local time, like 2026-10-31T14:50")
    p.add_argument(
        "--idle-exit", type=float, help="stop run after this many seconds with an empty queue"
    )


def dig(data: Any, path: str) -> Any:
    for part in path.split(".") if path else []:
        if isinstance(data, list) and part.lstrip("-").isdigit():
            if not -len(data) <= int(part) < len(data):
                return None
            data = data[int(part)]
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


def retry_after(value: str | None, now: float, fallback: float) -> float:
    """Seconds to wait from a Retry-After header, given as seconds or an HTTP date"""
    if not value:
        return fallback
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError):
            return fallback
    return max(seconds, 0.0) if math.isfinite(seconds) else fallback


@dataclass
class Policy:
    url: str
    gap: float
    mode: str
    field: str
    auth_env: str
    score_path: str
    goal: str
    until_score: float | None
    max_attempts: int
    deadline: float | None
    idle_exit: float
    max_errors: int

    def better(self, a: float | None, b: float | None) -> bool:
        if a is None:
            return False
        return b is None or (a > b if self.goal == "max" else a < b)

    def reached(self, best: float | None) -> bool:
        if self.until_score is None or best is None:
            return False
        return best >= self.until_score if self.goal == "max" else best <= self.until_score


@dataclass
class Submitter:
    policy: Policy
    log: Path
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    post: Callable[..., Any] | None = None
    say: Callable[[str], None] = lambda m: print(m, file=sys.stderr, flush=True)
    attempts: list[dict[str, Any]] = field(init=False)

    def __post_init__(self) -> None:
        self.attempts = read_jsonl(self.log)

    @property
    def last_epoch(self) -> float:
        # Restored from the log, so a restart never fires inside the previous gap
        return max((a["epoch"] for a in self.attempts), default=0.0)

    @property
    def best(self) -> dict[str, Any] | None:
        best = None
        for a in self.attempts:
            if self.policy.better(a.get("score"), best and best.get("score")):
                best = a
        return best

    def seen(self, sha: str) -> bool:
        return any(a["sha"] == sha for a in self.attempts)

    def _wait(self, seconds: float) -> bool:
        """Sleep without passing the deadline and report whether any time is left"""
        deadline = self.policy.deadline
        if seconds > 0:
            self.sleep(seconds if deadline is None else max(min(seconds, deadline - self.now()), 0))
        return deadline is None or self.now() < deadline

    @staticmethod
    def _archive(path: Path, sha: str, sent: Path) -> Path:
        # A later candidate may share the name, so never replace anything in sent
        dest = sent / f"{sha[:12]}_{path.name}"
        n = 1
        while dest.exists():
            n += 1
            dest = sent / f"{sha[:12]}_{n}_{path.name}"
        return path.rename(dest)

    def _request(self, path: Path) -> Any:
        import httpx

        headers = {}
        if self.policy.auth_env and os.environ.get(self.policy.auth_env):
            headers["Authorization"] = f"Bearer {os.environ[self.policy.auth_env]}"
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        post = self.post or httpx.post
        if self.policy.mode == "json":
            return post(self.policy.url, json=json.loads(data), headers=headers, timeout=120)
        if self.policy.mode == "raw":
            return post(
                self.policy.url,
                content=data,
                headers={**headers, "Content-Type": mime},
                timeout=120,
            )
        return post(
            self.policy.url,
            files={self.policy.field: (path.name, data, mime)},
            headers=headers,
            timeout=120,
        )

    def submit(self, path: Path, sent: Path | None = None) -> dict[str, Any]:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if self.seen(sha):
            if sent is not None:
                path = self._archive(path, sha, sent)
            return {"file": str(path), "sha": sha, "status": "skipped duplicate", "score": None}
        wait = self.policy.gap - (self.now() - self.last_epoch)
        if wait > 0:
            self.say(f"waiting {wait:.0f}s for the rate limit")
        if not self._wait(wait):
            return {"file": str(path), "sha": sha, "status": DEADLINE, "score": None}
        status, response, score = "error", None, None
        try:
            for _ in range(RETRY_429):
                try:
                    r = self._request(path)
                except Exception as e:  # noqa: BLE001
                    response = error_note(e, 300)
                    break
                if r.status_code == 429:
                    status = "http 429"
                    retry = retry_after(r.headers.get("Retry-After"), self.now(), self.policy.gap)
                    self.say(f"429 from the endpoint, retrying in {retry:.0f}s")
                    if not self._wait(retry):
                        response = f"{DEADLINE} while waiting for Retry-After"
                        break
                    continue
                try:
                    response = r.json()
                except ValueError:
                    response = r.text[:500]
                if r.status_code < 400:
                    status = "ok"
                    raw = (
                        dig(response, self.policy.score_path)
                        if isinstance(response, dict | list)
                        else None
                    )
                    # bool is an int subclass, but true isn't a score
                    ok = isinstance(raw, int | float) and not isinstance(raw, bool)
                    score = float(raw) if ok else None
                else:
                    status = f"http {r.status_code}"
                break
        except Exception as e:  # noqa: BLE001
            status, response = "error", error_note(e, 300)
        finally:
            # Log even when interrupted. The endpoint already saw the request,
            # and a restart must keep the gap from it
            if sent is not None:
                with contextlib.suppress(OSError):
                    path = self._archive(path, sha, sent)
            record = {
                "ts": datetime.fromtimestamp(self.now()).isoformat(timespec="seconds"),
                "epoch": self.now(),
                "file": str(path),
                "sha": sha,
                "status": status,
                "score": score,
                "response": response,
            }
            append_jsonl(self.log, [record])
            self.attempts.append(record)
        best = self.best
        lead = f"best={best['score']} ({Path(best['file']).name})" if best else "best=None"
        self.say(f"{record['ts']} {path.name} {status} score={score} {lead}")
        return record

    def run(self, queue: Path, sent: Path) -> str:
        queue.mkdir(parents=True, exist_ok=True)
        sent.mkdir(parents=True, exist_ok=True)
        submitted = errors = 0
        idle_since = self.now()
        while True:
            if self.policy.reached(self.best and self.best.get("score")):
                return "reached until_score"
            if self.policy.max_attempts and submitted >= self.policy.max_attempts:
                return "reached max_attempts"
            if self.policy.deadline and self.now() >= self.policy.deadline:
                return DEADLINE
            if errors >= self.policy.max_errors:
                return f"{errors} errors in a row"
            files = sorted(
                (p for p in queue.iterdir() if p.is_file() and not p.name.startswith(".")),
                key=lambda p: p.stat().st_mtime,
            )
            if not files:
                if self.policy.idle_exit and self.now() - idle_since >= self.policy.idle_exit:
                    return "queue idle"
                self.sleep(1)
                continue
            idle_since = self.now()
            record = self.submit(files[0], sent)
            if record["status"] == DEADLINE:
                return DEADLINE
            if record["status"] == "skipped duplicate":
                continue
            submitted += 1
            errors = 0 if record["status"] == "ok" else errors + 1


def _policy(args: argparse.Namespace, conf: dict[str, Any]) -> Policy:
    def pick(cli: Any, key: str) -> Any:
        return cli if cli is not None else conf.get(key)

    until = pick(args.until_score, "until_score")
    deadline = pick(args.deadline, "deadline")
    return Policy(
        url=pick(args.url, "url") or "",
        gap=float(pick(args.gap, "gap")),
        mode=pick(args.mode, "mode"),
        field=pick(args.field, "field"),
        auth_env=conf.get("auth_env", ""),
        score_path=pick(args.score_path, "score_path"),
        goal=pick(args.goal, "goal"),
        until_score=float(until) if until not in (None, "") else None,
        max_attempts=int(pick(args.max_attempts, "max_attempts") or 0),
        deadline=datetime.fromisoformat(deadline).timestamp() if deadline else None,
        idle_exit=float(pick(args.idle_exit, "idle_exit") or 0),
        max_errors=int(conf.get("max_errors", 5)),
    )


def _row(a: dict[str, Any], best: dict[str, Any] | None) -> dict[str, Any]:
    return finding(
        Src(a["file"]),
        ts=a.get("ts"),
        status=a["status"],
        score=a.get("score"),
        best=best is not None and a.get("sha") == best.get("sha"),
        response=a.get("response"),
    )


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    env = Envelope(command=NAME, version=__version__)
    base = Path(settings["out_dir"]) / "submit"
    policy = _policy(args, settings.get("submit", {}))
    s = Submitter(policy, base / "attempts.jsonl")
    env.artifact(str(s.log), "results")

    if args.action == "status":
        best = s.best
        env.results = [_row(a, best) for a in s.attempts]
        lead = f"best {best['score']} from {best['file']}" if best else "no scored attempt yet"
        env.summary = f"{len(s.attempts)} attempts, {lead}"
        return env
    if not policy.url:
        env.error(USAGE, "no endpoint, pass --url or set submit.url in meltify.toml")
        return env
    if args.action == "once":
        if args.file is None:
            env.error(USAGE, "once needs a FILE")
            return env
        record = s.submit(args.file)
        env.inputs.append({"path": str(args.file)})
        env.results = [_row(record, s.best)]
    else:
        queue = args.queue or base / "queue"
        try:
            reason = s.run(queue, base / "sent")
        except KeyboardInterrupt:
            reason = "interrupted"
        best = s.best
        env.results = [_row(a, best) for a in s.attempts]
        env.summary = f"stopped: {reason}"
    best = s.best
    env.summary = (env.summary + ". " if env.summary else "") + (
        f"best {best['score']} from {best['file']}" if best else "no scored attempt yet"
    )
    return env
