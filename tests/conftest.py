"""Shared fixtures for the pepper-cli tests."""

from __future__ import annotations

import pytest

from pepper_cli import credentials


@pytest.fixture(autouse=True)
def _reset_credentials_profile():
    """``cli.main`` selects a process-wide credentials profile; make sure one
    test's ``--profile`` never leaks into the next."""
    credentials.set_profile(None)
    yield
    credentials.set_profile(None)
