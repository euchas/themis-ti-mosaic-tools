"""Shared fixtures: small synthetic GeoTIFF tiles in the format of the
published quantitative product (float32, LZW, 256 x 256 tiles, NoData).  No
network access and no mission data are needed."""
from __future__ import annotations

import numpy as np
import pytest

from themis_ti_tools import NODATA

MARS_EQC = "+proj=eqc +lat_ts=0 +lat_0=0 +lon_0=0 +x_0=0 +y_0=0 +R=3396190 +units=m +no_defs"


def write_geotiff(path, arr, nodata=NODATA, **extra):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    profile = dict(driver="GTiff", dtype="float32", count=1, height=arr.shape[0],
                   width=arr.shape[1], crs=MARS_EQC, transform=from_origin(0, 0, 100, 100),
                   nodata=nodata, compress="lzw", tiled=True, blockxsize=256, blockysize=256)
    profile.update(extra)
    if not profile.get("tiled"):
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
    profile = {k: v for k, v in profile.items() if v is not None}
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(np.asarray(arr, dtype="float32"), 1)
    return path


def clean_array(shape=(600, 600), seed=0):
    rng = np.random.default_rng(seed)
    arr = rng.uniform(20.0, 3015.0, size=shape).astype("float32")
    arr[:50, :50] = NODATA
    return arr


@pytest.fixture
def clean_tile(tmp_path):
    """A valid 600 x 600 tile (3 x 3 internal tiles, one 2048-row strip)."""
    return write_geotiff(tmp_path / "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif", clean_array())


@pytest.fixture
def truncated_tile(tmp_path):
    """A copy of a valid tile cut off at 40 % of its length."""
    src = write_geotiff(tmp_path / "full.tif", clean_array(seed=1))
    data = src.read_bytes()
    dst = tmp_path / "00N060E.CORRUPT.tif.bak"
    dst.write_bytes(data[: int(len(data) * 0.4)])
    return dst
