"""Resumable download logic against an in-memory fake server (no network)."""
from __future__ import annotations

import pytest
import requests

from themis_ti_tools import ManifestRow, download_file, download_manifest
from themis_ti_tools.download import DownloadError

PAYLOAD = bytes(range(256)) * 40          # 10,240 bytes


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status_code, self.body, self.headers = status, body, headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]


class FakeServer:
    """Serves ``payload`` with Range support.  ``cut_first`` delivers only
    that many bytes of the first response (a dropped connection);
    ``honour_range=False`` always answers with the whole file."""

    def __init__(self, payload=PAYLOAD, honour_range=True, cut_first=None, status=None):
        self.payload, self.honour_range, self.cut, self.status = payload, honour_range, cut_first, status
        self.requests = []
        self.headers = {}

    def get(self, url, headers=None, stream=False, timeout=None):
        headers = dict(headers or {})
        self.requests.append(headers)
        if self.status:
            return FakeResponse(self.status, b"", {})
        n = len(self.payload)
        if "Range" in headers and self.honour_range:
            start = int(headers["Range"].split("=")[1].rstrip("-"))
            if start >= n:
                return FakeResponse(416, b"", {"Content-Range": f"bytes */{n}"})
            body = self.payload[start:]
            status = 206
            hdr = {"Content-Range": f"bytes {start}-{n - 1}/{n}", "Content-Length": str(len(body))}
        else:
            body, status, hdr = self.payload, 200, {"Content-Length": str(n)}
        if self.cut is not None:
            body, self.cut = body[: self.cut], None
        return FakeResponse(status, body, hdr)


def test_fresh_download_skip_and_resume(tmp_path):
    srv = FakeServer()
    dest = tmp_path / "a.tif"
    assert download_file("u", dest, len(PAYLOAD), session=srv) == len(PAYLOAD)
    assert dest.read_bytes() == PAYLOAD and len(srv.requests) == 1

    download_file("u", dest, len(PAYLOAD), session=srv)      # complete file: no request at all
    assert len(srv.requests) == 1

    dest.write_bytes(PAYLOAD[:1000])                            # partial file: one Range request
    download_file("u", dest, len(PAYLOAD), session=srv)
    assert srv.requests[-1]["Range"] == "bytes=1000-" and dest.read_bytes() == PAYLOAD


def test_dropped_connection_resumes_and_ignored_range_restarts(tmp_path):
    srv = FakeServer(cut_first=1500)                            # first response ends early
    dest = tmp_path / "b.tif"
    log = []
    download_file("u", dest, len(PAYLOAD), session=srv, retries=3, retry_delay=0, log=log.append)
    assert dest.read_bytes() == PAYLOAD
    assert [h.get("Range") for h in srv.requests] == [None, "bytes=1500-"]
    assert any("incomplete" in m for m in log)

    srv = FakeServer(honour_range=False)                        # server answers 200 to a Range request
    dest.write_bytes(PAYLOAD[:2000])
    download_file("u", dest, len(PAYLOAD), session=srv, retry_delay=0)
    assert dest.read_bytes() == PAYLOAD                         # rewritten from the start, not appended

    dest.write_bytes(PAYLOAD + b"xx")                           # local file larger than expected
    download_file("u", dest, len(PAYLOAD), session=srv, retry_delay=0)
    assert dest.read_bytes() == PAYLOAD


def test_size_mismatch_and_4xx_raise(tmp_path):
    srv = FakeServer()
    with pytest.raises(DownloadError, match="incomplete"):
        download_file("u", tmp_path / "c.tif", len(PAYLOAD) + 5, session=srv, retries=1, retry_delay=0)
    assert len(srv.requests) == 2                               # one attempt plus one retry

    srv = FakeServer(status=404)
    with pytest.raises(DownloadError, match="HTTP 404"):
        download_file("u", tmp_path / "d.tif", 10, session=srv, retries=5, retry_delay=0)
    assert len(srv.requests) == 1                               # 4xx is not retried

    srv = FakeServer(status=503)
    with pytest.raises(DownloadError):
        download_file("u", tmp_path / "e.tif", 10, session=srv, retries=2, retry_delay=0)
    assert len(srv.requests) == 3                               # 5xx is retried


def test_download_manifest_filters_tiles_and_reports(tmp_path):
    rows = [ManifestRow("u1", len(PAYLOAD), "THEMIS_TI_Mosaic_Quant_00N240E_100mpp.tif"),
            ManifestRow("u2", len(PAYLOAD) + 1, "THEMIS_TI_Mosaic_Quant_30S060E_100mpp.tif")]
    srv = FakeServer()
    log = []
    report = download_manifest(rows, tmp_path / "tiles", tiles=["00N240E"], session=srv, log=log.append)
    assert report.ok == [rows[0].name] and report.failed == []
    assert (tmp_path / "tiles" / rows[0].name).read_bytes() == PAYLOAD
    assert log[0].startswith("1 files")

    report = download_manifest(rows, tmp_path / "tiles", session=srv, retries=0, retry_delay=0, log=log.append)
    assert report.ok == [rows[0].name] and report.failed == [rows[1].name]   # wrong size in the manifest
