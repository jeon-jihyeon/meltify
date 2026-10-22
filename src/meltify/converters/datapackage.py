"""Frictionless Data Package v2: the descriptor, then each resource cited by name and row

A cite like `datapackage.json#result!row=1` points at the first data row of the resource
named `result`. Nothing in the package is executed or fetched, and only files inside the
package folder are read
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import io
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import IO, Any

from meltify.converters import Block, Child, Converted
from meltify.converters.limits import (
    MAX_EMBED_BYTES,
    MAX_MEMBER_BYTES,
    MAX_PART_BYTES,
    ByteBudget,
    human_bytes,
)
from meltify.converters.tables import SAMPLE_ROWS, counted, sampled
from meltify.evidence import Src
from meltify.safe import MissingTool

NAME = "datapackage.json"
# One rendered JSON resource or descriptor value, so a huge document can't flood the markdown
MAX_JSON_CHARS = 20000
MAX_VALUE_CHARS = 2000
# Any `scheme:` prefix, which covers URLs, data URIs and Windows drive letters
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# Keys the descriptor block shows first, in this order. Every other key follows as it comes
LEAD = ("name", "title", "created", "sources", "licenses")
# Keys an object on the descriptor block shows first, so cutting a long value like a query
# never hides whether the run that wrote the package finished
STATUS = ("complete", "error")
# Resource keys rendered as the resource itself rather than in its summary line
BODY = {"name", "data", "schema", "dialect"}
TABULAR = {"csv", "tsv"}
JSONL = {"jsonl", "ndjson"}
ARROW = {"arrow", "arrows", "feather"}


class Skip(Exception):
    """A resource or part that stays unread, with the reason for its needs line"""


# What one resource may raise without taking the descriptor or a folder walk down with it.
# A path with a NUL byte raises ValueError, a symlink loop RuntimeError before Python 3.13,
# and JSON nested past the recursion limit RecursionError, a RuntimeError too. LookupError
# guards a codec that passes the text check yet still can't decode
UNREAD = (Skip, OSError, ValueError, RuntimeError, LookupError)


def sniff(path: Path, head: bytes) -> bool:
    return path.name.lower() == NAME


def _compact(value: Any, limit: int = MAX_VALUE_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


class Package:
    """What one descriptor holds, shared by its resources so a repeated file can't multiply it"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.budget = ByteBudget(MAX_EMBED_BYTES)
        # Handed-on files, which melt once
        self.read: set[Path] = set()
        # Every file a resource has read, so a later read of it is charged to the budget
        self.seen: set[Path] = set()


def _local(root: Path, ref: Any) -> Path:
    """The file a descriptor path names, once it's proven to stay inside the package folder

    Hidden names are skipped the way a folder walk skips them, so a descriptor can't
    reach `.env` or `.git/config`
    """
    if not isinstance(ref, str) or not ref:
        raise Skip(f"path {_compact(ref, 80)} is not a string")
    if SCHEME.match(ref):
        raise Skip(f"{ref} not fetched, only local files are read")
    parts = PurePosixPath(ref).parts
    if ref.startswith("/") or "\\" in ref or ".." in parts:
        raise Skip(f"{ref} rejected, outside the package")
    if any(p.startswith(".") for p in parts):
        raise Skip(f"{ref} skipped, hidden file")
    target = root.joinpath(*parts)
    # A symlink inside the folder may still point out of it
    if not target.resolve().is_relative_to(root.resolve()):
        raise Skip(f"{ref} rejected, outside the package")
    if not target.is_file():
        raise Skip(f"{ref} missing")
    return target


def _object(root: Path, value: Any, what: str) -> dict:
    """A schema or dialect given inline or as a path to a JSON file in the package"""
    if value is None:
        return {}
    if isinstance(value, str):
        target = _local(root, value)
        if target.stat().st_size > MAX_PART_BYTES:
            raise Skip(f"{what} {value} over the {human_bytes(MAX_PART_BYTES)} limit")
        try:
            value = json.loads(target.read_bytes())
        except ValueError as e:
            raise Skip(f"{what} {value} is not JSON: {e}") from e
    if not isinstance(value, dict):
        raise Skip(f"{what} is not an object")
    return value


def _paths(resource: dict) -> list[Any]:
    path = resource.get("path")
    if path is None:
        return []
    return path if isinstance(path, list) else [path]


def _format(resource: dict) -> str:
    fmt = resource.get("format")
    if isinstance(fmt, str) and fmt:
        return fmt.lower()
    paths = _paths(resource)
    if paths and isinstance(paths[0], str):
        return PurePosixPath(paths[0]).suffix.lower().lstrip(".")
    return ""


def _missing(values: Any) -> set[str]:
    """missingValues as plain strings, from strings or v2 `{value, label}` objects"""
    if not isinstance(values, list):
        return {""}
    out = set()
    for v in values:
        if isinstance(v, dict):
            v = v.get("value")
        if isinstance(v, str):
            out.add(v)
    return out


class Table:
    """Columns, types and missing values from a resource's schema"""

    def __init__(self, schema: dict, typed: bool = False) -> None:
        """`typed` rows come from JSON, where null already marks a missing value"""
        fields = schema.get("fields")
        self.fields = [f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else []
        self.names = [str(f.get("name", "")) for f in self.fields]
        self.types = {str(f.get("name", "")): f.get("type") for f in self.fields if f.get("type")}
        default = _missing(schema.get("missingValues", [] if typed else [""]))
        self.missing = [
            _missing(f["missingValues"]) if "missingValues" in f else default for f in self.fields
        ]
        self.default = default

    def label(self, name: str) -> str:
        kind = self.types.get(name)
        return f"{name} ({kind})" if kind else name

    def clean(self, row: list[Any]) -> list[Any]:
        out = []
        for i, v in enumerate(row):
            missing = self.missing[i] if i < len(self.missing) else self.default
            out.append(None if isinstance(v, str) and v in missing else v)
        return out


def _reader(text: Iterator[str], dialect: dict, fmt: str) -> Iterator[list[str]]:
    delimiter = dialect.get("delimiter", "\t" if fmt == "tsv" else ",")
    options: dict[str, Any] = {
        "delimiter": delimiter if isinstance(delimiter, str) and len(delimiter) == 1 else ",",
        "doublequote": bool(dialect.get("doubleQuote", True)),
        "skipinitialspace": bool(dialect.get("skipInitialSpace", False)),
    }
    quote = dialect.get("quoteChar", '"')
    if isinstance(quote, str) and len(quote) == 1:
        options["quotechar"] = quote
    escape = dialect.get("escapeChar")
    if isinstance(escape, str) and len(escape) == 1:
        options["escapechar"] = escape
    comment = dialect.get("commentChar")
    rows = csv.reader(text, **options)
    if isinstance(comment, str) and comment:
        rows = (r for r in rows if not (r and r[0].startswith(comment)))
    return rows


def _encoding(resource: dict) -> str:
    encoding = resource.get("encoding")
    if not isinstance(encoding, str) or not encoding:
        return "utf-8-sig"
    try:
        info = codecs.lookup(encoding)
    except LookupError as e:
        raise Skip(f"unknown encoding {encoding}") from e
    # Codecs like base64 and rot13 look up fine, then fail when text is decoded with them
    if not info._is_text_encoding:
        raise Skip(f"{encoding} is not a text encoding")
    return "utf-8-sig" if info.name == "utf-8" else info.name


def _load_json(target: Path, encoding: str) -> Any:
    try:
        return json.loads(target.read_bytes().decode(encoding, errors="replace"))
    except ValueError as e:
        raise Skip(f"not JSON: {e}") from e


def _joined(values: list[Any], tabular: bool) -> Any:
    """Multipart JSON as one value, the parts of a table as one list of rows

    A table of arrays repeats its header row at the top of each part, so only the first
    part's header stays
    """
    if len(values) == 1:
        return values[0]
    if not tabular or not all(isinstance(v, list) for v in values):
        return values
    rows = list(values[0])
    header = rows[0] if rows and isinstance(rows[0], list) else None
    for v in values[1:]:
        rows += v[1:] if header is not None and v[:1] == [header] else v
    return rows


def _tabular(resource: dict) -> bool:
    """Whether the resource says it's a table, which decides how a JSON value renders"""
    fmt = _format(resource)
    return resource.get("type") == "table" or fmt in TABULAR | JSONL or "schema" in resource


def _kind(resource: dict) -> str:
    """How a resource's files are read"""
    fmt = _format(resource)
    if fmt == "json":
        return "json"
    if fmt in ARROW:
        return "arrow"
    if fmt in JSONL:
        return "jsonl"
    if fmt in TABULAR or (_tabular(resource) and fmt in ("", "txt")):
        return "csv"
    return "file"


def _plan(resource: dict, pkg: Package) -> list[Path]:
    """The files a resource's path lists, once every check that needs no parsing passes

    Folder reads claim exactly these, so a file the descriptor turns down up front still
    melts on its own
    """
    refs = _paths(resource)
    if not refs:
        raise Skip("no path or data")
    kind = _kind(resource)
    if kind in ("json", "jsonl", "csv"):
        _encoding(resource)
    # Arrow is memory-mapped and checks what it unpacks, JSON is parsed whole
    limit = {"arrow": None, "json": MAX_PART_BYTES}.get(kind, MAX_MEMBER_BYTES)
    parts: list[Path] = []
    keys: set[Path] = set()
    held = 0
    for ref in refs:
        target = _local(pkg.root, ref)
        size = target.stat().st_size
        if limit is not None and size > limit:
            raise Skip(f"{ref} is {size:,} bytes, over the {human_bytes(limit)} limit")
        if (key := target.resolve()) in keys:
            raise Skip(f"{ref} listed twice")
        # Resources may share a table under different schemas, but a file handed on twice
        # would only melt twice
        if kind == "file" and key in pkg.read:
            raise Skip(f"{ref} already read")
        # Parsed JSON and handed-on files sit in memory. A first CSV, JSON Lines or Arrow
        # read streams or maps the file, but a repeat is charged so repeats can't multiply it
        if kind in ("json", "file") or key in pkg.seen:
            held += size
        keys.add(key)
        parts.append(target)
    if not pkg.budget.spend(held):
        raise Skip(f"over the {human_bytes(MAX_EMBED_BYTES)} package limit")
    pkg.seen |= keys
    if kind == "file":
        pkg.read |= keys
    return parts


Opener = Callable[[], IO[str]]


class Reader(Package):
    """A package being melted, its resources cited under `src` and rendered into `out`"""

    def __init__(self, root: Path, src: Src, out: Converted) -> None:
        super().__init__(root)
        self.src = src
        self.out = out

    def resource(self, i: int, resource: Any) -> None:
        if not isinstance(resource, dict):
            raise Skip("not an object")
        name = str(resource.get("name") or f"resource{i}")
        fmt = _format(resource)
        kind, tabular = _kind(resource), _tabular(resource)
        schema = resource.get("schema")

        if "data" in resource:
            data = resource["data"]
            if isinstance(data, str):
                if fmt in JSONL:
                    self._jsonl(name, [("inline data", partial(io.StringIO, data))], schema)
                elif kind == "csv":
                    self._csv(name, [partial(io.StringIO, data)], resource)
                elif fmt == "json":
                    try:
                        value = json.loads(data)
                    except ValueError as e:
                        raise Skip(f"inline data is not JSON: {e}") from e
                    self._value(name, value, tabular, schema)
                else:
                    child = Child(f"{name}.{fmt or 'txt'}", self.src, data.encode("utf-8"))
                    self.out.children.append(child)
            else:
                self._value(name, data, tabular, schema)
            return

        refs = _paths(resource)
        parts = _plan(resource, self)
        if kind == "json":
            encoding = _encoding(resource)
            value = _joined([_load_json(p, encoding) for p in parts], tabular)
            self._value(name, value, tabular, schema)
        elif kind == "arrow":
            self._arrow(name, parts)
        elif kind == "jsonl":
            encoding = _encoding(resource)
            opens = [
                (ref, partial(p.open, encoding=encoding, errors="replace"))
                for ref, p in zip(refs, parts, strict=True)
            ]
            self._jsonl(name, opens, schema)
        elif kind == "csv":
            encoding = _encoding(resource)
            opens = [
                partial(p.open, encoding=encoding, errors="replace", newline="") for p in parts
            ]
            self._csv(name, opens, resource)
        else:
            for ref, part in zip(refs, parts, strict=True):
                self.out.children.append(Child(ref, self.src, part.read_bytes()))

    def _block(
        self,
        name: str,
        columns: list[str],
        rows: Iterator[list[Any]],
        table: Table,
        total: int | None = None,
    ) -> None:
        """The first rows as a sample, with `total` given when `rows` holds only the sample

        Every row is counted, but only the shown ones get their missing values cleaned
        """
        shown: list[list[Any]] = []
        seen = 0
        for row in rows:
            seen += 1
            if len(shown) < SAMPLE_ROWS:
                shown.append(table.clean(row))
        text = sampled(name, columns, shown, seen if total is None else total)
        at = replace(self.src, sheet=name, cell="row=1" if shown else None)
        self.out.blocks.append(Block(at, text, role="sample"))

    def _csv(self, name: str, parts: list[Opener], resource: dict) -> None:
        table = Table(_object(self.root, resource.get("schema"), "schema"))
        dialect = _object(self.root, resource.get("dialect"), "dialect")
        header = bool(dialect.get("header", True))
        fmt = _format(resource)

        def rows() -> Iterator[list[Any]]:
            """The header row first when the dialect has one, then every data row of every part

            Only the first part of a multipart path carries the header
            """
            for i, part in enumerate(parts):
                with part() as f:
                    reader = _reader(f, dialect, fmt)
                    if header and i == 0:
                        yield next(reader, [])
                    yield from reader

        try:
            it = rows()
            columns = next(it) if header else table.names
            self._block(name, [table.label(c) for c in columns], it, table)
        except csv.Error as e:
            raise Skip(f"unreadable CSV: {e}") from e

    def _records(self, name: str, value: list, table: Table) -> None:
        """A tabular JSON resource: arrays of values with a header row first, or objects"""
        if value and all(isinstance(r, dict) for r in value):
            columns = list(table.names) or list(dict.fromkeys(k for r in value for k in r))
            body = value
            rows = ([r.get(c) for c in columns] for r in body[:SAMPLE_ROWS])
        else:
            columns = [str(c) for c in value[0]] if value and isinstance(value[0], list) else []
            body = value[1:]
            rows = (r if isinstance(r, list) else [r] for r in body[:SAMPLE_ROWS])
        self._block(name, [table.label(c) for c in columns], rows, table, len(body))

    def _json(self, name: str, value: Any) -> None:
        if isinstance(value, list):
            shown, total = value[:SAMPLE_ROWS], len(value)
            head = counted(total, "item")
            if total > len(shown):
                head = f"first {len(shown)} of {head}"
            lines = [f"{name}: {head}"]
            lines += [f"row={n}| {_compact(v)}" for n, v in enumerate(shown, start=1)]
            at = replace(self.src, sheet=name, cell="row=1" if shown else None)
            self.out.blocks.append(Block(at, "\n".join(lines), role="sample"))
            return
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > MAX_JSON_CHARS:
            cut = f"JSON cut at {MAX_JSON_CHARS:,} of {len(text):,} chars"
            self.out.needs.append(f"resource {name}: {cut}")
            text = text[:MAX_JSON_CHARS] + "..."
        self.out.blocks.append(Block(replace(self.src, sheet=name), text))

    def _value(self, name: str, value: Any, tabular: bool, schema: Any) -> None:
        """A parsed JSON resource, as a table when the resource says it's one and rows fit"""
        rows = isinstance(value, list) and (
            all(isinstance(r, dict) for r in value) or isinstance(value[0], list)
        )
        if tabular and rows:
            self._records(name, value, Table(_object(self.root, schema, "schema"), typed=True))
        else:
            self._json(name, value)

    def _jsonl(self, name: str, parts: list[tuple[str, Opener]], schema: Any) -> None:
        """A JSON Lines table read a line at a time, so a big result never sits in memory whole"""
        table = Table(_object(self.root, schema, "schema"), typed=True)
        # Without a schema, columns are every key seen, so the sample keeps whole objects
        keys: dict[str, None] = {}
        shown: list[dict] = []
        total = bad = 0
        first_bad = ""
        for label, part in parts:
            with part() as f:
                for n, line in enumerate(f, start=1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        row = None
                    if not isinstance(row, dict):
                        bad += 1
                        first_bad = first_bad or f"{label} line {n}"
                        continue
                    total += 1
                    if not table.names:
                        keys.update(dict.fromkeys(row))
                    if len(shown) < SAMPLE_ROWS:
                        shown.append(row)
        if bad:
            what = "line" if bad == 1 else "lines"
            first = f"first at {first_bad}"
            self.out.needs.append(f"resource {name}: {bad:,} {what} not JSON objects, {first}")
        columns = table.names or list(keys)
        rows = ([r.get(c) for c in columns] for r in shown)
        self._block(name, [table.label(c) for c in columns], rows, table, total)

    def _arrow(self, name: str, parts: list[Path]) -> None:
        """Arrow resources through the Arrow converter, multipart paths joined when schemas match"""
        from meltify.converters import arrow

        # Without pyarrow this raises MissingTool, a need for this resource alone
        pa = arrow.require()
        tables = []
        try:
            for part in parts:
                # Name the package limit when it's the one that binds, so the need matches
                # what other resources over the budget say
                left = self.budget.left
                package = None
                if left < MAX_MEMBER_BYTES:
                    package = f"the {human_bytes(MAX_EMBED_BYTES)} package limit"
                got = arrow.load(part, min(MAX_MEMBER_BYTES, left), package)
                # Declared, not unpacked, so a batch that fails validation spends what a claim does
                self.budget.spend(got.declared)
                self.out.needs += [f"resource {name}: {need}" for need in got.needs]
                tables.append(got.table)
            if any(not t.schema.equals(tables[0].schema) for t in tables[1:]):
                raise Skip("parts have different Arrow schemas")
            table = tables[0] if len(tables) == 1 else pa.concat_tables(tables)
            at = replace(self.src, sheet=name)
            rows = replace(at, cell="row=1") if table.num_rows else at
            self.out.blocks += arrow.blocks(table, name, at, rows)
        except pa.ArrowException as e:
            # Kernels raise TypeError and NotImplementedError kinds too, and one resource
            # shouldn't take the descriptor block down with it
            raise Skip(f"unreadable Arrow: {e}"[:200]) from e


def _summary(i: int, resource: Any) -> str:
    if not isinstance(resource, dict):
        return f"- resource{i}: {_compact(resource)}"
    name = resource.get("name") or f"resource{i}"
    rest = [f"{k}={_compact(v)}" for k, v in resource.items() if k not in BODY]
    if "data" in resource:
        rest.insert(0, "inline data")
    return f"- {name}: {', '.join(rest)}" if rest else f"- {name}"


def _descriptor(descriptor: dict) -> str:
    keys = [k for k in LEAD if k in descriptor]
    keys += [k for k in descriptor if k not in LEAD and k != "resources"]
    lines = []
    for k in keys:
        value = descriptor[k]
        if isinstance(value, dict):
            value = {**{s: value[s] for s in STATUS if s in value}, **value}
        lines.append(f"{k}: {_compact(value)}")
    resources = descriptor.get("resources")
    if isinstance(resources, list):
        lines.append(f"resources: {len(resources)}")
        lines += [_summary(i, r) for i, r in enumerate(resources, start=1)]
    return "\n".join(lines)


def _as_text(path: Path, src: Src, why: str) -> Converted:
    from meltify.converters import text

    out = text.convert(path, src)
    out.needs.append(f"not a Data Package: {why}")
    return out


def convert(path: Path, src: Src) -> Converted:
    if (size := path.stat().st_size) > MAX_PART_BYTES:
        return _as_text(path, src, f"{size:,} bytes, over the {human_bytes(MAX_PART_BYTES)} limit")
    try:
        descriptor = json.loads(path.read_bytes())
    except (ValueError, RecursionError) as e:
        return _as_text(path, src, f"invalid JSON: {e}"[:200])
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("resources"), list):
        return _as_text(path, src, "no resources list")

    out = Converted("datapackage")
    out.blocks.append(Block(src, _descriptor(descriptor), role="metadata"))
    reader = Reader(path.parent, src, out)
    for i, resource in enumerate(descriptor["resources"], start=1):
        name = resource.get("name") if isinstance(resource, dict) else None
        try:
            reader.resource(i, resource)
        except UNREAD as e:
            out.needs.append(f"resource {name or f'resource{i}'}: {e}")
        except MissingTool as e:
            out.needs.append(f"resource {name or f'resource{i}'}: needs {e.name}, {e.hint}")
    return out


def _reads(resource: dict) -> tuple[str, ...]:
    """The schema and dialect keys `convert` loads for this resource"""
    kind = _kind(resource)
    if "data" in resource and not isinstance(resource["data"], str):
        kind = "json"
    if kind == "csv":
        return ("schema", "dialect")
    if kind == "jsonl" or (kind == "json" and _tabular(resource)):
        return ("schema",)
    return ()


def _unpacks(parts: list[Path], pkg: Package) -> None:
    """Spend what the Arrow read would unpack for these parts, from their declared sizes"""
    from meltify.converters import arrow

    for part in parts:
        pkg.budget.spend(arrow.unpacks(part, min(MAX_MEMBER_BYTES, pkg.budget.left)))


def claims(path: Path) -> set[Path]:
    """Resolved files a descriptor's resources read, schemas and dialects given as paths included

    Each resource runs the same checks `convert` runs before reading, in the same order. A
    schema or dialect is claimed only when its resource passes them and reads that file
    """
    if not sniff(path, b""):
        return set()
    try:
        if path.stat().st_size > MAX_PART_BYTES:
            return set()
        data = json.loads(path.read_bytes())
    except UNREAD:
        return set()
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, list):
        return set()
    pkg, out = Package(path.parent), set()
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if "data" not in resource:
            try:
                parts = _plan(resource, pkg)
            except UNREAD:
                continue
            out |= {p.resolve() for p in parts}
            if _kind(resource) == "arrow":
                with contextlib.suppress(*UNREAD, MissingTool):
                    _unpacks(parts, pkg)
        for key in _reads(resource):
            if isinstance(ref := resource.get(key), str):
                try:
                    out.add(_local(pkg.root, ref).resolve())
                except UNREAD:
                    continue
    return out
