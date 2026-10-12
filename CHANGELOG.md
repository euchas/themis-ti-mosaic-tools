# Changelog

## 0.1.0 (2026-10-12)

First public release, consolidated from the scripts used to download and
check the quantitative product in 2026:

- `manifest`: anonymous listing of the `asc-astropedia` bucket prefix and a
  tab-separated download manifest (URL, size, file name); the quantitative
  GeoTIFF manifest is bundled.
- `download`: resumable HTTP download with size verification and a
  descriptive User-Agent (replaces a PowerShell/curl script).
- `verify`: structural (tile index), decode (every strip through libtiff)
  and value-envelope checks, optional Pillow cross-check, JSON report,
  parallel workers.
- `repair`: rebuild a GeoTIFF tile from its ISIS `.cub` master (mask special
  pixels, clamp to the model envelope, LZW tiled float32), decode-check and
  swap in, keeping the corrupt original as `<tile>.CORRUPT.tif.bak`.
