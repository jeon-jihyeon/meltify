from __future__ import annotations

import argparse
import json
import re
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.evidence import USAGE, Envelope, Src, finding

NAME = "brief"
HELP = "quote a problem statement's conditions with line numbers, and track several problems"
COLUMNS = ["kinds", "quote", "cite"]
BOARD_COLUMNS = ["problem", "state", "points", "checks", "idle_min", "answer"]

# Words that mark a sentence a solver shouldn't paraphrase, in English and Korean
KINDS: dict[str, re.Pattern[str]] = {
    "count": re.compile(
        r"\d+\s*(연승|연속|회|번|times?|in a row|consecutive|wins?|attempts?|items?|개|명|건)"
        r"|at least|at most|exactly|최소|최대|이상|이하|미만|초과",
        re.I,
    ),
    "date": re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}|기준일|as of|deadline|마감|기한|due", re.I),
    "timezone": re.compile(r"\b(KST|UTC|GMT|PST|EST)\b|[+-]\d{2}:\d{2}|시간대|time ?zone", re.I),
    "format": re.compile(
        r"JSON|CSV|대문자|소문자|upper ?case|lower ?case|숫자만|digits? only|단어|words?\b"
        r"|형식|format|쉼표|comma|소수점|decimal|템플릿|template|띄어쓰기|spaces?\b",
        re.I,
    ),
    "priority": re.compile(
        r"우선|priority|lowest|highest|가장 낮은|가장 높은|먼저|first"
        r"|여러 개|multiple|if more than",
        re.I,
    ),
    "limit": re.compile(
        r"per (minute|second|hour)|분당|초당|\d+\s*분에|rate limit|제한"
        r"|한 번만|only once|duplicate|중복",
        re.I,
    ),
    "prohibition": re.compile(
        r"금지|must not|do not|don't|never|않|없이|추측|guess|assume|불가", re.I
    ),
}


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["extract", "new", "board", "set"])
    p.add_argument("target", nargs="?", help="statement file for extract, number for new and set")
    p.add_argument("title", nargs="?", help="title for new")
    p.add_argument("--from", dest="source", type=Path, help="statement file for new")
    p.add_argument("--state", help="state for set, like todo, doing, done or skipped")
    p.add_argument("--points", type=float, help="points for set")
    p.add_argument("--answer", help="answer note for set")
    p.add_argument("--force", action="store_true", help="rewrite an existing problem's NOTES.md")


def extract(text: str, origin: str) -> list[dict[str, Any]]:
    rows = []
    for n, line in enumerate(text.splitlines(), start=1):
        quote = line.strip()
        if not quote:
            continue
        kinds = [k for k, rx in KINDS.items() if rx.search(quote)]
        if kinds:
            rows.append(finding(Src(origin, line=n), kinds=kinds, quote=quote))
    return rows


def _slug(title: str) -> str:
    return re.sub(r"[^\w]+", "-", title).strip("-")[:40] or "problem"


def _problem_dirs(root: Path, number: str) -> list[Path]:
    prefix = f"{int(number):02d}-"
    return [d for d in sorted(root.glob(f"{prefix}*")) if d.is_dir()]


def _new(d: Path, number: str, title: str, source: Path | None, body: str) -> None:
    statement = source.read_text("utf-8") if source else ""
    conditions = extract(statement, str(source)) if source else []
    listed = "\n".join(f"- [ ] {r['quote']}  `{r['cite']}`" for r in conditions)
    notes = body.format(
        number=number, title=title, statement=statement.strip(), conditions=listed or "-"
    )
    (d / "data").mkdir(parents=True, exist_ok=True)
    (d / "NOTES.md").write_text(notes, encoding="utf-8")
    status = d / "status.json"
    if not status.exists():
        status.write_text(json.dumps({"state": "todo", "points": 0, "answer": None}), "utf-8")


def _board(dirs: list[Path], now: float) -> list[dict[str, Any]]:
    rows = []
    for d in dirs:
        status = json.loads((d / "status.json").read_text("utf-8"))
        notes = (d / "NOTES.md").read_text("utf-8")
        done, todo = notes.count("- [x]"), notes.count("- [ ]")
        newest = max(f.stat().st_mtime for f in d.rglob("*") if f.is_file())
        rows.append(
            finding(
                Src(str(d / "NOTES.md")),
                problem=d.name,
                state=status.get("state"),
                points=status.get("points"),
                checks=f"{done}/{done + todo}",
                idle_min=int((now - newest) / 60),
                answer=status.get("answer"),
            )
        )
    return rows


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    env = Envelope(command=NAME, version=__version__)
    conf = settings.get("brief", {})
    root = Path(conf.get("root", "problems"))

    if args.action == "extract":
        if not args.target:
            env.error(USAGE, "extract needs a statement file")
            return env
        path = Path(args.target)
        env.inputs.append({"path": str(path)})
        env.results = extract(path.read_text("utf-8"), str(path))
        env.summary = (
            f"{len(env.results)} condition lines. Quote them as written and check each one"
        )
        return env

    if args.action == "new":
        if not (args.target and args.title):
            env.error(USAGE, "new needs a number and a title")
            return env
        existing = _problem_dirs(root, args.target)
        if existing and not args.force:
            env.error(
                USAGE,
                f"problem {args.target} already exists at {existing[0]}, add --force to rewrite it",
            )
            return env
        if len(existing) > 1:
            env.error(USAGE, f"problem {args.target} matches {len(existing)} folders under {root}")
            return env
        template = files("meltify.data").joinpath(f"brief_{conf.get('template', 'en')}.md")
        if not template.is_file():
            env.error(USAGE, f"no brief template {conf.get('template')!r}, use en or ko")
            return env
        d = existing[0] if existing else root / f"{int(args.target):02d}-{_slug(args.title)}"
        _new(d, args.target, args.title, args.source, template.read_text("utf-8"))
        env.artifact(str(d / "NOTES.md"), "notes")
        env.summary = f"{'rewrote' if existing else 'created'} {d}"
        return env

    if args.action == "set":
        found = _problem_dirs(root, args.target) if args.target else []
        if len(found) != 1:
            what = "no problem" if not found else f"{len(found)} folders for problem"
            env.error(USAGE, f"{what} {args.target} under {root}")
            return env
        status_path = found[0] / "status.json"
        status = json.loads(status_path.read_text("utf-8"))
        for key in ("state", "points", "answer"):
            if (value := getattr(args, key)) is not None:
                status[key] = value
        status_path.write_text(json.dumps(status, ensure_ascii=False), "utf-8")

    dirs = sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []
    complete = []
    for d in dirs:
        missing = [n for n in ("status.json", "NOTES.md") if not (d / n).is_file()]
        if missing:
            env.warnings.append(f"skipped {d}: no {' and no '.join(missing)}")
        else:
            complete.append(d)
    env.results = _board(complete, time.time())
    env.columns = BOARD_COLUMNS
    env.summary = f"{len(env.results)} problems under {root}"
    return env
