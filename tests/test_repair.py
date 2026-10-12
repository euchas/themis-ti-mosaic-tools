import shutil

import numpy as np
import pytest

from conftest import clean_array, write_geotiff
from themis_ti_tools import NODATA, TI_MAX, TI_MIN, convert_cub_to_geotiff, repair_tile, verify_tile

pytest.importorskip("rasterio")
pytest.importorskip("tifffile")


def _cube_like(tmp_path, name="master.tif"):
    """A raster with ISIS special pixels and junk, as read from a .cub."""
    arr = clean_array(seed=3)
    arr[0, 0], arr[0, 1] = -3.4028235e38, 3.4028235e38      # ISIS NULL / HRS-style specials
    arr[0, 2], arr[0, 3] = 6.9e33, np.nan                   # junk and non-finite
    arr[1, 0], arr[1, 1] = 5.0, 5000.0                      # below / above the envelope
    arr[1, 2] = 300.0
    return write_geotiff(tmp_path / name, arr, nodata=None, compress=None, tiled=False), arr


def test_convert_masks_clamps_and_matches_tile_format(tmp_path):
    import rasterio

    src, arr = _cube_like(tmp_path)
    dst = tmp_path / "00N240E.recovered.tif"
    stats = convert_cub_to_geotiff(src, dst)
    with rasterio.open(dst) as ds:
        assert ds.dtypes[0] == "float32" and ds.nodata == NODATA
        assert ds.compression.name.lower() == "lzw" and ds.block_shapes == [(256, 256)]
        out = ds.read(1)
    assert np.all(out[0, :4] == np.float32(NODATA))
    assert out[1, 0] == np.float32(TI_MIN) and out[1, 1] == np.float32(TI_MAX) and out[1, 2] == 300.0
    assert np.all(out[2:50, :50] == np.float32(NODATA))       # NoData block of the input stays NoData
    assert stats["masked"] == 50 * 50 - 3 and stats["bad_strips"] == 0   # block minus the three test pixels
    assert stats["value_min"] == TI_MIN and stats["value_max"] == pytest.approx(TI_MAX, abs=1e-3)
    assert verify_tile(dst)["verdict"] == "CLEAN"


def test_repair_tile_swaps_in_the_rebuilt_geotiff(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    master, _ = _cube_like(tmp_path)
    corrupt = data / "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif"
    corrupt.write_bytes(b"not a tiff")
    fetched = []

    def fetch(url, dest, expected):
        fetched.append(url)
        shutil.copy(master, dest)

    log = []
    r = repair_tile("00N240E", data, fetch=fetch, log=log.append)
    assert r["status"] == "recovered" and r["bad_strips"] == 0
    assert fetched == ["https://asc-astropedia.s3.us-west-2.amazonaws.com/Mars/Odyssey/"
                       "THEMIS-Global-Thermal-Inertia-Mosaic/Quantitative-32-Bit/"
                       "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub"]
    assert (data / "00N240E.CORRUPT.tif.bak").read_bytes() == b"not a tiff"
    assert not (data / "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub").exists()
    assert not (data / "00N240E.recovered.tif").exists()
    assert verify_tile(corrupt)["verdict"] == "CLEAN"

    r2 = repair_tile("00N240E", data, fetch=fetch, log=log.append)   # idempotent: backup marks it done
    assert r2["status"] == "already-recovered" and len(fetched) == 1

    with pytest.raises(ValueError):
        repair_tile("99N000E", data, fetch=fetch)
