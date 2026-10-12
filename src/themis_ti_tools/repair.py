"""Rebuild a quantitative GeoTIFF tile from its ISIS ``.cub`` master.

The ``.cub`` masters in the bucket are intact, so a corrupt ancillary GeoTIFF
can be regenerated locally: download the cube (resumable, size checked
against the bucket), mask ISIS special pixels and junk to NoData, clamp to
the thermal-model envelope, write a float32 LZW-compressed 256 x 256 tiled
GeoTIFF with the same CRS and NoData as the published tiles, decode-check
the result, then swap it in while keeping the corrupt original as
``<tile>.CORRUPT.tif.bak``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .download import download_file
from .manifest import (JUNK_ABS, NODATA, TI_MAX, TI_MIN, remote_size, resolve_data_dir,
                       tile_filename, tile_url)
from .verify import BACKUP_SUFFIX, STRIP_ROWS, decode_check


class RepairError(RuntimeError):
    """The rebuilt GeoTIFF did not pass the decode check."""


def convert_cub_to_geotiff(src: str | os.PathLike, dst: str | os.PathLike, *,
                           lo: float = TI_MIN, hi: float = TI_MAX, junk: float = JUNK_ABS,
                           nodata: float = NODATA, strip_rows: int = STRIP_ROWS,
                           log: Callable[[str], None] | None = None) -> dict:
    """Convert ``src`` (ISIS cube, or any raster GDAL can read) to a GeoTIFF
    matching the published quantitative tiles and decode-check it.

    Pixels that are non-finite or have ``|value| > junk`` (ISIS special
    pixels such as NULL, LRS, HRS at about +/-3.4e38, and stray junk) become
    ``nodata``; the rest is clamped to ``[lo, hi]``.  GDAL's ISIS3 driver
    only maps the NULL value, so the other special pixels must be removed
    here or they leak into the GeoTIFF as absurd values.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    say = log or (lambda msg: None)
    src, dst = os.fspath(src), os.fspath(dst)
    with rasterio.open(src) as s:
        height, width = s.height, s.width
        profile = s.profile.copy()
        profile.update(driver="GTiff", dtype="float32", count=1, compress="lzw", tiled=True,
                       blockxsize=256, blockysize=256, interleave="band", BIGTIFF="IF_SAFER",
                       nodata=nodata)
        vmin, vmax, valid, masked = float("inf"), float("-inf"), 0, 0
        with rasterio.open(dst, "w", **profile) as d:
            for top in range(0, height, strip_rows):
                rows = min(strip_rows, height - top)
                win = Window(0, top, width, rows)
                a = s.read(1, window=win).astype("float32")   # raises if the cube is damaged
                bad = ~np.isfinite(a) | (np.abs(a) > junk)
                out = np.where(bad, np.float32(nodata), np.clip(a, lo, hi)).astype("float32")
                d.write(out, 1, window=win)
                good = out[~bad]
                if good.size:
                    vmin = min(vmin, float(good.min()))
                    vmax = max(vmax, float(good.max()))
                    valid += int(good.size)
                masked += int(bad.sum())
    say(f"{Path(dst).name}: valid {100.0 * valid / (height * width):.2f} %, masked {masked}, "
        f"range [{vmin:.2f}, {vmax:.2f}]")
    check = decode_check(dst, strip_rows=strip_rows)
    result = {"valid": valid, "valid_fraction": valid / (height * width), "masked": masked,
              "value_min": None if valid == 0 else vmin, "value_max": None if valid == 0 else vmax,
              "strips": check["strips"], "bad_strips": check["bad_strips"],
              "size": os.path.getsize(dst)}
    if check["bad_strips"]:
        raise RepairError(f"{dst}: {check['bad_strips']} of {check['strips']} strips unreadable after conversion")
    return result


def repair_tile(tile: str, data_dir: str | os.PathLike | None = None, *, keep_cub: bool = False,
                session=None, fetch: Callable | None = None, convert: Callable | None = None,
                log: Callable[[str], None] | None = None) -> dict:
    """Download the ``.cub`` master of ``tile``, convert it and install the
    result as the tile's GeoTIFF in ``data_dir``.

    Idempotent: a tile whose ``<tile>.CORRUPT.tif.bak`` already exists is
    treated as recovered and skipped.  The cube is deleted afterwards unless
    ``keep_cub``.  ``fetch(url, dest, expected_size)`` and
    ``convert(src, dst, log=...)`` can be injected (tests).
    """
    data_dir = resolve_data_dir(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    say = log or (lambda msg: None)
    cub = data_dir / tile_filename(tile, "quant", "cub")
    tif = data_dir / tile_filename(tile, "quant", "tif")
    bak = data_dir / f"{tile}{BACKUP_SUFFIX}"
    new = data_dir / f"{tile}.recovered.tif"
    if bak.exists():
        say(f"{tile}: {bak.name} exists, already recovered; skipping")
        return {"tile": tile, "status": "already-recovered", "geotiff": str(tif), "corrupt_backup": str(bak)}

    url = tile_url(tile, "quant", "cub")
    convert = convert or convert_cub_to_geotiff
    try:
        if fetch is None:
            try:
                expected = remote_size(url, session=session)
            except Exception as exc:  # no HEAD: download without a size check
                say(f"{tile}: could not read the cube size from the bucket ({exc})")
                expected = None
            say(f"{tile}: downloading {cub.name}" + (f" ({expected:,} bytes, resumable)" if expected else ""))
            download_file(url, cub, expected, session=session, log=log)
        else:
            fetch(url, cub, None)
        say(f"{tile}: converting {cub.name} and checking the result")
        stats = convert(cub, new, log=log)
        if tif.exists():
            tif.replace(bak)          # keep the corrupt original
        new.replace(tif)
        if not keep_cub:
            cub.unlink()
    except BaseException:
        if new.exists():
            new.unlink()
        raise
    say(f"{tile}: recovered -> {tif.name}" + (f", original kept as {bak.name}" if bak.exists() else ""))
    return {"tile": tile, "status": "recovered", "geotiff": str(tif),
            "corrupt_backup": str(bak) if bak.exists() else None, **stats}
