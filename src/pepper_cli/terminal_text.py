"""Make server-provided text safe to put on a terminal.

Anything the server sends — an error ``detail``, a room or task name, a
message body, a streamed reply — is untrusted as far as the terminal is
concerned. An escape sequence in it could clear the screen, move the cursor,
retitle the window or plant a hyperlink; a bidirectional override could make
the text read differently from what it is. These helpers remove all of that
while leaving ordinary text, including any Unicode, exactly as it was.

Two strengths, chosen by the kind of field:

- :func:`sanitize_line` for one-line fields (names, titles, ids, labels,
  statuses): every control character, line breaks and tabs included, becomes
  a space, so a field can never break the line it is printed on.
- :func:`sanitize_text` for free text (message bodies, notes, descriptions,
  replies, error details): newlines and tabs are kept, every other control
  character is dropped.

Both are idempotent and return clean input unchanged. Neither escapes rich
markup; callers that interpolate text into a markup string still need
``rich.markup.escape`` (or print with ``markup=False``).

``--json`` output never goes through here: JSON encoding already escapes
control characters, and that output must stay what the server sent.
"""

from __future__ import annotations

import re

# Whole terminal escape sequences, removed as units so no parameter bytes
# (the ``[2J`` of a clear-screen) are left behind as litter. In order: OSC
# (``ESC ]`` or C1 0x9d, up to BEL / ST), CSI (``ESC [`` or C1 0x9b), then any
# other two-byte ``ESC x`` escape. A bare trailing ESC (a sequence split
# across two streamed fragments) matches the last branch and is removed too.
ESCAPE_SEQUENCE_RE = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
)
# Every remaining C0 control, DEL and C1 control except newline. Unicode line
# and paragraph separators are not terminal controls and are left alone.
CONTROL_CHAR_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
# The same, minus tab as well: what :func:`sanitize_text` drops.
_TEXT_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Invisible format characters that can reorder or hide what is displayed:
# zero-width characters, bidirectional marks, embeddings, overrides and
# isolates, and the byte-order mark.
INVISIBLE_FORMAT_RE = re.compile(r"[\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def strip_sequences(text: str) -> str:
    """``text`` without terminal escape sequences or invisible format characters.

    Control characters outside an escape sequence are left for the caller,
    which decides whether they become spaces or disappear.
    """
    return INVISIBLE_FORMAT_RE.sub("", ESCAPE_SEQUENCE_RE.sub("", text))


def sanitize_line(text: str) -> str:
    """``text`` made safe to print inside one line of terminal output.

    Removes escape sequences and invisible format characters, then turns
    every remaining control character — newline, carriage return and tab
    included — into a space. Nothing is trimmed, collapsed or truncated, so
    a clean id or timestamp comes back byte-for-byte and stays copy-pasteable.
    """
    text = strip_sequences(text)
    return CONTROL_CHAR_RE.sub(" ", text.replace("\r\n", " ").replace("\n", " "))


def sanitize_text(text: str) -> str:
    """Multi-line ``text`` made safe to print to a terminal.

    Removes escape sequences and invisible format characters, normalizes
    CRLF and a lone CR to LF (a bare carriage return would let
    a later line overwrite an earlier one), and drops every other control
    character. Newlines and tabs are kept.
    """
    text = strip_sequences(text)
    return _TEXT_CONTROL_CHAR_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
