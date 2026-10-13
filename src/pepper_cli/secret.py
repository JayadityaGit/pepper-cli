"""Tiny ``SecretStr`` shim — same surface as ``pydantic.SecretStr`` but
zero extra dependencies.

The repo-wide rule in ``CLAUDE.md`` is "declare secrets with
``SecretStr`` and read with ``.get_secret_value()`` only at the use
site." Server-side that's ``pydantic.SecretStr`` because pydantic is
already a server dep. The CLI package is deliberately dep-light (see
``clients/pepper-cli/pyproject.toml`` — only ``httpx`` + ``rich``), so
pulling in pydantic for a single type would bloat the wheel and the
iOS/Android-port reference. This shim provides the same behavior in
~20 lines.

What ``SecretStr`` buys us here: the ``Credentials`` dataclass's
default ``repr`` would print ``Credentials(server_url='...',
token='pep_pat_...')`` if anyone ever logged or printed it. Wrapping
the token makes that ``token=SecretStr('**********')`` and forces a
deliberate ``.get_secret_value()`` call to unwrap — easy to grep for
when auditing leak paths.

If a future port to a richer client (Textual, mobile bridge) ends up
needing pydantic anyway, dropping this shim for ``pydantic.SecretStr``
is a one-line import swap — the method names match.
"""

from __future__ import annotations


class SecretStr:
    """Wrap a string so its plaintext doesn't leak via ``repr`` / ``str``.

    Only ``.get_secret_value()`` returns the plaintext. ``repr`` and
    ``str`` produce a fixed redaction marker so the value can't sneak
    into logging frameworks that ``str``-format their args or into
    tracebacks that include locals.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"SecretStr expects str, got {type(value).__name__}")
        self._value = value

    def get_secret_value(self) -> str:
        """Return the wrapped plaintext. The grep-friendly unwrap point
        — searching the codebase for ``.get_secret_value()`` lists
        every site that touches a secret."""
        return self._value

    def __repr__(self) -> str:
        # Matches ``pydantic.SecretStr``'s redaction marker so the
        # shape is familiar to anyone coming from the server side.
        return "SecretStr('**********')" if self._value else "SecretStr('')"

    def __str__(self) -> str:
        return "**********" if self._value else ""

    def __eq__(self, other: object) -> bool:
        # Equality only against other ``SecretStr`` — comparing to a
        # raw str is almost always a test mistake (the assertion
        # silently fails). Returning ``NotImplemented`` for str makes
        # the mismatch loud.
        if not isinstance(other, SecretStr):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)
