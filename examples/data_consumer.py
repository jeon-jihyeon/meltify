"""Read a gatereach snapshot as cited documents without treating read success as source success"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterator
from pathlib import Path

import meltify

MAX_DESCRIPTOR_BYTES = 64 << 20
MAX_STREAM_CHARS = 4 << 20
# gatereach end statuses. A value outside them may be producer text, so it never goes out
STATUSES = {"finished", "stopped", "budget", "interrupted", "error"}
UNITS = {"rows examined", "reply elements", "read capacity units", "records", "bytes scanned"}
FORMATS = {"csv", "arrow", "arrows", "feather"}


def _known(value: object, words: set[str]) -> bool:
    return isinstance(value, str) and value in words


def _rows(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as stream:
        while line := stream.readline(MAX_STREAM_CHARS + 1):
            if len(line) > MAX_STREAM_CHARS:
                raise ValueError("stream row exceeds 4 Mi characters")
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("expected an object row")
            yield row


def read_events(path: Path, exit_code: int) -> dict:
    count = 0
    terminal = None
    for row in _rows(path):
        if row.get("schema") != "gatereach/v1" or terminal is not None:
            raise ValueError("invalid event sequence")
        if row.get("type") == "end":
            terminal = row
        elif row.get("type") == "record" and isinstance(row.get("record"), dict):
            count += 1
        else:
            raise ValueError("invalid event")
    end = terminal or {}
    status = end["status"] if _known(end.get("status"), STATUSES) else None
    complete = (
        exit_code == 0
        and terminal is not None
        and type(end.get("rows")) is int
        and end["rows"] == count
        and status in {"finished", "stopped", "budget"}
        # A signal ends a follow normally, while a stopped single read never finished
        and (status != "stopped" or end.get("follow") is True)
        and not end.get("error")
    )
    return {
        "ready": complete and status != "budget",
        "complete": complete,
        "status": status,
        "rows": count,
        "terminal": terminal is not None,
        "process_exit": exit_code,
        "scope": {
            key: end[key]
            for key, kind in (("limit", int), ("follow", bool))
            if type(end.get(key)) is kind
        },
    }


def read_matches(path: Path, exit_code: int, strict: bool) -> dict:
    count = misses = 0
    for row in _rows(path):
        if row.get("schema_version") != "moldrain.match/v1" or type(row.get("matched")) is not bool:
            raise ValueError("invalid match row")
        count += 1
        misses += not row["matched"]
    # Strict mode exits one when a row missed, which still means the whole stream arrived
    complete = exit_code == 0 or (strict and exit_code == 1 and misses > 0)
    return {
        "ready": complete and exit_code == 0,
        "complete": complete,
        "status": "misses" if misses else "matches",
        "rows": count,
        "misses": misses,
        "process_exit": exit_code,
    }


def _head(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(MAX_DESCRIPTOR_BYTES + 1)


def _scope(producer: dict) -> dict:
    """The producer's outcome and scope, keeping only typed values and known words"""
    source = {
        k: producer[k]
        for k, kind in (("complete", bool), ("stopped", str), ("limit", int), ("follow", bool))
        if type(producer.get(k)) is kind
    }
    if source.get("stopped") not in (None, "", "budget"):
        source["stopped"] = "unknown"
    cap = producer.get("max")
    if type(cap) in (int, float) and math.isfinite(cap) and cap > 0:
        source["max"] = cap
    if _known(producer.get("unit"), UNITS):
        source["unit"] = producer["unit"]
    return source


def _supported(resources: list) -> bool:
    if not all(isinstance(r, dict) and _known(r.get("format"), FORMATS) for r in resources):
        return False
    names = [r.get("name") for r in resources]
    return all(isinstance(n, str) and n for n in names) and len(set(names)) == len(names)


def _texts(envelope: dict, resources: set[str]) -> dict[str, tuple[dict, list[str]]]:
    """Resource summary and sample text by cite, with the `src` of the cite's first block"""
    texts: dict[str, tuple[dict, list[str]]] = {}
    for row in envelope["results"]:
        for block in row.get("content", []):
            # The descriptor and Arrow schema metadata hold source queries and accounts,
            # not table evidence
            if block.get("role") == "metadata" or block["src"].get("sheet") not in resources:
                continue
            texts.setdefault(block["cite"], (block["src"], []))[1].append(block["text"])
    return texts


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_package(path: Path, out: Path) -> dict:
    before = _head(path)
    if len(before) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("descriptor exceeds 64 MiB")
    descriptor = json.loads(before)
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("resources"), list):
        raise ValueError("expected a Data Package descriptor")
    producer = descriptor.get("gatereach")
    if not isinstance(producer, dict):
        producer = {}
    envelope = meltify.read(path, out=out, content=True, shallow=True).to_dict()
    unchanged = _head(path) == before
    read = {
        "schema": envelope["schema"],
        "version": envelope["version"],
        "ok": envelope["ok"],
        "complete": envelope["complete"],
        "coverage": [row["coverage"]["status"] for row in envelope["results"]],
    }
    supported = _supported(descriptor["resources"])
    ready = (
        producer.get("complete") is True
        and producer.get("error") in (None, "")
        and producer.get("stopped") in (None, "")
        and read["ok"]
        and read["complete"]
        and all(status == "complete" for status in read["coverage"])
        and unchanged
        and supported
    )
    documents, source_map = [], {}
    if ready:
        names = {r["name"] for r in descriptor["resources"]}
        for cite, (src, parts) in _texts(envelope, names).items():
            content = "\n\n".join(parts)
            doc_id = _digest(cite)
            documents.append(
                {
                    "docId": doc_id,
                    "content": content,
                    "metadata": {"evidenceLocation": cite, "parserType": "meltify"},
                }
            )
            source_map[doc_id] = {"src": src, "cite": cite, "sha256": _digest(content)}
    return {
        "ready": ready,
        "source": _scope(producer),
        "read": read,
        "descriptor_unchanged": unchanged,
        "descriptor_sha256": hashlib.sha256(before).hexdigest(),
        "resources_supported": supported,
        "representation": "summary_and_sample",
        "full_rows": False,
        "connector": (
            {"ok": True, "documents": documents}
            if ready
            else {"ok": False, "reason": "invalid-data"}
        ),
        "source_map": source_map,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, nargs="?")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--events", type=Path)
    mode.add_argument("--match", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--exit-code", type=int)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    stream = args.events or args.match
    if bool(args.package) == bool(stream):
        parser.error("choose a package or one stream input")
    if stream and (args.exit_code is None or args.out or (args.strict and not args.match)):
        parser.error("a stream needs --exit-code; --out is package-only and --strict is match-only")
    if args.package and (not args.out or args.exit_code is not None or args.strict):
        parser.error("a package needs --out and takes no stream flags")
    try:
        if args.events:
            result = read_events(args.events, args.exit_code)
        elif args.match:
            result = read_matches(args.match, args.exit_code, args.strict)
        else:
            result = read_package(args.package, args.out)
    except (OSError, ValueError, RecursionError):
        # Failure text may hold paths or source values, so diagnostics stay coarse
        result = {"ready": False, "connector": {"ok": False, "reason": "invalid-data"}}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
