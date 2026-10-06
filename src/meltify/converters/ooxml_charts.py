"""DrawingML charts and SmartArt as cited text, and renders of charts that saved no values"""

from __future__ import annotations

import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from meltify.converters import Block
from meltify.converters.embeds import Embeds
from meltify.converters.limits import read_part
from meltify.converters.ooxml import NS, R_ID, TEXT, integer, rels, with_parts
from meltify.converters.tables import escape_cell
from meltify.converters.xmlsafe import zip_xml
from meltify.evidence import Src

POINT = f"{{{NS['c']}}}pt"
FRAME = f"{{{NS['p']}}}graphicFrame"
GROUP = f"{{{NS['p']}}}grpSp"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
EXTENT = f"{{{WP}}}extent"
EMU_PER_TWIP = 635
# Half an inch around a chart drawn alone on its page, in twips
PAGE_MARGIN = 720
# Word's default chart size in EMU, for a drawing that saved none
CHART_SIZE = (5 * 914400, 3 * 914400)
# Past this many points a chart cache with a huge idx is read sparse instead of padded
MAX_POINTS = 100_000

# Cell values a chart formula like `'Sales'!$B$2:$B$5` names, None when it can't say
Resolve = Callable[[str], list[str] | None]


def take(
    embeds: Embeds,
    z: zipfile.ZipFile,
    kind: str,
    rid: str | None,
    targets: dict[str, str | None],
    src: Src,
    resolve: Resolve | None = None,
) -> None:
    """Read one object that `ooxml.objects` found into `embeds`"""
    if kind == "picture":
        embeds.member(z, src, targets, rid)
        return
    read_object(embeds, z, kind, targets.get(rid or ""), src, resolve)


def read_object(
    embeds: Embeds,
    z: zipfile.ZipFile,
    kind: str,
    part: str | None,
    src: Src,
    resolve: Resolve | None,
) -> None:
    """Read a chart or diagram part into a cited block"""
    if part is None or part not in z.NameToInfo:
        embeds.skipped[f"missing {kind}"] += 1
        return
    try:
        root = zip_xml(z, part)
    except (ET.ParseError, ValueError):
        # One broken part shouldn't cost the document its text
        embeds.skipped[f"unreadable {kind}"] += 1
        return
    if kind == "chart":
        text = chart_text(root, resolve)
        if text is None and (inner := embedded_cells(z, part, root)) is not None:
            text = chart_text(root, inner)
        if text is None:
            embeds.uncached.append(src)
            return
    else:
        text = diagram_text(root)
    if text:
        embeds.blocks.append(Block(src, text))


def _points(el: ET.Element | None, resolve: Resolve | None) -> list[str] | None:
    """Cached points of a series part, or the cells its formula names when nothing is cached"""
    if el is None:
        return None
    # Multi level categories list the innermost level first
    scope = el.find(".//c:lvl", NS)
    scope = el if scope is None else scope
    values = {integer(pt.get("idx")): pt.findtext("c:v", "", NS) for pt in scope.iter(POINT)}
    if values:
        count = scope.find(".//c:ptCount", NS)
        size = max(integer(None if count is None else count.get("val")), max(values) + 1)
        if size > MAX_POINTS:
            return [values[i] for i in sorted(values)]
        return [values.get(i, "") for i in range(size)]
    if (literal := el.findtext("c:v", None, NS)) is not None:
        return [literal]
    ref = el.findtext(".//c:f", None, NS)
    return resolve(ref) if ref and resolve else None


def _title(el: ET.Element | None) -> str:
    if el is None:
        return ""
    rich = ["".join(t.text or "" for t in p.iter(TEXT)) for p in el.iterfind("c:tx/c:rich/a:p", NS)]
    text = " ".join(p for p in rich if p) or " ".join(_points(el.find("c:tx", NS), None) or [])
    return text.strip()


def chart_text(root: ET.Element, resolve: Resolve | None = None) -> str | None:
    """A chart's title, axes and data table, None when its series saved no values"""
    chart = root.find("c:chart", NS)
    if chart is None:
        return ""
    lines = [f"Chart: {title}" if (title := _title(chart.find("c:title", NS))) else "Chart"]
    plot = chart.find("c:plotArea", NS)
    if plot is None:
        return lines[0]
    axes = [t for ax in plot if ax.tag.endswith("Ax") and (t := _title(ax.find("c:title", NS)))]
    if axes:
        lines.append("Axes: " + ", ".join(axes))
    series = []
    for i, ser in enumerate(plot.iterfind("*/c:ser", NS), start=1):
        name = " ".join(_points(ser.find("c:tx", NS), resolve) or []) or f"series {i}"
        cats = _points(ser.find("c:cat", NS), resolve) or _points(ser.find("c:xVal", NS), resolve)
        vals = _points(ser.find("c:val", NS), resolve) or _points(ser.find("c:yVal", NS), resolve)
        series.append((name, cats or [], vals or []))
    if not series:
        return "\n".join(lines)
    # Formula cells nobody calculated resolve to blanks, which are no values either
    if not any(any(vals) for _, _, vals in series):
        return None
    rows = max(max(len(c), len(v)) for _, c, v in series)
    lines.append("| category | " + " | ".join(escape_cell(name) for name, _, _ in series) + " |")
    lines.append("|---" * (len(series) + 1) + "|")
    for r in range(rows):
        label = next((c[r] for _, c, _ in series if r < len(c) and c[r]), str(r + 1))
        values = [v[r] if r < len(v) else "" for _, _, v in series]
        lines.append(f"| {escape_cell(label)} | " + " | ".join(map(escape_cell, values)) + " |")
    return "\n".join(lines)


def embedded_cells(z: zipfile.ZipFile, part: str, root: ET.Element) -> Resolve | None:
    """Cells of the workbook Office embeds beside a chart, which its formulas point into

    Word and PowerPoint charts keep their data there, so it still answers when the chart
    part saved no cached values
    """
    from meltify.converters.sheet import resolver, rows_by_title

    link = root.find("c:externalData", NS)
    target = None if link is None else rels(z, part).get(link.get(R_ID) or "")
    if target is None or target not in z.NameToInfo:
        return None
    try:
        data = read_part(z, target)
    except ValueError:
        # Over the part limits, which leaves the chart listed as having no cached values
        return None
    return resolver(rows_by_title(data))


def diagram_text(root: ET.Element) -> str:
    """SmartArt node text from its data part, indented by the diagram's hierarchy"""
    texts: dict[str, str] = {}
    tops: list[str] = []
    for pt in root.iterfind("dgm:ptLst/dgm:pt", NS):
        kind, mid = pt.get("type", "node"), pt.get("modelId") or ""
        if kind == "doc":
            tops.append(mid)
        elif kind in ("node", "asst"):
            paras = [
                "".join(t.text or "" for t in p.iter(TEXT)) for p in pt.iterfind("dgm:t/a:p", NS)
            ]
            texts[mid] = " ".join(p.strip() for p in paras if p.strip())
    kids: defaultdict[str, list[tuple[int, str]]] = defaultdict(list)
    for cxn in root.iterfind("dgm:cxnLst/dgm:cxn", NS):
        if cxn.get("type", "parOf") == "parOf":
            kids[cxn.get("srcId") or ""].append(
                (integer(cxn.get("srcOrd")), cxn.get("destId") or "")
            )
    lines: list[str] = []
    seen: set[str] = set()

    def visit(mid: str, depth: int) -> None:
        if mid in seen:
            return
        seen.add(mid)
        if text := texts.get(mid):
            lines.append(f"{'  ' * depth}- {text}")
            depth += 1
        for _, kid in sorted(kids[mid]):
            visit(kid, depth)

    for mid in tops:
        visit(mid, 0)
    # Points no connection reaches still hold text a viewer sees
    for mid in texts:
        visit(mid, 0)
    return "\n".join(["Diagram", *lines]) if lines else ""


def extent(up: tuple[ET.Element, ...]) -> tuple[int, int]:
    """A Word drawing's size in EMU, from the inline or anchor that holds it"""
    for a in reversed(up):
        if (ext := a.find(EXTENT)) is not None:
            cx, cy = integer(ext.get("cx")), integer(ext.get("cy"))
            if cx > 0 and cy > 0:
                return cx, cy
    return CHART_SIZE


def frame(up: tuple[ET.Element, ...], slide: tuple[int, int]) -> tuple[float, ...] | None:
    """Where a chart's frame sits on its slide as page fractions, None inside a group"""
    if any(a.tag == GROUP for a in up):
        return None
    found = next((a for a in reversed(up) if a.tag == FRAME), None)
    off = None if found is None else found.find("p:xfrm/a:off", NS)
    ext = None if found is None else found.find("p:xfrm/a:ext", NS)
    if off is None or ext is None or not all(slide):
        return None
    x, y = integer(off.get("x")), integer(off.get("y"))
    w, h = integer(ext.get("cx")), integer(ext.get("cy"))
    return (x / slide[0], y / slide[1], (x + w) / slide[0], (y + h) / slide[1])


def chart_pages(charts: list[tuple[str, int, int]]) -> tuple[bytes, list[tuple[float, ...]]]:
    """A Word document with each chart alone on a page its own size, and where it sits there

    Word lays out flowing pages only when it renders, so a chart's page in the real document
    can't be known. Charts are given as relationship id and size in EMU
    """
    m = PAGE_MARGIN
    plain = (
        '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
        '<w:ind w:left="0" w:right="0" w:firstLine="0"/><w:jc w:val="left"/>'
    )
    body, clips = [], []
    for n, (rid, cx, cy) in enumerate(charts, start=1):
        w, h = cx // EMU_PER_TWIP, cy // EMU_PER_TWIP
        # A little room under the chart, so rounding never pushes it onto a second page
        page_w, page_h = w + 2 * m, h + 2 * m + m // 4
        clips.append((m / page_w, m / page_h, (m + w) / page_w, (m + h) / page_h))
        section = (
            f'<w:sectPr><w:pgSz w:w="{page_w}" w:h="{page_h}"/><w:pgMar w:top="{m}" '
            f'w:right="{m}" w:bottom="{m}" w:left="{m}" w:header="0" w:footer="0" w:gutter="0"/>'
            "</w:sectPr>"
        )
        run = (
            '<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            f'<wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{n}" name="Chart {n}"/>'
            f'<a:graphic><a:graphicData uri="{NS["c"]}"><c:chart r:id="{escape(rid)}"/>'
            "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
        )
        # Each paragraph but the last ends its own section, which starts the next page
        last = n == len(charts)
        body.append(f"<w:p><w:pPr>{plain}{'' if last else section}</w:pPr>{run}</w:p>")
        if last:
            body.append(section)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{NS["w"]}" xmlns:r="{NS["r"]}" xmlns:wp="{WP}" '
        f'xmlns:a="{NS["a"]}" xmlns:c="{NS["c"]}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    return document.encode(), clips


def _may_render() -> bool:
    """Whether uncached charts get drawn for OCR

    --shallow starts no renderer and drops picture jobs, so there the charts stay listed
    as having no cached values instead of turning into jobs that never run
    """
    from meltify.converters import render, run

    return not run.current().shallow and render.available()


def render_docx_charts(
    path: Path, part: str, embeds: Embeds, charts: dict[Src, tuple[str, int, int]]
) -> None:
    """Draw charts that saved no values from a copy of the document holding only them"""
    wanted = {at: c for at, c in charts.items() if at in embeds.uncached}
    if not wanted or not _may_render():
        return
    document, clips = chart_pages(list(wanted.values()))
    with tempfile.TemporaryDirectory(prefix="meltify-chart-") as tmp:
        copy = Path(tmp) / "charts.docx"
        with_parts(path, {part: document}, copy)
        placed = zip(wanted, clips, strict=True)
        regions = {at: (n, clip) for n, (at, clip) in enumerate(placed, start=1)}
        render_charts(copy, embeds, regions, len(regions))


def _page_count(pdf: Path) -> int:
    import pymupdf

    from meltify.converters import run

    with run.LOCK, pymupdf.open(pdf) as doc:
        return doc.page_count


def render_charts(
    path: Path, embeds: Embeds, regions: dict[Src, tuple[int, tuple[float, ...]]], pages: int
) -> None:
    """Crop charts that saved no values out of a render of the file for OCR

    A render with other than the `pages` expected would put every crop on the wrong chart
    """
    from meltify.converters import render

    wanted = {at: r for at, r in regions.items() if at in embeds.uncached}
    if not wanted or not _may_render():
        return
    with tempfile.TemporaryDirectory(prefix="meltify-chart-") as tmp:
        try:
            pdf = render.to_pdf(path, out_dir=Path(tmp))
            if pdf is None or _page_count(pdf) != pages:
                return
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return
        for at, (page, clip) in wanted.items():
            try:
                image = render.png(pdf, page, clip)
            except Exception:  # noqa: BLE001
                # A page the render doesn't have stays listed as not read
                continue
            embeds.uncached.remove(at)
            embeds.add(at, image, "chart.png")
