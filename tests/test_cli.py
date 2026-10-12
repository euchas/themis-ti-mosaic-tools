import json

import pytest

from themis_ti_tools import cli
from themis_ti_tools import manifest as M

LISTING_KEYS = [
    ("Quantitative-32-Bit/ancillary/THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif", 217684352),
    ("Quantitative-32-Bit/THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub", 2532510192),
]


def test_manifest_verify_and_repair_commands(tmp_path, monkeypatch, capsys):
    objs = [M.S3Object(M.PREFIX + k, n, "", "") for k, n in LISTING_KEYS]
    monkeypatch.setattr(cli, "list_objects", lambda prefix: objs)
    out = tmp_path / "m.tsv"
    assert cli.main(["manifest", "-o", str(out)]) == 0
    assert M.read_manifest(out) == [M.ManifestRow(M.tile_url("00N240E"), 217684352,
                                                  "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif")]
    assert cli.main(["manifest", "--kind", "cub"]) == 0
    assert "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub" in capsys.readouterr().out

    pytest.importorskip("rasterio")
    pytest.importorskip("tifffile")
    from conftest import clean_array, write_geotiff

    data = tmp_path / "tiles"
    data.mkdir()
    good = write_geotiff(data / "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif", clean_array())
    bad = data / "00N060E.CORRUPT.tif.bak"
    bad.write_bytes(good.read_bytes()[:3000])
    report = tmp_path / "verify.json"
    assert cli.main(["verify", "--data-dir", str(data), "--json", str(report)]) == 1
    printed = capsys.readouterr().out
    assert "00N240E   current   CLEAN" in printed and "00N060E   original  CORRUPT" in printed
    assert "1/2 clean" in printed
    assert [r["verdict"] for r in json.loads(report.read_text())] == ["CLEAN", "CORRUPT"]
    assert cli.main(["verify", str(good)]) == 0

    calls = []
    monkeypatch.setattr(cli, "repair_tile", lambda tile, d, keep_cub, log: calls.append((tile, d, keep_cub)))
    assert cli.main(["repair", "--known", "--data-dir", str(data), "--keep-cub"]) == 0
    assert [c[0] for c in calls] == list(M.KNOWN_CORRUPT_TILES) and calls[0][1:] == (str(data), True)
    assert cli.main(["repair"]) == 2
