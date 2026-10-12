"""Command line interface: ``themis-ti-tools {manifest,download,verify,repair}``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._version import __version__
from .download import DownloadError, download_manifest
from .manifest import (ENV_DATA_DIR, KNOWN_CORRUPT_TILES, PREFIX, build_manifest, bundled_manifest,
                       list_objects, read_manifest, write_manifest)
from .repair import RepairError, repair_tile
from .verify import find_tiles, verify_many


def _add_data_dir(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data-dir", metavar="DIR",
                   help=f"directory holding the tiles (default: ${ENV_DATA_DIR} or the current directory)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="themis-ti-tools",
        description="Download, verify and repair the USGS/ASU THEMIS-derived global thermal "
                    "inertia mosaic tiles of Mars.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    m = sub.add_parser("manifest", help="list the bucket and write a download manifest (url, size, name)")
    m.add_argument("--product", choices=("quant", "qual"), default="quant",
                   help="quantitative 32-bit (default) or qualitative 8-bit product")
    m.add_argument("--kind", choices=("tif", "cub", "png"), default="tif",
                   help="ancillary GeoTIFF (default), ISIS cube master or PNG browse")
    m.add_argument("-o", "--out", metavar="FILE", help="output TSV (default: stdout)")

    d = sub.add_parser("download", help="download the quantitative GeoTIFF tiles (resumable, size-verified)")
    d.add_argument("--manifest", metavar="FILE",
                   help="TSV written by 'manifest' (default: the bundled quantitative GeoTIFF manifest)")
    d.add_argument("--tiles", nargs="+", metavar="TILE", help="only these tiles, e.g. 00N240E 30S060E")
    _add_data_dir(d)

    v = sub.add_parser("verify", help="structural, decode and value check of GeoTIFF tiles")
    v.add_argument("paths", nargs="*", metavar="FILE",
                   help="files to check (default: every *.tif and *.CORRUPT.tif.bak in the data directory)")
    v.add_argument("--workers", type=int, default=1, help="parallel processes (default 1)")
    v.add_argument("--pillow", action="store_true", help="also decode with Pillow (independent libtiff build)")
    v.add_argument("--json", metavar="FILE", help="write the full results as JSON")
    _add_data_dir(v)

    r = sub.add_parser("repair", help="rebuild GeoTIFF tiles from their ISIS .cub masters")
    r.add_argument("tiles", nargs="*", metavar="TILE", help="tile ids, e.g. 00N240E")
    r.add_argument("--known", action="store_true",
                   help="the tiles found corrupt in the published product: " + " ".join(KNOWN_CORRUPT_TILES))
    r.add_argument("--keep-cub", action="store_true", help="keep the downloaded .cub after conversion")
    _add_data_dir(r)
    return p


def cmd_manifest(a: argparse.Namespace) -> int:
    rows = build_manifest(list_objects(PREFIX), a.product, a.kind)
    if a.out:
        write_manifest(rows, a.out)
        print(f"{len(rows)} rows -> {a.out}")
    else:
        write_manifest(rows, sys.stdout)
    return 0


def cmd_download(a: argparse.Namespace) -> int:
    rows = read_manifest(a.manifest) if a.manifest else bundled_manifest()
    report = download_manifest(rows, a.data_dir, tiles=a.tiles, log=print)
    return 1 if report.failed else 0


def cmd_verify(a: argparse.Namespace) -> int:
    paths = [Path(p) for p in a.paths] or find_tiles(a.data_dir)
    if not paths:
        print("no GeoTIFF files found", file=sys.stderr)
        return 2
    results = verify_many(paths, workers=a.workers, pillow=a.pillow)
    for r in results:
        print(f"{(r['tile'] or r['file']):9s} {r['role']:9s} {r['verdict']:7s} {', '.join(r['reasons']) or 'ok'}")
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    n_bad = sum(r["verdict"] != "CLEAN" for r in results)
    print(f"{len(results) - n_bad}/{len(results)} clean")
    return 1 if n_bad else 0


def cmd_repair(a: argparse.Namespace) -> int:
    tiles = list(a.tiles) + (list(KNOWN_CORRUPT_TILES) if a.known else [])
    if not tiles:
        print("give tile ids or --known", file=sys.stderr)
        return 2
    failed = []
    for tile in dict.fromkeys(tiles):
        try:
            repair_tile(tile, a.data_dir, keep_cub=a.keep_cub, log=print)
        except (DownloadError, RepairError, OSError, ValueError) as exc:
            failed.append(tile)
            print(f"{tile}: FAILED: {exc}", file=sys.stderr)
    if failed:
        print(f"failed: {' '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return {"manifest": cmd_manifest, "download": cmd_download,
            "verify": cmd_verify, "repair": cmd_repair}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
