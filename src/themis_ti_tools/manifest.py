"""Tile names, bucket listing and download manifests for the USGS/ASU Mars
THEMIS-derived global thermal inertia mosaic.

The mosaic is published in the public ``asc-astropedia`` S3 bucket of the USGS
Astrogeology Science Center (anonymous HTTPS access, no credentials).  Two
products exist, ``Quantitative-32-Bit`` (apparent thermal inertia, float32)
and ``Qualitative-8-Bit`` (normalised for display); each has 24 ISIS ``.cub``
masters in the product directory and the derived GeoTIFF and PNG versions
under ``ancillary/``.

Tiles are 30 degrees of latitude by 60 degrees of longitude and are named by
their south-west corner: ``30S060E`` covers 30S-0N, 60E-120E.
"""
from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from importlib import resources
from pathlib import Path
from typing import Iterable, NamedTuple

from ._http import make_session

BUCKET_URL = "https://asc-astropedia.s3.us-west-2.amazonaws.com"
PREFIX = "Mars/Odyssey/THEMIS-Global-Thermal-Inertia-Mosaic/"
CATALOG_URL = (
    "https://astrogeology.usgs.gov/search/map/"
    "mars-themis-derived-global-thermal-inertia-mosaic"
)
#: product key -> (bucket directory, file-name tag)
PRODUCTS = {"quant": ("Quantitative-32-Bit", "Quant"), "qual": ("Qualitative-8-Bit", "Qual")}
KINDS = ("tif", "cub", "png")

ROWS = ("00N", "30N", "30S", "60S")
COLS = ("000E", "060E", "120E", "180E", "240E", "300E")
#: the 24 tile names in bucket order
TILES = tuple(f"{r}{c}" for r in ROWS for c in COLS)
#: quantitative GeoTIFF tiles found corrupt in the published product (two
#: truncated files, five with corrupt LZW tile data; see the README)
KNOWN_CORRUPT_TILES = ("00N000E", "00N060E", "00N240E", "00N300E", "30N060E", "30N300E", "30S060E")

#: NoData value of the quantitative GeoTIFFs (the float32 minimum)
NODATA = -3.4028226550889045e+38
#: thermal-model envelope of the quantitative product, J m-2 K-1 s-1/2
TI_MIN = 20.0
TI_MAX = 3015.906
#: |value| above this in a .cub is an ISIS special pixel or junk, not thermal inertia
JUNK_ABS = 1.0e4

ENV_DATA_DIR = "THEMIS_TI_DATA_DIR"
BUNDLED_MANIFEST = "quant_geotiff_manifest.tsv"

_TILE_RE = re.compile(r"(?<![0-9A-Za-z])(\d{2}[NS]\d{3}E)(?![0-9A-Za-z])")


class ManifestRow(NamedTuple):
    url: str
    size: int
    name: str


class S3Object(NamedTuple):
    key: str
    size: int
    last_modified: str
    etag: str


# ----------------------------------------------------------------------------
# names and URLs
# ----------------------------------------------------------------------------
def tile_filename(tile: str, product: str = "quant", kind: str = "tif") -> str:
    """``THEMIS_TI_Mosaic_Quant_30S060E_100mpp.tif`` and friends."""
    if tile not in TILES:
        raise ValueError(f"unknown tile {tile!r}; expected one of {', '.join(TILES)}")
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}; expected 'quant' or 'qual'")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    return f"THEMIS_TI_Mosaic_{PRODUCTS[product][1]}_{tile}_100mpp.{kind}"


def tile_key(tile: str, product: str = "quant", kind: str = "tif") -> str:
    """Object key inside the bucket (``.cub`` masters sit in the product
    directory, GeoTIFF and PNG versions under ``ancillary/``)."""
    name = tile_filename(tile, product, kind)
    sub = "" if kind == "cub" else "ancillary/"
    return f"{PREFIX}{PRODUCTS[product][0]}/{sub}{name}"


def tile_url(tile: str, product: str = "quant", kind: str = "tif") -> str:
    """Public HTTPS URL of one object."""
    return f"{BUCKET_URL}/{tile_key(tile, product, kind)}"


def tile_from_name(name: str | os.PathLike) -> str | None:
    """Extract the tile id (``00N240E``) from a file name or key, or None."""
    m = _TILE_RE.search(os.path.basename(os.fspath(name)))
    return m.group(1) if m else None


def resolve_data_dir(path: str | os.PathLike | None = None) -> Path:
    """Directory holding the tiles: the argument, else ``$THEMIS_TI_DATA_DIR``,
    else the current directory."""
    if path is None or str(path) == "":
        path = os.environ.get(ENV_DATA_DIR) or "."
    return Path(path).expanduser()


# ----------------------------------------------------------------------------
# bucket listing
# ----------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse(xml_text: str) -> tuple[list[S3Object], str | None]:
    root = ET.fromstring(xml_text)
    objects: list[S3Object] = []
    token = None
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "Contents":
            fields = {_local(c.tag): (c.text or "") for c in el}
            objects.append(S3Object(fields.get("Key", ""), int(fields.get("Size") or 0),
                                    fields.get("LastModified", ""),
                                    fields.get("ETag", "").strip('"')))
        elif tag == "NextContinuationToken":
            token = el.text
    return objects, token


def parse_listing(xml_text: str) -> list[S3Object]:
    """Parse one S3 ``ListBucketResult`` document."""
    return _parse(xml_text)[0]


def list_objects(prefix: str = PREFIX, session=None, timeout: float = 60.0) -> list[S3Object]:
    """List every object under ``prefix`` in the public bucket (anonymous
    ``GET ?list-type=2``, following continuation tokens)."""
    s = make_session(session)
    objects: list[S3Object] = []
    params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
    while True:
        r = s.get(f"{BUCKET_URL}/", params=params, timeout=timeout)
        r.raise_for_status()
        page, token = _parse(r.text)
        objects.extend(page)
        if not token:
            return objects
        params["continuation-token"] = token


def build_manifest(objects: Iterable[S3Object], product: str = "quant",
                   kind: str = "tif") -> list[ManifestRow]:
    """Select one product and file kind from a listing and turn it into
    manifest rows sorted by file name."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    head = f"{PREFIX}{PRODUCTS[product][0]}/" + ("" if kind == "cub" else "ancillary/")
    rows = [ManifestRow(f"{BUCKET_URL}/{o.key}", o.size, o.key.rsplit("/", 1)[-1])
            for o in objects
            if o.key.startswith(head) and o.key.endswith("." + kind)
            and "/" not in o.key[len(head):]]
    return sorted(rows, key=lambda r: r.name)


def remote_size(url: str, session=None, timeout: float = 60.0) -> int | None:
    """``Content-Length`` of an object from a HEAD request, or None."""
    s = make_session(session)
    r = s.head(url, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    length = r.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


# ----------------------------------------------------------------------------
# manifest files (tab separated: url, size, name)
# ----------------------------------------------------------------------------
def _parse_manifest_text(text: str) -> list[ManifestRow]:
    rows = []
    for ln, line in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 2:
            raise ValueError(f"manifest line {ln}: expected 'url<TAB>size[<TAB>name]'")
        url, size = parts[0], int(parts[1])
        name = parts[2] if len(parts) > 2 and parts[2] else url.rsplit("/", 1)[-1]
        rows.append(ManifestRow(url, size, name))
    return rows


def read_manifest(path: str | os.PathLike) -> list[ManifestRow]:
    """Read a manifest written by :func:`write_manifest` (a byte-order mark
    and ``#`` comment lines are tolerated)."""
    return _parse_manifest_text(Path(path).read_text(encoding="utf-8-sig"))


def write_manifest(rows: Iterable[ManifestRow], dest, header: bool = True) -> None:
    """Write rows as ``url<TAB>size<TAB>name`` lines to a path or text stream."""
    lines = []
    if header:
        lines.append("# url\tsize_bytes\tfilename")
    lines.extend(f"{r.url}\t{r.size}\t{r.name}" for r in rows)
    text = "\n".join(lines) + "\n"
    if isinstance(dest, io.TextIOBase):
        dest.write(text)
    else:
        Path(dest).write_text(text, encoding="utf-8")


def bundled_manifest() -> list[ManifestRow]:
    """The 24 quantitative GeoTIFF tiles with the byte sizes the bucket listed
    when this package was written; ``themis-ti-tools manifest`` fetches a
    fresh listing."""
    text = resources.files(__package__).joinpath("data", BUNDLED_MANIFEST).read_text(encoding="utf-8")
    return _parse_manifest_text(text)
