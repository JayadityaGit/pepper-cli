"""Allow ``python -m pepper_cli`` to invoke the CLI.

The ``pepper`` entry point declared in ``pyproject.toml`` is the
recommended invocation; this module exists so curl-and-pipe install
recipes (``python -m pepper_cli login`` before the script is wired up
on PATH) still work.
"""

from pepper_cli.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
