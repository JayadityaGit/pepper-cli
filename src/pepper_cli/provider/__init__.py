"""Tools this CLI runs on the user's own machine, for one turn.

``pepper chat --root ~/notes "…"``
declares three read-only filesystem tools on the chat request, serves the
``client_tool_call`` frames that come back down the stream, and answers them
with ``POST /api/v1/chat/tool-result``.

**Every filesystem rule lives here, not on the server**: the server
cannot verify a path on a disk it cannot see, and should not pretend to. It
sends a tool name and arguments; this module resolves them inside the roots
the user named on their own command line and refuses everything else. The
consequence the design rests on is that a fully compromised server — or a
prompt-injected model — can call only the tools this process publishes, with
arguments this process validates, which is exactly the authority the user
granted when they typed ``--root`` and no more.

Which is why **no tool here takes a root, base path or scope argument**
(the grant is whatever the user typed after ``--root``). The moment a root is a parameter, the grant moves to the
server and that whole property is void.
"""

from __future__ import annotations

from pepper_cli.provider.fs import (
    FilesystemProvider,
    PathRefused,
)

__all__ = ["FilesystemProvider", "PathRefused"]
