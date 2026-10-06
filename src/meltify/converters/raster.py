from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from meltify.converters import Converted, RecognizeJob
from meltify.evidence import Src


def convert(path: Path, src: Src) -> Converted:
    from meltify import imaging

    kept, unread = imaging.frames(path)
    if kept == [1] and not unread:
        return Converted("image", jobs=[RecognizeJob("image", src, path=path)])
    out = Converted(
        "image", jobs=[RecognizeJob("image", replace(src, frame=n), path=path) for n in kept]
    )
    if unread:
        out.needs.append(f"{unread} frames beyond {imaging.MAX_FRAMES} not read")
    return out
