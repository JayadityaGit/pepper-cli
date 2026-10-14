"""Tests for the ``window_title`` context manager.

``_stdout_is_tty`` is monkeypatched directly rather than faking
``sys.stdout.isatty`` — pytest's own stdout capture isn't a TTY, and this
sidesteps fighting that.
"""

from __future__ import annotations

import pytest

from pepper_cli import window_title


def test_tty_emits_push_then_set_then_pop_in_order(monkeypatch, capsys) -> None:
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: True)

    with window_title.window_title("Pepper"):
        pass

    out = capsys.readouterr().out
    push_at = out.index("\x1b[22;0t")
    set_at = out.index("\x1b]0;Pepper\x07")
    pop_at = out.index("\x1b[23;0t")
    assert push_at < set_at < pop_at


def test_pop_happens_on_exception(monkeypatch, capsys) -> None:
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: True)

    with pytest.raises(RuntimeError), window_title.window_title("Pepper"):
        raise RuntimeError("boom")

    out = capsys.readouterr().out
    assert "\x1b[23;0t" in out


def test_pop_happens_on_keyboard_interrupt(monkeypatch, capsys) -> None:
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: True)

    with pytest.raises(KeyboardInterrupt), window_title.window_title("Pepper"):
        raise KeyboardInterrupt

    out = capsys.readouterr().out
    assert "\x1b[23;0t" in out


def test_non_tty_emits_nothing(monkeypatch, capsys) -> None:
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: False)

    with window_title.window_title("Pepper"):
        pass

    assert capsys.readouterr().out == ""


class _BrokenStdout:
    """A stdout whose writes fail the way a closed or hung-up terminal's do."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.writes = 0

    def write(self, _s: str) -> int:
        self.writes += 1
        raise self.exc

    def flush(self) -> None:
        raise self.exc


@pytest.mark.parametrize("exc", [OSError(5, "EIO"), ValueError("I/O operation on closed file")])
def test_failed_setup_write_does_not_stop_the_block_and_still_pops(monkeypatch, exc) -> None:
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: True)
    broken = _BrokenStdout(exc)
    monkeypatch.setattr(window_title.sys, "stdout", broken)

    ran = False
    with window_title.window_title("Pepper"):
        ran = True

    assert ran
    # The setup write was attempted, and cleanup still tried the pop.
    assert broken.writes == 2


@pytest.mark.parametrize("exc", [OSError(5, "EIO"), ValueError("I/O operation on closed file")])
def test_failed_cleanup_write_does_not_mask_the_original_exception(monkeypatch, exc) -> None:
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: True)
    broken = _BrokenStdout(exc)
    monkeypatch.setattr(window_title.sys, "stdout", broken)

    with pytest.raises(RuntimeError, match="boom"), window_title.window_title("Pepper"):
        raise RuntimeError("boom")


def test_failed_cleanup_write_after_clean_exit_is_swallowed(monkeypatch, capsys) -> None:
    """Stdout closes while the REPL runs (e.g. the terminal hangs up): the
    pop fails, and the ``with`` block still exits cleanly."""
    monkeypatch.setattr(window_title, "_stdout_is_tty", lambda: True)

    with window_title.window_title("Pepper"):
        monkeypatch.setattr(window_title.sys, "stdout", _BrokenStdout(ValueError("closed")))

    assert "\x1b]0;Pepper\x07" in capsys.readouterr().out
