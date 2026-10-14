"""The installed package version and the ``User-Agent`` every request sends."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

#: Distribution name on PyPI (the import package is ``pepper_cli``).
DISTRIBUTION = "vycari-pepper"

#: Reported when package metadata is missing, e.g. running from a bare source tree.
UNKNOWN_VERSION = "0+unknown"


def get_version() -> str:
    """Installed ``vycari-pepper`` version, or :data:`UNKNOWN_VERSION`."""
    try:
        return version(DISTRIBUTION)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


#: Sent as ``User-Agent`` on every HTTP request the CLI makes.
USER_AGENT = f"pepper-cli/{get_version()}"
