"""XML parsing for parts of untrusted files, refusing entity declarations"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile

from meltify.converters.limits import read_part

# Entity declarations are the billion laughs payload, and real OOXML, ODF, iWork and SVG
# never have them. Expat reads UTF-16 too, so the check covers both byte orders
ENTITY = (b"<!ENTITY", "<!ENTITY".encode("utf-16-le"), "<!ENTITY".encode("utf-16-be"))
REFUSED = "refusing XML with <!ENTITY declarations"


def refuse_entities(data: bytes) -> None:
    if any(e in data for e in ENTITY):
        raise ValueError(REFUSED)


def parse(data: bytes) -> ET.Element:
    refuse_entities(data)
    return ET.fromstring(data)


def zip_xml(z: zipfile.ZipFile, part: str) -> ET.Element:
    """A package part parsed, refused before reading past the part limits"""
    return parse(read_part(z, part))
