from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "meltify/v1"
MISSING = "missing"
USAGE = "usage"


def clock(seconds: float) -> str:
    # Round to whole tenths once, so 59.96 carries into the minute instead of showing 60.0
    tenths = round(seconds * 10)
    h, rem = divmod(tenths, 36000)
    m, rem = divmod(rem, 600)
    s, d = divmod(rem, 10)
    return f"{h:02d}:{m:02d}:{s:02d}.{d}"


def coordinate(v: float) -> str:
    return f"{v:g}" if float(v).is_integer() else f"{v:.1f}"


@dataclass(frozen=True)
class Src:
    """Where a value came from in the original input

    The cite joins the set fields in this fixed order, each one optional:

        path ("#att=" member)* ["#" jpath] ["#" anchor] ["#s" section] ["#p" page]
        ["#slide" slide] ["#frame" frame] ["#" sheet ["!" cell] | "#cell=" cell] ["#para" para]
        ["#ts=" ts] ["#img" img] ["@" unit "(" x0,y0,x1,y1 ")"] ["@" start "-" end]
        [":" line]

    Each container level adds one `#att=`, so `/` inside a member stays a directory:
    `a.zip#att=docs/b.pdf#p3`, `a.zip#att=x.zip#att=c.txt:1`, `mail.eml#att=inv.png@px(1,2,3,4)`
    """

    path: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    unit: str | None = None  # pt for PDF space, px for images
    t: tuple[float, float] | None = None  # seconds
    sheet: str | None = None  # also a table path like `Sheet>Table` or a database table
    cell: str | None = None  # `B2` under a sheet, a notebook cell index without one
    line: int | None = None
    parts: tuple[str, ...] = ()  # attachment or archive members, outermost first
    jpath: str | None = None  # JSONPath inside a structured file
    anchor: str | None = None  # fragment id inside a web page
    section: int | None = None
    slide: int | None = None
    frame: int | None = None  # page of a multi-page TIFF or frame of an animation, from 1
    para: int | None = None
    img: int | None = None  # embedded image index within its page, slide or document
    ts: str | None = None  # message timestamp id in a chat export

    def inside(self, member: str) -> Src:
        """Root of a member nested one level deeper in this container"""
        return Src(self.path, parts=(*self.parts, member))

    def cite(self) -> str:
        # Stable one-line form an agent can paste next to a claim
        out = self.path + "".join(f"#att={p}" for p in self.parts)
        if self.jpath:
            out += f"#{self.jpath}"
        if self.anchor:
            out += f"#{self.anchor}"
        if self.section is not None:
            out += f"#s{self.section}"
        if self.page is not None:
            out += f"#p{self.page}"
        if self.slide is not None:
            out += f"#slide{self.slide}"
        if self.frame is not None:
            out += f"#frame{self.frame}"
        if self.sheet:
            out += f"#{self.sheet}" + (f"!{self.cell}" if self.cell else "")
        elif self.cell:
            out += f"#cell={self.cell}"
        if self.para is not None:
            out += f"#para{self.para}"
        if self.ts:
            out += f"#ts={self.ts}"
        if self.img is not None:
            out += f"#img{self.img}"
        if self.bbox is not None:
            out += f"@{self.unit or 'px'}(" + ",".join(coordinate(v) for v in self.bbox) + ")"
        if self.t is not None:
            out += f"@{clock(self.t[0])}-{clock(self.t[1])}"
        if self.line is not None:
            out += f":{self.line}"
        return out

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != ()}


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
