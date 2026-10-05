from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "meltify/v1"
MISSING = "missing"
USAGE = "usage"


def _clock(seconds: float) -> str:
    # Round to whole tenths once, so 59.96 carries into the minute instead of showing 60.0
    tenths = round(seconds * 10)
    h, rem = divmod(tenths, 36000)
    m, rem = divmod(rem, 600)
    s, d = divmod(rem, 10)
    return f"{h:02d}:{m:02d}:{s:02d}.{d}"


def _num(v: float) -> str:
    return f"{v:g}" if float(v).is_integer() else f"{v:.1f}"


@dataclass(frozen=True)
class Src:
    """Where a value came from in the original input"""

    path: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    unit: str | None = None  # pt for PDF space, px for images
    t: tuple[float, float] | None = None  # seconds
    sheet: str | None = None
    cell: str | None = None
    line: int | None = None
    part: str | None = None  # attachment or archive member inside path
    jpath: str | None = None  # JSONPath inside a structured file

    def cite(self) -> str:
        # Stable one-line form an agent can paste next to a claim
        out = self.path
        if self.part:
            out += f"#att={self.part}"
        if self.jpath:
            out += f"#{self.jpath}"
        if self.page is not None:
            out += f"#p{self.page}"
        if self.sheet:
            out += f"#{self.sheet}" + (f"!{self.cell}" if self.cell else "")
        if self.bbox is not None:
            out += f"@{self.unit or 'px'}(" + ",".join(_num(v) for v in self.bbox) + ")"
        if self.t is not None:
            out += f"@{_clock(self.t[0])}-{_clock(self.t[1])}"
        if self.line is not None:
            out += f":{self.line}"
        return out

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def finding(src: Src, **values: Any) -> dict[str, Any]:
    return {**values, "src": src.to_dict(), "cite": src.cite()}


@dataclass
class Envelope:
    command: str
    version: str
    inputs: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    # Table columns for commands that show different rows per action
    columns: list[str] | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def exit_code(self) -> int:
        # 3 tells an agent to fix the environment instead of the input
        codes = {e["code"] for e in self.errors}
        if MISSING in codes:
            return 3
        if USAGE in codes:
            return 2
        return 1 if codes else 0

    def error(self, code: str, message: str, hint: str | None = None) -> None:
        item = {"code": code, "message": message}
        if hint:
            item["hint"] = hint
        self.errors.append(item)

    def artifact(self, path: str, role: str) -> None:
        self.artifacts.append({"path": path, "role": role})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "version": self.version,
            "ok": self.ok,
            "summary": self.summary,
            "inputs": self.inputs,
            "results": self.results,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
            "errors": self.errors,
        }
