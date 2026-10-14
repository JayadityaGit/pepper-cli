"""Best-effort process-title fix for the ``pepper`` console script.

``uv``/``pip`` installs ``pepper`` as a console-script whose shebang is
``python3``, so the process's ``argv[0]`` is the interpreter rather than
``pepper``. Terminal multiplexers and tab-naming tools (e.g. herdr's
automatic-rename plugin) name a tab after ``argv[0]``, so every Pepper tab
reads ``python3`` instead of ``pepper``. :func:`set_process_title` renames
the process via ``setproctitle`` to fix that.
"""

from __future__ import annotations

import contextlib


def set_process_title(title: str) -> None:
    """Rename this process to ``title``, best-effort.

    Swallows anything — a missing ``setproctitle`` (expected: it's only a
    dependency on the platforms it ships wheels for, see ``pyproject.toml``)
    or a native call that fails — because a cosmetic tab name must never
    stop the CLI from starting.
    """
    with contextlib.suppress(Exception):
        import setproctitle

        setproctitle.setproctitle(title)
