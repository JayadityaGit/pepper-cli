"""SKILL.md compose/parse for the ``pepper skills`` editor round-trip.

``pepper skills edit`` presents a skill as the same SKILL.md document
format the server's skills use everywhere else (YAML frontmatter +
Markdown body), opens it in ``$EDITOR``, and parses the result back
into API fields. Validation proper is the server's job — this module
only handles the document shape, so a value the server would reject
still round-trips to a readable 422 issue list rather than dying in
the client.

Frontmatter keys: ``name``, ``description``, and the optional extras
``model`` / ``toolsets`` / ``max_tool_rounds`` (omitted when unset,
matching the server's canonical serialization).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml


@dataclass(frozen=True)
class SkillFields:
    """Parsed editable fields of a SKILL.md document."""

    name: str
    description: str
    body: str
    model: str | None = None
    toolsets: tuple[str, ...] = ()
    max_tool_rounds: int | None = None


def compose_skill_md(fields: SkillFields) -> str:
    """Render fields as SKILL.md text (frontmatter + body)."""
    meta: dict[str, object] = {"name": fields.name, "description": fields.description}
    if fields.model:
        meta["model"] = fields.model
    if fields.toolsets:
        meta["toolsets"] = list(fields.toolsets)
    if fields.max_tool_rounds is not None:
        meta["max_tool_rounds"] = fields.max_tool_rounds
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{frontmatter}\n---\n\n{fields.body.strip()}\n"


def parse_skill_md(text: str) -> tuple[SkillFields | None, str | None]:
    """Parse SKILL.md text back into fields.

    Returns ``(fields, None)`` on success or ``(None, error)`` with a
    human-readable message for shape problems (missing frontmatter,
    unparseable YAML, missing name/description). Value-level problems
    (bad tier, out-of-range rounds) pass through untouched — the server
    reports those with the same wording every other surface gets.
    """
    if not text.startswith("---"):
        return None, "missing YAML frontmatter (the document must start with ---)"
    remainder = text[3:].lstrip("\r\n")
    closing = remainder.find("\n---")
    if closing == -1:
        return None, "unterminated YAML frontmatter (no closing ---)"
    yaml_text = remainder[:closing]
    body = remainder[closing + len("\n---") :].lstrip("\r\n")
    try:
        meta = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        return None, f"unparseable frontmatter: {exc}"
    if not isinstance(meta, dict):
        return None, "frontmatter is not a mapping"
    name = meta.get("name")
    description = meta.get("description")
    if not isinstance(name, str) or not name.strip():
        return None, "frontmatter is missing 'name'"
    if not isinstance(description, str) or not description.strip():
        return None, "frontmatter is missing 'description'"
    raw_toolsets = meta.get("toolsets")
    if raw_toolsets is None:
        toolsets: tuple[str, ...] = ()
    elif isinstance(raw_toolsets, list):
        toolsets = tuple(str(t) for t in raw_toolsets)
    else:
        return None, "'toolsets' must be a YAML list (e.g. toolsets: [github])"
    raw_model = meta.get("model")
    model = str(raw_model).strip() if raw_model is not None else None
    raw_rounds = meta.get("max_tool_rounds")
    if raw_rounds is not None and (isinstance(raw_rounds, bool) or not isinstance(raw_rounds, int)):
        return None, "'max_tool_rounds' must be an integer"
    return (
        SkillFields(
            name=name.strip(),
            description=description.strip(),
            body=body.strip(),
            model=model or None,
            toolsets=toolsets,
            max_tool_rounds=raw_rounds,
        ),
        None,
    )
