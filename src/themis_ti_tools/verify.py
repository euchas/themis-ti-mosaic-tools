"""Three-way integrity check for the quantitative GeoTIFF tiles.

1. structural: ``tifffile`` reads the TIFF tile index without decoding and
   flags tiles whose offset plus byte count lies beyond the end of the file
   (truncation; independent of any decoder).
2. decode: ``rasterio``/GDAL decompresses every 2048-row strip, which forces
   every internal 256 x 256 tile through libtiff; a strip that fails means
   truncated or corrupt LZW data.
3. value sanity: readable pixels outside the thermal-model envelope, or
   non-finite, point to silent corruption.

An optional fourth check decodes the file with Pillow, whose libtiff build is
independent of GDAL's.  A byte-size or checksum comparison against the
archive listing cannot find any of this: the corrupt tiles were uploaded
exactly as they are.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Iterable

from .manifest import NODATA, resolve_data_dir, tile_from_name

STRIP_ROWS = 2048
#: tolerant bounds around (TI_MIN, TI_MAX) that allow for float32 rounding
ENVELOPE = (19.99, 3016.0)
BACKUP_SUFFIX = ".CORRUPT.tif.bak"


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def structural_check(path: str | os.PathLike) -> dict:
    """Read the tile index with tifffile (no decoding) and count the tiles
    that extend past the end of the file."""
    import tifffile

    path = os.fspath(path)
    size = os.path.getsize(path)
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        ends = [int(o) + int(c) for o, c in zip(page.dataoffsets, page.databytecounts)]
    declared_end = max(ends) if ends else 0
    return {"ntiles": len(ends), "filesize": size, "declared_end": declared_end,
            "past_eof_tiles": sum(1 for e in ends if e > size),
            "truncated": declared_end > size}


def decode_check(path: str | os.PathLike, *, strip_rows: int = STRIP_ROWS,
                 envelope: tuple[float, float] = ENVELOPE, nodata: float | None = None) -> dict:
    """Decode every strip with rasterio and gather value statistics.  Memory
    use is one strip in float64 (about 0.6 GB for a full-width tile)."""
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    lo, hi = envelope
    with rasterio.open(os.fspath(path)) as ds:
        nd = ds.nodata if nodata is None else nodata
        if nd is None:
            nd = NODATA
        height, width = ds.height, ds.width
        strips = bad = nonfinite = anomalies = 0
        first_bad = None
        vmin, vmax = float("inf"), float("-inf")
        for top in range(0, height, strip_rows):
            rows = min(strip_rows, height - top)
            strips += 1
            try:
                a = ds.read(1, window=Window(0, top, width, rows)).astype("float64")
            except Exception:
                bad += 1
                if first_bad is None:
                    first_bad = top
                continue
            finite = np.isfinite(a)
            valid = finite & (a != nd)
            nonfinite += int((~finite).sum())
            anomalies += int((valid & ((a < lo) | (a > hi))).sum())
            phys = a[valid & (a >= lo) & (a <= hi)]
            if phys.size:
                vmin = min(vmin, float(phys.min()))
                vmax = max(vmax, float(phys.max()))
    return {"strips": strips, "bad_strips": bad, "first_bad_row": first_bad, "nodata": float(nd),
            "value_min": None if vmin == float("inf") else round(vmin, 3),
            "value_max": None if vmax == float("-inf") else round(vmax, 3),
            "nonfinite": nonfinite, "out_of_envelope": anomalies}


def pillow_check(path: str | os.PathLike) -> dict:
    """Decode the whole image with Pillow (a libtiff build independent of GDAL)."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(os.fspath(path)) as im:
            im.load()
            return {"ok": True, "size": list(im.size), "mode": im.mode}
    except Exception as exc:
        return {"ok": False, "error": _err(exc)}


def verify_tile(path: str | os.PathLike, *, pillow: bool = False, strip_rows: int = STRIP_ROWS,
                envelope: tuple[float, float] = ENVELOPE) -> dict:
    """Run the checks on one file and return a dict with a ``verdict``
    (``CLEAN`` or ``CORRUPT``) and the list of ``reasons``."""
    path = os.fspath(path)
    name = os.path.basename(path)
    res = {"file": name, "tile": tile_from_name(name),
           "role": "original" if name.endswith(BACKUP_SUFFIX) else "current",
           "filesize": os.path.getsize(path)}
    try:
        res["structural"] = structural_check(path)
    except Exception as exc:
        res["structural"] = {"error": _err(exc)}
    try:
        res["decode"] = decode_check(path, strip_rows=strip_rows, envelope=envelope)
    except Exception as exc:
        res["decode"] = {"error": _err(exc)}
    if pillow:
        res["pillow"] = pillow_check(path)

    s, d = res["structural"], res["decode"]
    reasons = []
    if "error" in s:
        reasons.append("structural-error")
    elif s["truncated"]:
        reasons.append(f"truncated:{s['past_eof_tiles']}-tiles-past-EOF")
    if "error" in d:
        reasons.append("decode-error")
    else:
        if d["bad_strips"]:
            reasons.append(f"decode:{d['bad_strips']}-bad-strips")
        anomalies = d["out_of_envelope"] + d["nonfinite"]
        if anomalies:
            reasons.append(f"value-anomalies:{anomalies}")
    if pillow and not res["pillow"]["ok"]:
        reasons.append("pillow-decode-failed")
    res["verdict"] = "CLEAN" if not reasons else "CORRUPT"
    res["reasons"] = reasons
    return res


def verify_many(paths: Iterable[str | os.PathLike], *, workers: int = 1,
                pillow: bool = False, **kwargs) -> list[dict]:
    """:func:`verify_tile` over several files, in processes when ``workers > 1``."""
    paths = [os.fspath(p) for p in paths]
    fn = partial(verify_tile, pillow=pillow, **kwargs)
    if workers <= 1 or len(paths) <= 1:
        return [fn(p) for p in paths]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, paths))


def find_tiles(data_dir: str | os.PathLike | None = None, include_backups: bool = True) -> list[Path]:
    """All ``*.tif`` (and ``*.CORRUPT.tif.bak``) files in the data directory."""
    d = resolve_data_dir(data_dir)
    files = sorted(d.glob("*.tif"))
    if include_backups:
        files += sorted(d.glob("*" + BACKUP_SUFFIX))
    return files
