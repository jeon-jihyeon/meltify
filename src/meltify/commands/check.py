from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meltify import __version__
from meltify.evidence import USAGE, Envelope, Src, finding

NAME = "check"
HELP = "check an answer file or string against its required output format before you hand it in"
COLUMNS = ["rule", "message", "value", "cite"]
VIOLATION = "violation"


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", nargs="?", type=Path, help="JSON, JSONL or CSV answer file")
    p.add_argument("--text", help="check one string instead of a file")
    p.add_argument("--schema", type=Path, help="JSON Schema file")
    p.add_argument("--count", type=int, help="exact number of top-level items")
    p.add_argument("--unique", action="append", help="key that can't repeat across items")
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="PATH",
        help="JSONPath the text rules apply to, like $[*].id (repeatable)",
    )
    p.add_argument("--pattern", help="regex the whole value must match")
    p.add_argument("--words", type=int, help="exact word count")
    p.add_argument("--max-words", type=int, help="maximum word count")
    p.add_argument("--upper", action="store_true", help="no lowercase letters")
    p.add_argument("--digits", action="store_true", help="ASCII digits only")
    p.add_argument("--include", action="append", help="phrase the value must contain")
    p.add_argument("--no-hygiene", action="store_true", help="skip whitespace and width checks")


@dataclass(frozen=True)
class Rule:
    """Text format rules for the values at one JSONPath"""

    path: str = "$"
    pattern: str | None = None
    words: int | None = None
    max_words: int | None = None
    upper: bool = False
    digits: bool = False
    include: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> Rule:
        return cls(
            path=raw.get("path", "$"),
            pattern=raw.get("pattern"),
            words=raw.get("words"),
            max_words=raw.get("max_words"),
            upper=bool(raw.get("upper", False)),
            digits=bool(raw.get("digits", False)),
            include=tuple(raw.get("must_include", raw.get("include", ()))),
        )

    def problems(self, value: Any) -> Iterator[tuple[str, str]]:
        if not isinstance(value, str):
            yield "type", f"expected a string, got {type(value).__name__}"
            return
        # \d also matches full-width digits, so spell out the ASCII class
        if self.digits and not re.fullmatch(r"[0-9]+", value):
            yield "digits", "ASCII digits only"
        if self.upper and value != value.upper():
            yield "upper", "contains lowercase letters"
        if self.pattern and not re.fullmatch(self.pattern, value):
            yield "pattern", f"doesn't fully match {self.pattern}"
        n = len(value.split())
        if self.words is not None and n != self.words:
            yield "words", f"{n} words, expected {self.words}"
        if self.max_words is not None and n > self.max_words:
            yield "max_words", f"{n} words, at most {self.max_words}"
        for phrase in self.include:
            if phrase not in value:
                yield "include", f"missing {phrase!r}"


def hygiene(value: str) -> Iterator[tuple[str, str]]:
    if value != value.strip():
        yield "hygiene", "leading or trailing whitespace"
    if "  " in value:
        yield "hygiene", "double space"
    if unicodedata.normalize("NFKC", value) != value:
        yield "hygiene", "full-width or compatibility characters"


PATH_TOKEN = re.compile(r"\.([^.\[\]]+)|\[(\*|-?\d+)\]")


def path_tokens(path: str) -> list[tuple[str, str]]:
    """Minimal JSONPath: dot keys, [n] and [*]"""
    body = path.removeprefix("$")
    found = list(PATH_TOKEN.finditer(body))
    # Reject syntax like filters, which would otherwise select nothing and look like missing data
    if not path.startswith("$") or "".join(m.group(0) for m in found) != body:
        raise ValueError(path)
    return [(m.group(1) or "", m.group(2) or "") for m in found]


def select(data: Any, path: str) -> Iterator[tuple[str, Any]]:
    nodes: list[tuple[str, Any]] = [("$", data)]
    for key, index in path_tokens(path):
        nxt = []
        for p, node in nodes:
            if key and isinstance(node, dict) and key in node:
                nxt.append((f"{p}.{key}", node[key]))
            elif index == "*" and isinstance(node, list):
                nxt.extend((f"{p}[{i}]", v) for i, v in enumerate(node))
            elif index == "*" and isinstance(node, dict):
                nxt.extend((f"{p}.{k}", v) for k, v in node.items())
            elif (
                index
                and index != "*"
                and isinstance(node, list)
                and -len(node) <= int(index) < len(node)
            ):
                nxt.append((f"{p}[{index}]", node[int(index)]))
        nodes = nxt
    return iter(nodes)


def strings(data: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(data, str):
        yield path, data
    elif isinstance(data, dict):
        for k, v in data.items():
            yield from strings(v, f"{path}.{k}")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from strings(v, f"{path}[{i}]")


def load(path: Path) -> Any:
    suffix = path.suffix.lower()
    text = path.read_text("utf-8-sig")
    if suffix == ".csv":
        return list(csv.DictReader(text.splitlines()))
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


@dataclass
class Checker:
    source: str
    rules: list[Rule] = field(default_factory=list)
    schema: dict[str, Any] | None = None
    count: int | None = None
    unique: list[str] = field(default_factory=list)
    check_hygiene: bool = True

    def run(self, data: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def add(rule: str, message: str, jpath: str, value: Any = None) -> None:
            found.append(
                finding(Src(self.source, jpath=jpath), rule=rule, message=message, value=value)
            )

        if self.schema is not None:
            from jsonschema import Draft202012Validator

            for e in Draft202012Validator(self.schema).iter_errors(data):
                add("schema", e.message, e.json_path, e.instance if _scalar(e.instance) else None)
        if not isinstance(data, list):
            # count and unique describe a list of items, so any other shape fails them
            got = type(data).__name__
            if self.count is not None:
                add("count", f"expected a list of items, got {got}", "$")
            if self.unique:
                add("unique", f"expected a list of items, got {got}", "$")
        elif self.count is not None and len(data) != self.count:
            add("count", f"{len(data)} items, expected {self.count}", "$", len(data))
        for key in self.unique:
            # Include the type in the key, since 1 == True in Python
            seen: dict[tuple[type, Any], int] = {}
            for i, item in enumerate(data if isinstance(data, list) else []):
                if not isinstance(item, dict) or key not in item or not _scalar(item[key]):
                    continue
                v = item[key]
                if (type(v), v) in seen:
                    add("unique", f"{key} repeats item {seen[type(v), v]}", f"$[{i}].{key}", v)
                    continue
                seen[type(v), v] = i
        if self.check_hygiene:
            for jpath, s in strings(data):
                for rule, message in hygiene(s):
                    add(rule, message, jpath, s)
        for r in self.rules:
            matched = list(select(data, r.path))
            if not matched:
                add("path", f"nothing at {r.path}", r.path)
            for jpath, value in matched:
                for rule, message in r.problems(value):
                    add(rule, message, jpath, value if _scalar(value) else None)
        return found


def _scalar(v: Any) -> bool:
    return isinstance(v, str | int | float | bool) or v is None


def _has_text_rule(args: argparse.Namespace) -> bool:
    return any(
        [
            args.pattern,
            args.words is not None,
            args.max_words is not None,
            args.upper,
            args.digits,
            args.include,
        ]
    )


def _cli_rule(args: argparse.Namespace, path: str) -> Rule:
    return Rule(
        path=path,
        pattern=args.pattern,
        words=args.words,
        max_words=args.max_words,
        upper=args.upper,
        digits=args.digits,
        include=tuple(args.include or ()),
    )


def run(args: argparse.Namespace, settings: dict[str, Any]) -> Envelope:
    env = Envelope(command=NAME, version=__version__)
    conf = settings.get("check", {})
    if (args.file is None) == (args.text is None):
        env.error(USAGE, "give exactly one of FILE or --text")
        return env

    hygiene_on = conf.get("hygiene", True) and not args.no_hygiene
    if args.field and not _has_text_rule(args):
        env.error(USAGE, "--field needs a text rule, like --pattern or --upper")
        return env
    if args.text is not None:
        ignored = [
            flag
            for flag, value in (
                ("--schema", args.schema),
                ("--count", args.count),
                ("--unique", args.unique),
                ("--field", args.field),
            )
            if value not in (None, [])
        ]
        if ignored:
            env.error(USAGE, f"{', '.join(ignored)} apply to a FILE, not --text")
            return env
        # Project rules describe an answer file, so a single string uses only its own flags
        rules = [_cli_rule(args, "$")] if _has_text_rule(args) else []
    else:
        if _has_text_rule(args) and not args.field:
            env.error(USAGE, "text rules on a file need --field, like $[*].id or $.q2")
            return env
        rules = [Rule.from_config(r) for r in conf.get("field", [])]
        if _has_text_rule(args):
            rules += [_cli_rule(args, p) for p in args.field]
    for r in rules:
        try:
            path_tokens(r.path)
        except ValueError:
            env.error(USAGE, f"unsupported path {r.path}, use $ with .key, [n], [-1] and [*]")
            return env
        try:
            re.compile(r.pattern or "")
        except re.error as e:
            env.error(USAGE, f"bad pattern {r.pattern!r}: {e}")
            return env
    if args.text is not None:
        checker = Checker(source="<text>", rules=rules, check_hygiene=hygiene_on)
        data: Any = args.text
    else:
        schema_path = args.schema or (Path(conf["schema"]) if conf.get("schema") else None)
        checker = Checker(
            source=str(args.file),
            rules=rules,
            schema=json.loads(schema_path.read_text("utf-8")) if schema_path else None,
            count=args.count if args.count is not None else conf.get("count"),
            unique=args.unique or list(conf.get("unique", [])),
            check_hygiene=hygiene_on,
        )
        env.inputs.append({"path": str(args.file)})
        data = load(args.file)
    source = checker.source
    env.results = checker.run(data)
    if env.results:
        env.error(VIOLATION, f"{len(env.results)} violations in {source}")
    else:
        env.summary = f"no violations in {source}"
    return env
