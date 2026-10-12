import numpy as np
import pytest

from conftest import clean_array, write_geotiff
from themis_ti_tools import NODATA, verify_many, verify_tile

pytest.importorskip("rasterio")
pytest.importorskip("tifffile")


def test_clean_tile_is_clean(clean_tile):
    r = verify_tile(clean_tile)
    assert r["verdict"] == "CLEAN" and r["reasons"] == []
    assert r["tile"] == "00N240E" and r["role"] == "current"
    assert r["structural"]["ntiles"] == 9 and r["structural"]["past_eof_tiles"] == 0
    assert r["decode"]["strips"] == 1 and r["decode"]["bad_strips"] == 0
    assert r["decode"]["nodata"] == NODATA
    assert 20.0 <= r["decode"]["value_min"] < 21.0 and 3014.0 < r["decode"]["value_max"] <= 3015.0
    assert r["decode"]["out_of_envelope"] == 0 and r["decode"]["nonfinite"] == 0


def test_truncated_tile_is_caught_structurally_and_by_decoding(truncated_tile):
    r = verify_tile(truncated_tile)
    assert r["verdict"] == "CORRUPT"
    assert r["tile"] == "00N060E" and r["role"] == "original"
    assert r["structural"]["truncated"] and r["structural"]["past_eof_tiles"] > 0
    assert r["decode"]["bad_strips"] == 1 and r["decode"]["first_bad_row"] == 0
    assert any(s.startswith("truncated:") for s in r["reasons"])
    assert any(s.startswith("decode:") for s in r["reasons"])


def test_value_anomalies_pillow_and_many(tmp_path, clean_tile):
    arr = clean_array(seed=2)
    arr[100, 100] = 5000.0                      # above the envelope
    arr[100, 101] = 5.0                         # below the envelope
    arr[200, 200] = np.nan                      # non-finite
    bad = write_geotiff(tmp_path / "THEMIS_TI_Mosaic_Quant_30N060E_100mpp.tif", arr)
    r = verify_tile(bad)
    assert r["verdict"] == "CORRUPT" and r["reasons"] == ["value-anomalies:3"]
    assert r["decode"]["out_of_envelope"] == 2 and r["decode"]["nonfinite"] == 1
    assert r["structural"]["truncated"] is False and r["decode"]["bad_strips"] == 0

    results = verify_many([clean_tile, bad], workers=1)
    assert [x["verdict"] for x in results] == ["CLEAN", "CORRUPT"]

    pytest.importorskip("PIL")
    r = verify_tile(clean_tile, pillow=True)
    assert r["pillow"]["ok"] and r["pillow"]["size"] == [600, 600] and r["verdict"] == "CLEAN"
