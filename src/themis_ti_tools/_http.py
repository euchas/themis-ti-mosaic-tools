"""HTTP session with a descriptive User-Agent, shared by the listing, download
and HEAD helpers."""
from __future__ import annotations

from ._version import __version__

USER_AGENT = (
    f"themis-ti-mosaic-tools/{__version__} "
    "(+https://github.com/euchas/themis-ti-mosaic-tools)"
)


def make_session(session=None):
    """Return ``session`` unchanged, or a new ``requests.Session`` that
    identifies itself with :data:`USER_AGENT`."""
    if session is not None:
        return session
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s
