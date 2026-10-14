"""Check the wire dataclasses against the vendored ``/api/v1`` schema snapshot.

``contracts/api-v1.openapi.json`` is a copy of the server's published OpenAPI schema. Every
``# MIRROR: <server path>::<Model>`` comment under ``src/pepper_cli/`` names the server model a
dataclass follows. This test resolves each one to a schema in the snapshot and asserts that
every field of the annotated dataclass is a property of that schema (the server may have more
fields; the CLI tolerates them, but it must never read one the server does not send).

Every ``MIRROR:`` comment must be either checked or listed in an allowlist below, and every
allowlist entry must still be needed, so the allowlists cannot rot silently.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pepper_cli"
SNAPSHOT = ROOT / "contracts" / "api-v1.openapi.json"

# A MIRROR comment: ``# MIRROR: <server file>[::<Name>[ / <Name>...]]``. Matched anywhere in a
# comment line, so ``# Groups (MIRROR: src/pepper/api/groups.py)`` is seen (and must be
# allowlisted as prose) rather than slipping past.
_MIRROR_RE = re.compile(
    r"#.*?\bMIRROR:\s*(?P<path>[\w/.]+\.py)(?:::(?P<name>\w+)(?P<more>(?:\s*/\s*\w+)*))?"
)

# MIRROR comments that do not map a dataclass to a snapshot schema. Keyed by
# (file under src/pepper_cli/, the ``path[::Name]`` the comment names). Each must really need
# the exemption: it is an error if the comment would in fact be checked.
NOT_CHECKABLE: dict[tuple[str, str], str] = {
    ("api_client.py", "src/pepper/api/groups.py"): (
        "Prose: a section header and a method-block comment about the whole groups route "
        "module. No model is named, so there is nothing to resolve."
    ),
    ("api_client.py", "src/pepper/web/auth/device_router.py::device_poll"): (
        "Names a route function, not a model. The poll response is an untyped dict "
        "(pending/denied/approved/...) with no schema in the snapshot, and DevicePoll also "
        "folds in a client-synthesised 'slow_down' state from an HTTP 429."
    ),
    ("cli.py", "src/pepper/agent/artifacts/service.py::INLINE_TEXT"): (
        "Mirrors a string constant (the artifact kind), not a model, and annotates no dataclass."
    ),
}

# Dataclass fields that are not properties of the mirrored schema, and are not mismatches.
# Keyed by (dataclass name, field name). Each must be a real field that the schema really
# lacks: it is an error if the field is gone or the schema grew it.
# A genuine mismatch (a CLI field the server does not send) belongs here too, but its reason
# must start with "KNOWN MISMATCH". There are none today.
FIELD_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("RoomSkillDetail", "row"): (
        "Client-composed: the server returns the skill-row fields flattened beside 'skill'; "
        "the CLI groups those into a RoomSkill under 'row'. The flattened fields are checked "
        "through RoomSkill, which mirrors RoomSkillRow."
    ),
}


@dataclass(frozen=True)
class Mirror:
    file: str  # relative to src/pepper_cli/
    line: int
    path: str
    name: str | None
    extra_names: tuple[str, ...]
    cls: str | None  # the class the comment annotates, if any

    @property
    def target(self) -> str:
        return f"{self.path}::{self.name}" if self.name else self.path

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


def _load_schemas() -> dict[str, dict]:
    return json.loads(SNAPSHOT.read_text())["components"]["schemas"]


def resolve_schema(schemas: dict[str, dict], name: str) -> dict | None:
    """Find ``name`` in the snapshot.

    The server qualifies a schema name with its module path (``pkg__mod__Name``) when two
    models share a bare name; accept that form only when it is unambiguous.
    """
    if name in schemas:
        return schemas[name]
    qualified = [v for k, v in schemas.items() if k.endswith(f"__{name}")]
    return qualified[0] if len(qualified) == 1 else None


def _annotated_class(tree: ast.Module, lines: list[str], lineno: int) -> str | None:
    """The class defined right after the comment block containing line ``lineno``."""
    idx = lineno  # 0-based index of the line after the comment
    while idx < len(lines) and (not lines[idx].strip() or lines[idx].lstrip().startswith("#")):
        idx += 1
    first_code = idx + 1
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            if start == first_code:
                return node.name
    return None


def discover_mirrors() -> list[Mirror]:
    found = []
    for py in sorted(SRC.rglob("*.py")):
        text = py.read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        for i, line in enumerate(lines, 1):
            m = _MIRROR_RE.search(line)
            if not m:
                continue
            extra = tuple(re.findall(r"\w+", m.group("more") or ""))
            found.append(
                Mirror(
                    file=str(py.relative_to(SRC)),
                    line=i,
                    path=m.group("path"),
                    name=m.group("name"),
                    extra_names=extra,
                    cls=_annotated_class(tree, lines, i),
                )
            )
    return found


def _dataclass_fields(file: str, cls: str) -> list[str] | None:
    module = importlib.import_module("pepper_cli." + file.removesuffix(".py").replace("/", "."))
    obj = getattr(module, cls, None)
    if obj is None or not dataclasses.is_dataclass(obj):
        return None
    return [f.name for f in dataclasses.fields(obj)]


def _checks(mirror: Mirror) -> list[tuple[str, str, str]]:
    """The (dataclass, schema name, where) pairs this comment asks us to verify."""
    pairs = []
    if mirror.name and mirror.cls:
        pairs.append((mirror.cls, mirror.name))
    # ``Foo / Bar``: the comment also covers a sibling dataclass named after the extra schema.
    pairs.extend((extra, extra) for extra in mirror.extra_names)
    return [(cls, schema, mirror.where) for cls, schema in pairs]


def _is_checkable(mirror: Mirror, schemas: dict[str, dict]) -> bool:
    if not (mirror.name and mirror.cls):
        return False
    return (
        resolve_schema(schemas, mirror.name) is not None
        and _dataclass_fields(mirror.file, mirror.cls) is not None
    )


MIRRORS = discover_mirrors()


def test_mirrors_are_discovered():
    # Guard against the scan silently finding nothing (a moved directory, a changed marker).
    assert len(MIRRORS) >= 20


def test_snapshot_has_schemas():
    assert SNAPSHOT.is_file()
    assert _load_schemas()


def test_every_mirror_is_checked_or_allowlisted():
    schemas = _load_schemas()
    problems = []
    for m in MIRRORS:
        allowlisted = (m.file, m.target) in NOT_CHECKABLE
        checkable = _is_checkable(m, schemas)
        if not allowlisted and not checkable:
            problems.append(
                f"{m.where}: MIRROR {m.target!r} is neither checkable (needs ``::Model`` in the "
                "snapshot, directly above a dataclass) nor listed in NOT_CHECKABLE"
            )
        if allowlisted and checkable:
            problems.append(
                f"{m.where}: MIRROR {m.target!r} is in NOT_CHECKABLE but is now checkable; "
                "remove the allowlist entry"
            )
    assert not problems, "\n".join(problems)


def test_not_checkable_entries_are_all_used():
    seen = {(m.file, m.target) for m in MIRRORS}
    stale = sorted(set(NOT_CHECKABLE) - seen)
    assert not stale, f"NOT_CHECKABLE entries match no MIRROR comment: {stale}"


def test_dataclass_fields_exist_in_schema():
    schemas = _load_schemas()
    problems = []
    used_exceptions: set[tuple[str, str]] = set()
    for mirror in MIRRORS:
        if (mirror.file, mirror.target) in NOT_CHECKABLE:
            continue
        for cls, schema_name, where in _checks(mirror):
            schema = resolve_schema(schemas, schema_name)
            fields = _dataclass_fields(mirror.file, cls)
            if schema is None or fields is None:
                problems.append(f"{where}: cannot check {cls} against {schema_name}")
                continue
            props = set(schema.get("properties", {}))
            for fname in fields:
                if fname in props:
                    if (cls, fname) in FIELD_EXCEPTIONS:
                        problems.append(
                            f"FIELD_EXCEPTIONS[{cls}.{fname}] is stale: {schema_name} now has it"
                        )
                    continue
                if (cls, fname) in FIELD_EXCEPTIONS:
                    used_exceptions.add((cls, fname))
                else:
                    problems.append(
                        f"{where}: {cls}.{fname} is not a property of schema {schema_name}"
                    )
    stale = sorted(set(FIELD_EXCEPTIONS) - used_exceptions)
    problems.extend(f"FIELD_EXCEPTIONS[{c}.{f}] matches no mirrored field" for c, f in stale)
    assert not problems, "\n".join(problems)


def test_resolve_schema_handles_qualified_names():
    schemas = {"Plain": {"n": 1}, "pkg__mod__Dup": {"n": 2}, "a__Amb": {}, "b__Amb": {}}
    assert resolve_schema(schemas, "Plain") == {"n": 1}
    assert resolve_schema(schemas, "Dup") == {"n": 2}
    assert resolve_schema(schemas, "Amb") is None  # ambiguous: refuse to guess
    assert resolve_schema(schemas, "Missing") is None


@pytest.mark.parametrize("key", sorted(FIELD_EXCEPTIONS) + sorted(NOT_CHECKABLE))
def test_allowlist_reasons_are_written(key):
    reason = {**FIELD_EXCEPTIONS, **NOT_CHECKABLE}[key]
    assert len(reason) > 20
