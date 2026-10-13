"""Read-only filesystem tools under one or more declared roots.

Three tools, named exactly as ``planning/client-tools.md`` §9 names them:

- ``fs_list`` — the entries directly under a relative directory;
- ``fs_read`` — one file's text, size-capped with a truncation marker;
- ``fs_grep`` — a literal, case-insensitive search **run here**, returning
  bounded snippets rather than whole files (D6(a): the search happens on the
  device, so what crosses the wire is strictly less than the file).

Paths are relative to a declared root and are re-resolved with the OS's own
``realpath`` before anything is opened, which is what makes the escape matrix
— ``..``, a symlink pointing out, an absolute path, a root-prefix confusion
like ``/notes`` versus ``/notes-private`` — one check rather than four
string rules. The containment test is on *resolved* path components, never on
string prefixes: ``"/notes-private".startswith("/notes")`` is ``True`` and is
precisely the bug that test matrix exists to catch.

Everything refuses loudly. A refusal is an ``is_error`` result the model
reads as a sentence, not a silent empty answer — a tool that returns nothing
when it meant "you may not look there" teaches the model the folder is empty.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Bytes of one file ``fs_read`` will return. Past it the content is cut and
#: a marker says so, because a silently truncated file is one the model will
#: reason about as though it had all of it.
MAX_READ_BYTES = 200_000

#: Entries one ``fs_list`` returns. A directory with 40k files is a listing
#: nobody reads and a prompt nobody can afford.
MAX_LIST_ENTRIES = 500

#: Matches one ``fs_grep`` returns, and the characters of context around
#: each. Both are the "bounded snippets" half of D6(a).
MAX_GREP_MATCHES = 40
GREP_SNIPPET_CHARS = 240

#: Files ``fs_grep`` will open, per search. A search that walks a
#: node_modules tree is a search that hangs the turn.
MAX_GREP_FILES = 2_000

#: Directory and file names skipped by ``fs_list``'s walk and ``fs_grep``'s.
#: Not a security control — containment is — just the difference between a
#: useful answer and a wall of build artifacts.
IGNORED_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".DS_Store",
    }
)

#: Suffixes treated as binary without reading them. The NUL-byte sniff below
#: is the real test; this is the cheap one that avoids opening a 2 GB video
#: to discover it is a video.
BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
        ".pdf", ".zip", ".gz", ".bz2", ".xz", ".tar", ".7z", ".rar",
        ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac",
        ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".pyc", ".pyo", ".class", ".jar", ".wasm",
        ".sqlite", ".sqlite3", ".db",
    }
)  # fmt: skip

#: Bytes sniffed for a NUL before deciding a file is binary.
_SNIFF_BYTES = 8_000


def _read_capped(path: Path, *, marker: bool = True) -> str:
    """Read at most :data:`MAX_READ_BYTES` from ``path``.

    Reads from the open file rather than slicing ``read_bytes()``, so the cap
    bounds *memory and I/O* and not merely the returned string. The
    difference is not cosmetic: a valid multi-gigabyte text file inside a
    shared folder would otherwise be loaded in full before being trimmed —
    and ``fs_grep`` would do it again for every one of up to
    :data:`MAX_GREP_FILES` candidates.

    One byte past the cap is read deliberately: it is what distinguishes "the
    file ended here" from "this is where we stopped", and only the second
    earns the truncation marker.
    """
    with path.open("rb") as fh:
        raw = fh.read(MAX_READ_BYTES + 1)
    truncated = len(raw) > MAX_READ_BYTES
    text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
    if truncated and marker:
        text += f"\n…[truncated at {MAX_READ_BYTES} bytes]"
    return text


class PathRefused(Exception):
    """An argument did not resolve to something inside a declared root.

    Carries a model-facing sentence. It deliberately does **not** name the
    resolved path or the root: a refusal that echoes back where it looked
    turns ``fs_read`` into a filesystem oracle for anyone who can make the
    model call it, which on a prompt-injected turn is whoever wrote the file
    it just read.
    """


@dataclass(frozen=True)
class _ResolvedRoot:
    """One ``--root`` the user declared, as an absolute real path."""

    label: str
    path: Path


def _is_within(candidate: Path, root: Path) -> bool:
    """Containment by path components, never by string prefix.

    ``Path.is_relative_to`` compares parts, so ``/notes-private`` is not
    inside ``/notes`` — which a ``startswith`` check would get wrong, and
    which is its own row in the escape matrix.

    Both sides are already ``realpath``-resolved by the caller, so a symlink
    inside the root pointing out of it fails here rather than being followed.
    On macOS the comparison is still exact-case: the filesystem is usually
    case-insensitive but ``realpath`` returns the *stored* spelling, so
    ``/Users/x/Notes`` resolving under a root declared as ``/Users/x/notes``
    is caught by the resolve, not by a case-folded compare we would then have
    to get right on three platforms.
    """
    return candidate == root or candidate.is_relative_to(root)


class FilesystemProvider:
    """The three tools, bound to the roots one CLI invocation declared."""

    def __init__(self, roots: Sequence[str | os.PathLike[str]]) -> None:
        resolved: list[_ResolvedRoot] = []
        seen: set[Path] = set()
        for raw in roots:
            path = Path(raw).expanduser()
            if not path.is_dir():
                raise PathRefused(f"{raw} is not a directory")
            real = path.resolve(strict=True)
            if real in seen:
                continue
            seen.add(real)
            resolved.append(_ResolvedRoot(label=real.name or str(real), path=real))
        if not resolved:
            raise PathRefused("no readable roots")
        self._roots = tuple(resolved)

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(r.path for r in self._roots)

    # -- manifest ------------------------------------------------------

    def manifest(self) -> list[dict[str, Any]]:
        """The ``tools/list``-shaped manifest to declare on a chat request.

        ``readOnlyHint`` is ``True`` on all three and is the truth: nothing
        in this module opens a file for writing. The server would drop a
        tool without it anyway (phase 1 declares no writes), which is the
        conservative default working in the right direction.

        The root *names* appear in the descriptions so the model can say
        "your notes folder" — a label, never a path, and never something the
        server may treat as a guarantee (invariant 2).
        """
        where = ", ".join(r.label for r in self._roots)
        return [
            {
                "name": "fs_list",
                "description": (
                    f"List the files and folders inside {where} on this machine. "
                    "`path` is relative to the folder root; omit it for the top level."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative directory, e.g. 'projects/2026'. Optional.",
                        }
                    },
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "fs_read",
                "description": (
                    f"Read one text file from {where} on this machine. "
                    "`path` is relative to the folder root."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Relative file path, e.g. 'projects/2026/plan.md'.",
                        }
                    },
                    "required": ["path"],
                },
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "fs_grep",
                "description": (
                    f"Search {where} on this machine for a literal phrase, "
                    "case-insensitively, and return the matching lines with a little "
                    "context. The search runs on the machine — only the matches travel."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Literal text to look for."},
                        "path": {
                            "type": "string",
                            "description": "Relative directory to search under. Optional.",
                        },
                    },
                    "required": ["query"],
                },
                "annotations": {"readOnlyHint": True},
            },
        ]

    # -- dispatch ------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run one tool. Returns ``(text, is_error)``.

        Never raises for an ordinary refusal or an OS error: both are
        answers, and an exception here would leave the server's call to time
        out twenty seconds later with no reason attached.
        """
        try:
            if name == "fs_list":
                return self._list(str(arguments.get("path") or "")), False
            if name == "fs_read":
                return self._read(str(arguments.get("path") or "")), False
            if name == "fs_grep":
                return (
                    self._grep(
                        str(arguments.get("query") or ""),
                        str(arguments.get("path") or ""),
                    ),
                    False,
                )
        except PathRefused as exc:
            return str(exc), True
        except OSError as exc:
            # Errno text, not the path: the same oracle argument as
            # ``PathRefused``.
            return f"Couldn't read that: {exc.strerror or 'I/O error'}.", True
        return f"No such tool: {name}.", True

    # -- resolution ----------------------------------------------------

    def _resolve(self, relative: str) -> Path:
        """Resolve ``relative`` inside exactly one root, or refuse.

        The order is the point. An absolute path is refused *before*
        resolution rather than joined (``Path("/a") / "/etc/passwd"`` is
        ``/etc/passwd`` — the join silently discards the root), and the
        containment check runs on the ``realpath`` so a symlink is followed
        first and judged second.
        """
        candidate = Path(relative)
        if candidate.is_absolute() or (os.name == "nt" and candidate.drive):
            raise PathRefused(
                "Paths must be relative to the shared folder — absolute paths aren't allowed."
            )
        for root, rest in self._candidates(candidate):
            try:
                real = (root.path / rest).resolve(strict=True)
            except (OSError, RuntimeError):
                # Missing, or a symlink loop. Try the next root; if none
                # matches, the refusal below is deliberately the same one an
                # out-of-root path gets, so "does this file exist" cannot be
                # answered for anything outside the grant.
                continue
            if _is_within(real, root.path):
                return real
        raise PathRefused("That path isn't inside the folder you shared, or doesn't exist.")

    def _candidates(self, relative: Path) -> Iterator[tuple[_ResolvedRoot, Path]]:
        """``(root, path-within-root)`` pairs to try, in order.

        With one root there is one answer. With several, results are labelled
        ``<root-name>/<rest>`` (see :meth:`_label_for`), so a path the model
        read off a previous result starts with a root's own name — try that
        reading first, then fall back to treating the whole thing as relative
        to each root in turn. Without the first reading, ``notes/plan.md``
        would resolve as ``<notes-root>/notes/plan.md`` and a file the model
        had just been shown would come back "not found".
        """
        parts = relative.parts
        if len(self._roots) > 1 and parts:
            for root in self._roots:
                if parts[0] == root.label:
                    yield root, Path(*parts[1:]) if len(parts) > 1 else Path()
        for root in self._roots:
            yield root, relative

    @staticmethod
    def _require_regular(path: Path) -> None:
        """Refuse anything that is not a plain file.

        A fifo blocks forever on open, a device file is unbounded, and a
        socket is neither. ``is_file`` follows symlinks, which is correct
        here because the link was already resolved and contained.
        """
        if not path.is_file():
            raise PathRefused("That isn't a readable file.")

    @staticmethod
    def _looks_binary(path: Path) -> bool:
        if path.suffix.lower() in BINARY_SUFFIXES:
            return True
        with path.open("rb") as fh:
            return b"\x00" in fh.read(_SNIFF_BYTES)

    def _contained(self, path: Path) -> Path | None:
        """``path`` re-resolved, if it is still inside a granted root.

        The containment check every *opening* path goes through, not only the
        one a caller named. ``_resolve`` covers an argument; this covers a
        candidate the provider found for itself — which is how ``fs_grep``
        reaches a file, and a walked candidate can be a symlink whose target
        is outside the grant even though the link itself sits inside it
        (``os.walk(followlinks=False)`` stops a symlinked *directory* being
        descended and says nothing about a symlinked file).
        """
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        return real if any(_is_within(real, r.path) for r in self._roots) else None

    def _label_for(self, path: Path) -> str:
        """``root-name/relative/path`` — how a result names a file.

        Rooted at the folder's own name rather than at an absolute path so a
        result never ships the user's home directory to the server.
        """
        for root in self._roots:
            if _is_within(path, root.path):
                rel = path.relative_to(root.path)
                return f"{root.label}/{rel}" if str(rel) != "." else root.label
        return path.name

    # -- tools ---------------------------------------------------------

    def _list(self, relative: str) -> str:
        if not relative and len(self._roots) > 1:
            # More than one ``--root``: the top level is the set of shared
            # folders, not the first one. Falling through to ``_roots[0]``
            # would make the second folder reachable only by a name the
            # model was never shown.
            names = "\n".join(f"{r.label}/" for r in self._roots)
            return f"Shared folders:\n{names}"
        target = self._resolve(relative) if relative else self._roots[0].path
        if not target.is_dir():
            raise PathRefused("That isn't a folder.")
        rows: list[str] = []
        truncated = False
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if entry.name in IGNORED_NAMES:
                continue
            if len(rows) >= MAX_LIST_ENTRIES:
                truncated = True
                break
            # A listing names a symlink's target as if it were a file here,
            # and ``stat()`` follows the link — so an outward link would
            # publish an external file's existence and size. Same containment
            # rule as everything else, applied before the name is printed.
            real = self._contained(entry)
            if real is None:
                continue
            if real.is_dir():
                rows.append(f"{entry.name}/")
            elif real.is_file():
                rows.append(f"{entry.name} ({real.stat().st_size} bytes)")
            # Anything else (fifo, socket, device) is simply not listed:
            # naming it invites the model to read it, and it cannot.
        if not rows:
            return f"{self._label_for(target)} is empty."
        body = "\n".join(rows)
        if truncated:
            body += f"\n…[truncated: more than {MAX_LIST_ENTRIES} entries; narrow the path]"
        return f"{self._label_for(target)}:\n{body}"

    def _read(self, relative: str) -> str:
        if not relative:
            raise PathRefused("Which file? `path` is required.")
        target = self._resolve(relative)
        self._require_regular(target)
        if self._looks_binary(target):
            raise PathRefused("That file is binary — there's no text to read.")
        text = _read_capped(target)
        return f"{self._label_for(target)}:\n{text}"

    def _walk(self, start: Path, *, limit: int = MAX_GREP_FILES) -> Iterator[Path]:
        """Yield candidate files under ``start``, ignoring the usual noise.

        ``followlinks`` stays off: a symlinked directory would otherwise be
        walked, and a cycle would make the search unbounded. That says
        nothing about a symlinked *file*, which this happily yields — the
        caller is responsible for putting every candidate through
        :meth:`_contained` before opening it.
        """
        seen = 0
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_NAMES)
            for filename in sorted(filenames):
                if filename in IGNORED_NAMES:
                    continue
                if seen >= limit:
                    return
                seen += 1
                yield Path(dirpath) / filename

    def _walk_all(self, starts: Sequence[Path]) -> Iterator[Path]:
        """:meth:`_walk` over several trees, sharing one file budget."""
        budget = MAX_GREP_FILES
        for start in starts:
            for path in self._walk(start, limit=budget):
                budget -= 1
                yield path
            if budget <= 0:
                return

    def _grep(self, query: str, relative: str) -> str:
        needle = query.strip()
        if not needle:
            raise PathRefused("What should I search for? `query` is required.")
        if relative:
            start = self._resolve(relative)
            if not start.is_dir():
                raise PathRefused("That isn't a folder.")
            starts: tuple[Path, ...] = (start,)
        else:
            # No path given means "everywhere I can see", which with several
            # ``--root``s is all of them — searching only the first would
            # answer "no matches" about a folder it never opened.
            starts = self.roots
        lowered = needle.lower()
        hits: list[str] = []
        for candidate in self._walk_all(starts):
            if len(hits) >= MAX_GREP_MATCHES:
                break
            # Containment, before anything is opened. A walked candidate is a
            # path this provider chose, not one the caller named, so it has
            # had no realpath check yet — and a symlink inside a shared root
            # pointing at a readable file outside it is exactly how a search
            # would otherwise return content the user never granted.
            real = self._contained(candidate)
            if real is None:
                continue
            try:
                if not real.is_file() or self._looks_binary(real):
                    continue
                content = _read_capped(real, marker=False)
            except OSError:
                # One unreadable file does not fail a search over a folder.
                continue
            label = self._label_for(real)
            for lineno, line in enumerate(content.splitlines(), start=1):
                if lowered not in line.lower():
                    continue
                snippet = line.strip()[:GREP_SNIPPET_CHARS]
                hits.append(f"{label}:{lineno}: {snippet}")
                if len(hits) >= MAX_GREP_MATCHES:
                    break
        if not hits:
            return f"No matches for {needle!r}."
        body = "\n".join(hits)
        if len(hits) >= MAX_GREP_MATCHES:
            body += f"\n…[stopped at {MAX_GREP_MATCHES} matches; narrow the search]"
        return body


__all__ = [
    "BINARY_SUFFIXES",
    "GREP_SNIPPET_CHARS",
    "IGNORED_NAMES",
    "MAX_GREP_FILES",
    "MAX_GREP_MATCHES",
    "MAX_LIST_ENTRIES",
    "MAX_READ_BYTES",
    "FilesystemProvider",
    "PathRefused",
]
