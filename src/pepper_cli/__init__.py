"""Pepper command-line client.

Public surface is :mod:`pepper_cli.cli` (entry point) and
:mod:`pepper_cli.api_client` (the de-facto API contract that future
iOS / Android ports use as a reference). The ``credentials`` and
``render`` modules are implementation details.
"""

from pepper_cli._version import get_version

__version__ = get_version()
