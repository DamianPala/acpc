"""Discovery and parsing for skills bundled with acpc."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Skill:
    """One readable bundled skill and the content it serves."""

    name: str
    description: str | None
    path: Path
    body: str


class SkillNotFoundError(LookupError):
    """Raised when a bundled skill name is not present."""


_FIELD_RE = re.compile(r"^([A-Za-z0-9_-]+):(?:[ \t]*(.*))?$")
_BLOCK_INDICATORS = {">", ">-", ">+", "|", "|-", "|+"}


def _resource_path(resource: Any) -> Path:
    return Path(str(resource))


def _line_text(line: str) -> str:
    return line.rstrip("\r\n")


def _parse_scalar(value: str) -> str | None:
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1] if value.endswith('"') else value[1:]
        return parsed if isinstance(parsed, str) else str(parsed)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _block_value(lines: list[str], indicator: str) -> str:
    nonblank_indents = [
        len(line) - len(line.lstrip(" \t")) for line in lines if _line_text(line).strip()
    ]
    indent = min(nonblank_indents, default=0)
    content = [_line_text(line)[indent:] if _line_text(line).strip() else "" for line in lines]

    if indicator.startswith(">"):
        value = ""
        for line in content:
            if not line:
                value += "\n"
            else:
                if value and not value.endswith("\n"):
                    value += " "
                value += line
    else:
        value = "\n".join(content)
        if lines and lines[-1].endswith(("\n", "\r")):
            value += "\n"

    if indicator.endswith("-"):
        return value.rstrip("\n")
    if value and not value.endswith("\n"):
        value += "\n"
    return value


def _parse_frontmatter(lines: list[str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    index = 0
    while index < len(lines):
        current = _line_text(lines[index])
        if not current.strip() or current[0].isspace():
            index += 1
            continue
        match = _FIELD_RE.match(current)
        if match is None:
            index += 1
            continue
        key, raw_value = match.groups()
        value = (raw_value or "").strip()
        if value in _BLOCK_INDICATORS:
            block_lines: list[str] = []
            next_index = index + 1
            while next_index < len(lines):
                candidate = _line_text(lines[next_index])
                if not candidate.strip() or candidate[0].isspace():
                    block_lines.append(lines[next_index])
                    next_index += 1
                    continue
                break
            if key == "description":
                values[key] = _block_value(block_lines, value)
            index = next_index
            continue
        if key in {"name", "description"}:
            values[key] = _parse_scalar(value)
        index += 1
    return values


def _parse_document(text: str) -> tuple[str | None, str | None, str]:
    """Return frontmatter name, description, and the exact body after it."""
    lines = text.splitlines(keepends=True)
    if not lines or _line_text(lines[0]).strip() != "---":
        return None, None, text
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if _line_text(line).strip() == "---"
        ),
        None,
    )
    if closing is None:
        return None, None, text
    metadata = _parse_frontmatter(lines[1:closing])
    return metadata.get("name"), metadata.get("description"), "".join(lines[closing + 1 :])


def _load_skill(resource: Any) -> Skill | None:
    try:
        if not resource.is_dir():
            return None
        path = _resource_path(resource)
        skill_file = path / "SKILL.md"
        with skill_file.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
    except (OSError, UnicodeError):
        return None
    _, description, body = _parse_document(content)
    return Skill(name=resource.name, description=description, path=path, body=body)


def _bundled_resources() -> Iterator[Any]:
    root = files("acpc").joinpath("data", "skills")
    try:
        resources = sorted(root.iterdir(), key=lambda item: item.name)
    except (OSError, FileNotFoundError):
        return
    yield from resources


def list_skills() -> tuple[Skill, ...]:
    """Return every readable ``SKILL.md`` under the bundled skills directory."""
    return tuple(
        skill for resource in _bundled_resources() if (skill := _load_skill(resource)) is not None
    )


def get_skill(name: str) -> Skill:
    """Return one bundled skill, or raise ``SkillNotFoundError``."""
    for skill in list_skills():
        if skill.name == name:
            return skill
    raise SkillNotFoundError(name)
