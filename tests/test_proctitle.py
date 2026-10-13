"""Tests for :mod:`pepper_cli.proctitle`."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

from pepper_cli import proctitle


def test_calls_setproctitle_with_the_given_title(monkeypatch) -> None:
    calls: list[str] = []
    fake_module = types.SimpleNamespace(setproctitle=calls.append)
    monkeypatch.setitem(sys.modules, "setproctitle", fake_module)

    proctitle.set_process_title("pepper")

    assert calls == ["pepper"]


def test_missing_setproctitle_is_swallowed(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "setproctitle", None)  # import raises ImportError

    proctitle.set_process_title("pepper")  # must not raise


def test_failing_setproctitle_is_swallowed(monkeypatch) -> None:
    def _boom(_title: str) -> None:
        raise RuntimeError("no native support on this platform")

    fake_module = types.SimpleNamespace(setproctitle=_boom)
    monkeypatch.setitem(sys.modules, "setproctitle", fake_module)

    proctitle.set_process_title("pepper")  # must not raise


# The child reports what the OS now calls it. ``/proc/self/comm`` is what
# ``ps -o comm`` / ``top`` read and ``/proc/self/cmdline`` is what ``ps -o
# args`` and tab-naming tools read — both Linux-only; elsewhere
# ``getproctitle()`` is the portable readback.
_RENAME_AND_REPORT = """
import json, pathlib, sys
import setproctitle
from pepper_cli.proctitle import set_process_title

set_process_title("pepper-test")
report = {"title": setproctitle.getproctitle()}
if sys.platform == "linux":
    proc = pathlib.Path("/proc/self")
    report["comm"] = (proc / "comm").read_text().strip()
    report["cmdline"] = (proc / "cmdline").read_bytes().split(b"\\0")[0].decode()
print(json.dumps(report))
"""


def test_really_renames_the_process() -> None:
    """End to end against the installed extension, in a child process so the
    test runner's own title is untouched: the fakes above can't catch an
    import that succeeds but a rename that doesn't happen."""
    pytest.importorskip("setproctitle")

    result = subprocess.run(
        [sys.executable, "-c", _RENAME_AND_REPORT],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    report = json.loads(result.stdout)

    assert report["title"] == "pepper-test"
    if sys.platform == "linux":
        assert report["comm"] == "pepper-test"
        assert report["cmdline"] == "pepper-test"
