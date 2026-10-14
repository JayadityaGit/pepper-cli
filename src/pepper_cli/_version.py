"""The installed package version and the ``User-Agent`` every request sends."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

#: Distribution name on PyPI (the import package is ``pepper_cli``).
DISTRIBUTION = "vycari-pepper"

#: Reported when package metadata is missing, e.g. running from a bare source tree.
UNKNOWN_VERSION = "0+unknown"

# The characters a version string is made of. The version goes into an HTTP
# header, so anything else (whitespace, a line break, non-ASCII) is refused.
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}")


def get_version() -> str:
    """Installed ``vycari-pepper`` version, or :data:`UNKNOWN_VERSION`.

    Also :data:`UNKNOWN_VERSION` when the metadata holds something that is not
    shaped like a version, so the value is always safe in a header.
    """
    try:
        found = version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
    return found if _VERSION_RE.fullmatch(found) else UNKNOWN_VERSION


#: Sent as ``User-Agent`` on every HTTP request the CLI makes.
USER_AGENT = f"pepper-cli/{get_version()}"
