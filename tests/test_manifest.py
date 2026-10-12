from pathlib import Path

import pytest

from themis_ti_tools import manifest as M

P = "Mars/Odyssey/THEMIS-Global-Thermal-Inertia-Mosaic/"
LISTING = f"""<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>asc-astropedia</Name>
<Prefix>{P}</Prefix><KeyCount>4</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>
<Contents><Key>{P}Quantitative-32-Bit/THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub</Key>
<LastModified>2023-06-17T19:36:32.000Z</LastModified><ETag>&quot;abc-76&quot;</ETag><Size>2532510192</Size>
<StorageClass>INTELLIGENT_TIERING</StorageClass></Contents>
<Contents><Key>{P}Quantitative-32-Bit/ancillary/THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif</Key>
<LastModified>2023-06-17T19:44:42.000Z</LastModified><ETag>&quot;def-4&quot;</ETag><Size>217684352</Size>
<StorageClass>INTELLIGENT_TIERING</StorageClass></Contents>
<Contents><Key>{P}Quantitative-32-Bit/ancillary/THEMIS_TI_Mosaic_Quant_00N000E_100mpp.tif</Key>
<LastModified>2023-06-17T19:21:01.000Z</LastModified><ETag>&quot;ghi-40&quot;</ETag><Size>2652810020</Size>
<StorageClass>INTELLIGENT_TIERING</StorageClass></Contents>
<Contents><Key>{P}Qualitative-8-Bit/ancillary/THEMIS_TI_Mosaic_Qual_00N000E_100mpp.tif</Key>
<LastModified>2023-06-17T19:00:00.000Z</LastModified><ETag>&quot;jkl-1&quot;</ETag><Size>100</Size>
<StorageClass>STANDARD</StorageClass></Contents>
</ListBucketResult>"""


def test_tile_names_and_urls():
    assert len(M.TILES) == 24 and M.TILES[0] == "00N000E" and M.TILES[-1] == "60S300E"
    assert set(M.KNOWN_CORRUPT_TILES) <= set(M.TILES)
    assert M.tile_url("00N240E") == (M.BUCKET_URL + "/" + M.PREFIX
                                     + "Quantitative-32-Bit/ancillary/THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif")
    assert M.tile_url("00N240E", kind="cub").endswith(
        "/Quantitative-32-Bit/THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub")
    assert M.tile_filename("30S060E", "qual", "png") == "THEMIS_TI_Mosaic_Qual_30S060E_100mpp.png"
    assert M.tile_from_name("00N240E.CORRUPT.tif.bak") == "00N240E"
    assert M.tile_from_name(Path("x") / "THEMIS_TI_Mosaic_Quant_60S300E_100mpp.tif") == "60S300E"
    assert M.tile_from_name("readme.txt") is None
    with pytest.raises(ValueError):
        M.tile_filename("99N000E")
    with pytest.raises(ValueError):
        M.tile_filename("00N000E", kind="jp2")


def test_listing_to_manifest():
    objs = M.parse_listing(LISTING)
    assert len(objs) == 4
    assert objs[0].size == 2532510192 and objs[0].etag == "abc-76"
    rows = M.build_manifest(objs, "quant", "tif")
    assert [r.name for r in rows] == ["THEMIS_TI_Mosaic_Quant_00N000E_100mpp.tif",
                                      "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif"]
    assert rows[1].size == 217684352 and rows[1].url == M.tile_url("00N240E")
    assert [r.name for r in M.build_manifest(objs, "quant", "cub")] == [
        "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.cub"]
    assert M.build_manifest(objs, "qual", "tif")[0].name.startswith("THEMIS_TI_Mosaic_Qual_")

    class Paged:
        """Two listing pages joined by a continuation token."""
        calls = []

        def get(self, url, params=None, timeout=None):
            self.calls.append(dict(params))
            page = LISTING.replace("<IsTruncated>false</IsTruncated>",
                                   "<IsTruncated>true</IsTruncated><NextContinuationToken>tok</NextContinuationToken>")
            text = LISTING if "continuation-token" in params else page

            class R:
                status_code = 200

                def raise_for_status(self):
                    pass
            r = R()
            r.text = text
            return r

    objs2 = M.list_objects(session=Paged())
    assert len(objs2) == 8 and Paged.calls[1]["continuation-token"] == "tok"
    assert Paged.calls[0]["list-type"] == "2" and Paged.calls[0]["prefix"] == M.PREFIX


def test_manifest_file_roundtrip_bundle_and_data_dir(tmp_path, monkeypatch):
    rows = M.bundled_manifest()
    assert len(rows) == 24
    assert [M.tile_from_name(r.name) for r in rows] == list(M.TILES)
    assert all(r.size > 0 and r.url == M.tile_url(M.tile_from_name(r.name)) for r in rows)
    p = tmp_path / "m.tsv"
    M.write_manifest(rows[:3], p)
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())          # a BOM is tolerated
    assert M.read_manifest(p) == rows[:3]
    p.write_text("https://example.org/a/b.tif\t12\n", encoding="utf-8")   # name defaults to the URL tail
    assert M.read_manifest(p) == [M.ManifestRow("https://example.org/a/b.tif", 12, "b.tif")]

    monkeypatch.delenv(M.ENV_DATA_DIR, raising=False)
    assert M.resolve_data_dir(None) == Path(".")
    monkeypatch.setenv(M.ENV_DATA_DIR, str(tmp_path))
    assert M.resolve_data_dir() == tmp_path
    assert M.resolve_data_dir(tmp_path / "x") == tmp_path / "x"
