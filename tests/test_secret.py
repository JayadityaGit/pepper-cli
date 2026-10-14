"""Tests for the SecretStr shim — pins the protection behavior so a
future swap to ``pydantic.SecretStr`` (or any refactor) keeps the
guarantees the rest of the package relies on."""

from __future__ import annotations

import pytest

from pepper_cli.secret import SecretStr


def test_repr_redacts_plaintext() -> None:
    s = SecretStr("pep_pat_supersecret")
    assert "pep_pat_supersecret" not in repr(s)
    assert "**********" in repr(s)


def test_str_redacts_plaintext() -> None:
    s = SecretStr("pep_pat_supersecret")
    assert "pep_pat_supersecret" not in str(s)


def test_get_secret_value_returns_plaintext() -> None:
    s = SecretStr("pep_pat_abc")
    assert s.get_secret_value() == "pep_pat_abc"


def test_dataclass_repr_does_not_leak_via_default_repr() -> None:
    """The whole point: a frozen dataclass holding a SecretStr can
    ``repr()`` without spilling the secret. This is the leakage path
    that motivated adopting the wrapper in the first place."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Wrap:
        token: SecretStr

    out = repr(_Wrap(token=SecretStr("pep_pat_should_not_appear")))
    assert "pep_pat_should_not_appear" not in out
    assert "**********" in out


def test_eq_against_plain_str_is_not_implemented() -> None:
    """Equality against ``str`` returns ``NotImplemented`` so a
    careless ``assert creds.token == 'pep_pat_abc'`` fails loudly
    instead of silently."""
    assert (SecretStr("abc") == "abc") is False


def test_eq_secretstr_to_secretstr() -> None:
    assert SecretStr("abc") == SecretStr("abc")
    assert SecretStr("abc") != SecretStr("xyz")


def test_bool_reflects_emptiness() -> None:
    assert bool(SecretStr("x")) is True
    assert bool(SecretStr("")) is False


def test_rejects_non_str_input() -> None:
    with pytest.raises(TypeError):
        SecretStr(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SecretStr(123)  # type: ignore[arg-type]
