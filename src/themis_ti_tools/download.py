"""Resumable HTTP download with byte-size verification.

Interrupted transfers continue with a ``Range`` request instead of restarting;
files that already have the expected size are skipped; files larger than
expected are discarded and fetched again.  A read timeout aborts stalled
connections so that the retry loop can resume them.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

from ._http import USER_AGENT, make_session  # noqa: F401  (USER_AGENT re-exported)
from .manifest import ManifestRow, resolve_data_dir, tile_from_name

CHUNK_SIZE = 1 << 20
TIMEOUT = (30.0, 120.0)  # connect, read (seconds)


class DownloadError(RuntimeError):
    """The file could not be fetched or does not have the expected size."""


class DownloadReport(NamedTuple):
    ok: list
    failed: list


def _total_from_headers(r, start: int) -> int | None:
    if r.status_code == 206:
        rng = r.headers.get("Content-Range", "")
        total = rng.rsplit("/", 1)[-1] if "/" in rng else ""
        if total.isdigit():
            return int(total)
        length = r.headers.get("Content-Length")
        return start + int(length) if length and length.isdigit() else None
    length = r.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def _fetch(session, url: str, dest: Path, chunk_size: int, timeout, say) -> tuple[int, int | None]:
    """One transfer attempt, resuming from the current file size.  Returns
    (bytes on disk afterwards, total size announced by the server)."""
    start = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={start}-"} if start > 0 else {}
    with session.get(url, headers=headers, stream=True, timeout=timeout) as r:
        if r.status_code == 416:  # range not satisfiable: already complete, or a wrong local file
            total = r.headers.get("Content-Range", "").rsplit("/", 1)[-1]
            if total.isdigit() and int(total) == start:
                return start, start
            say(f"{dest.name}: server rejected the resume offset, restarting")
            dest.unlink()
            return 0, None
        r.raise_for_status()
        total = _total_from_headers(r, start)
        if start > 0 and r.status_code == 200:
            say(f"{dest.name}: server ignored the range request, restarting")
        mode = "ab" if (start > 0 and r.status_code == 206) else "wb"
        with open(dest, mode) as fh:
            for chunk in r.iter_content(chunk_size):
                if chunk:
                    fh.write(chunk)
    return dest.stat().st_size, total


def download_file(url: str, dest: str | os.PathLike, expected_size: int | None = None, *,
                  session=None, chunk_size: int = CHUNK_SIZE, retries: int = 8,
                  retry_delay: float = 5.0, timeout=TIMEOUT,
                  log: Callable[[str], None] | None = None) -> int:
    """Download ``url`` to ``dest`` (resumable) and return the final size.

    ``expected_size`` (bytes) drives the skip, resume and discard logic and
    the final verification; without it the size announced by the server is
    used.  Raises :class:`DownloadError` after ``retries`` failed attempts
    or at once on a 4xx response.
    """
    import requests

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    say = log or (lambda msg: None)
    s = make_session(session)

    if dest.exists() and expected_size is not None:
        have = dest.stat().st_size
        if have == expected_size:
            say(f"{dest.name}: already complete, skipping")
            return have
        if have > expected_size:
            say(f"{dest.name}: local file larger than expected, restarting")
            dest.unlink()

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            say(f"{dest.name}: retry {attempt}/{retries} after: {last_error}")
            time.sleep(retry_delay)
        try:
            size, total = _fetch(s, url, dest, chunk_size, timeout, say)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                raise DownloadError(f"{url}: HTTP {status}") from exc
            last_error = exc
            continue
        except (requests.RequestException, OSError) as exc:
            last_error = exc
            continue
        want = expected_size if expected_size is not None else total
        if want is None or size == want:
            return size
        if size > want:
            say(f"{dest.name}: {size} bytes on disk, expected {want}; discarding")
            dest.unlink()
            last_error = DownloadError(f"{dest.name}: got {size} bytes, expected {want}")
        else:
            last_error = DownloadError(f"{dest.name}: incomplete, {size} of {want} bytes")
    raise DownloadError(str(last_error))


def download_manifest(rows: Iterable[ManifestRow], dest_dir: str | os.PathLike | None = None, *,
                      tiles: Iterable[str] | None = None, session=None,
                      log: Callable[[str], None] | None = None, **kwargs) -> DownloadReport:
    """Fetch every manifest row into ``dest_dir`` (default: the data
    directory, see :func:`resolve_data_dir`), skipping complete files.
    ``tiles`` restricts the rows to those tile ids.  Extra keyword arguments
    go to :func:`download_file`."""
    dest_dir = resolve_data_dir(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    say = log or (lambda msg: None)
    rows = list(rows)
    if tiles is not None:
        wanted = set(tiles)
        rows = [r for r in rows if tile_from_name(r.name) in wanted]
    s = make_session(session)
    total = sum(r.size for r in rows)
    say(f"{len(rows)} files, {total / 2**30:.2f} GiB -> {dest_dir}")
    ok, failed = [], []
    for i, r in enumerate(rows, 1):
        tag = f"[{i}/{len(rows)}] {r.name}"
        try:
            download_file(r.url, dest_dir / r.name, r.size, session=s,
                          log=lambda m, t=tag: say(f"{t}: {m.split(': ', 1)[-1]}"), **kwargs)
            ok.append(r.name)
            say(f"{tag}: OK")
        except DownloadError as exc:
            failed.append(r.name)
            say(f"{tag}: FAILED ({exc})")
    say(f"done: {len(ok)}/{len(rows)} verified" + (f", {len(failed)} failed" if failed else ""))
    return DownloadReport(ok, failed)
