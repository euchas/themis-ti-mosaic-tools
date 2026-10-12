# themis-ti-mosaic-tools

Download, verify and repair the quantitative tiles of the USGS/ASU
*Mars THEMIS-Derived Global Thermal Inertia Mosaic* (100 m/pixel, 24 GeoTIFF
tiles, about 45 GiB) from the public USGS Astrogeology S3 bucket.

| Command | What it does |
|---|---|
| `themis-ti-tools manifest` | lists the bucket and writes a download manifest (URL, byte size, file name) |
| `themis-ti-tools download` | resumable, size-verified download of the 24 quantitative GeoTIFFs or a subset |
| `themis-ti-tools verify` | three-way integrity check of each GeoTIFF: TIFF tile index, full decode, value envelope |
| `themis-ti-tools repair` | rebuilds a tile from its intact ISIS `.cub` master and swaps it in |

The tools exist because seven of the 24 published GeoTIFF tiles were found
to be corrupt in 2026 although their byte sizes match the archive listing
(see [The corrupt tiles](#the-corrupt-tiles)). The finding has been reported
to the USGS Astrogeology Science Center; until the files are regenerated,
`verify` shows which tiles not to trust and `repair` rebuilds them.

## Install

```bash
pip install git+https://github.com/euchas/themis-ti-mosaic-tools              # manifest, download
pip install "themis-ti-mosaic-tools[raster] @ git+https://github.com/euchas/themis-ti-mosaic-tools"  # + verify, repair
```

The `raster` extra brings numpy, rasterio and tifffile; `pillow` adds the
optional Pillow cross-check. Requires Python 3.10 or newer. For development:
`git clone`, then `pip install -e .[test]` and `pytest -q` (the tests build
their own small GeoTIFFs and need neither network access nor mission data).

## Quickstart

```bash
export THEMIS_TI_DATA_DIR=/data/themis_ti          # or pass --data-dir to every command
themis-ti-tools download                           # all 24 quantitative GeoTIFFs, resumable
themis-ti-tools download --tiles 00N240E 30S060E   # a subset
themis-ti-tools verify --workers 4 --json verify_results.json
themis-ti-tools repair --known                     # the seven tiles listed below
themis-ti-tools manifest --kind cub -o cubes.tsv   # fresh listing of the ISIS masters
```

The same functions are available from Python (`download_manifest`,
`verify_tile`, `repair_tile`, ...). Exit status 1 means a tile failed.

## Data source

* Catalogue page: https://astrogeology.usgs.gov/search/map/mars-themis-derived-global-thermal-inertia-mosaic
  (USGS Astrogeology Science Center with Arizona State University; method of
  Fergason et al. 2006).
* Bucket `https://asc-astropedia.s3.us-west-2.amazonaws.com/`, prefix
  `Mars/Odyssey/THEMIS-Global-Thermal-Inertia-Mosaic/`, anonymous HTTPS:
  `Quantitative-32-Bit/` holds the 24 ISIS `.cub` masters and its
  `ancillary/` the GeoTIFF and PNG versions; `Qualitative-8-Bit/` (a display
  product) is laid out the same way.
* Tiles of 30 x 60 degrees between 60S and 60N, named by their south-west
  corner (`00N000E` ... `60S300E`): `THEMIS_TI_Mosaic_Quant_<tile>_100mpp.tif`,
  float32 apparent thermal inertia in J m-2 K-1 s-1/2, Mars equirectangular
  projection (sphere radius 3,396,190 m), about 35,565 x 17,783 pixels, NoData
  -3.4028226550889045e+38, LZW, 256 x 256 internal tiles, values within
  [20.0, 3015.906]. The bundled manifest carries the sizes the bucket reported
  in June 2026; `themis-ti-tools manifest` fetches a fresh listing.

## The three-way check

A size or checksum comparison against the archive listing proves a faithful
download, not a valid file. `verify` runs three independent checks per file:

1. **Structural** (`tifffile`, no decoding): reads the TIFF tile index and
   counts tiles whose offset plus byte count lies past the end of the file.
   Catches truncation only.
2. **Decode** (`rasterio`/GDAL): reads every 2048-row strip, which forces every
   internal tile through libtiff. Catches truncation and corrupt LZW data
   (`LZWDecode: Using code not yet in table`, `TIFFReadEncodedTile() failed`).
3. **Value sanity** (`numpy`): counts readable pixels that are non-finite or
   outside [19.99, 3016.0], NoData excluded. Catches silent corruption.

`--pillow` adds a decode with Pillow's own libtiff build, independent of
GDAL. A file is `CLEAN` only when every check passes; each worker holds one
strip in float64, about 0.6 GB for a full-width tile.

## The corrupt tiles

Seven quantitative GeoTIFFs failed these checks in June 2026 with byte sizes
identical to the listing and byte-identical re-downloads, so the defect is in
the published files. The `.cub` masters of the same tiles are intact and
convert cleanly, which points at the GeoTIFF generation step, not the data.

| Tile | Failure | Detail |
|---|---|---|
| `00N060E` | truncated | 8,515 of 9,730 internal tiles past the end of the file (about 89 %) |
| `00N240E` | truncated | 8,896 of 9,730 past the end of the file; only the first 834 readable |
| `00N000E` | corrupt LZW data | 2 of 9 strips fail to decode |
| `00N300E` | corrupt LZW data | 1 of 9 strips |
| `30N060E` | corrupt LZW data | 1 of 9 strips |
| `30N300E` | corrupt LZW data | 1 of 9 strips |
| `30S060E` | corrupt LZW data | 1 of 9 strips |

The two truncated tiles are caught by the structural check, the other five
only by decoding; the remaining 17 tiles pass everything.

`repair` downloads the tile's `.cub` (resumable, size checked against the
bucket), masks non-finite and ISIS special pixels (|value| > 1e4; GDAL's
ISIS3 driver only maps NULL, so the other special values of about +/-3.4e38
would otherwise leak through), clamps the rest to [20.0, 3015.906], writes a
float32 LZW 256 x 256 tiled GeoTIFF with the master's georeferencing and the
product's NoData, decode-checks it, moves the corrupt file to
`<tile>.CORRUPT.tif.bak` and installs the rebuilt one; a tile with a `.bak`
is skipped on re-runs. Rebuilt tiles hold the same values on the same grid
but are not byte-identical to USGS output.

## Known limitations

* The value envelope and the special-pixel threshold were read off the clean
  published tiles (spikes at exactly 20.0 and 3015.906), not taken from
  product documentation; `verify` is meaningful for the quantitative product only.
* `download` verifies byte sizes only (the bucket's ETags are multipart
  hashes, not file checksums); run `verify` afterwards.
* Listing and HEAD requests are anonymous and rely on the bucket staying
  public; the client sends the User-Agent
  `themis-ti-mosaic-tools/0.1.0 (+https://github.com/euchas/themis-ti-mosaic-tools)`.

## Citing

If this software is useful in your work, please cite it (see `CITATION.cff`):

> Valantinas, A. (2026). themis-ti-mosaic-tools: download, verify and repair
> the THEMIS-derived global thermal inertia mosaic of Mars (version 0.1.0).
> https://github.com/euchas/themis-ti-mosaic-tools

and the instrument and product it serves:

* THEMIS: Christensen, P. R. et al. (2004), Space Science Reviews 110, 85-130,
  https://doi.org/10.1023/B:SPAC.0000021008.16305.94.
* Thermal inertia derivation: Fergason, R. L., Christensen, P. R. and
  Kieffer, H. H. (2006), Journal of Geophysical Research 111, E12004,
  https://doi.org/10.1029/2006JE002735.
* The mosaic: USGS Astrogeology Science Center and Arizona State University,
  Mars THEMIS-Derived Global Thermal Inertia Mosaic,
  https://astrogeology.usgs.gov/search/map/mars-themis-derived-global-thermal-inertia-mosaic.

## Acknowledgement

Much of the code was written with an AI coding assistant (Claude, Anthropic)
under the direction of the author, who reviewed the code and results.

## License

MIT, see `LICENSE`.
