from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Slash commands and skills already work in a headless run — Claude Code expands
# `/name` in the prompt before executing, and AIOps never passes `--bare`, so
# skills, commands, CLAUDE.md and plugins are all auto-discovered. This module
# only *finds* them, so the composer can offer them instead of making you
# remember what exists.

FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Capability:
    name: str          # what you type, without the leading slash
    kind: str          # skill | command | builtin
    description: str
    source: str        # workspace | user | built-in


def _frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return {}
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, _, value = line.partition(":")
            data[key.strip().lower()] = value.strip().strip("\"'")
    return data


def _first_heading_or_line(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return ""
    body = FRONTMATTER.sub("", text)
    for line in body.splitlines():
        line = line.strip()
        if line:
            return line.lstrip("# ").strip()[:200]
    return ""


def _scan_skills(root: Path, source: str) -> list[Capability]:
    out: list[Capability] = []
    base = root / ".claude" / "skills" if source == "workspace" else root / "skills"
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir()):
        skill_file = entry / "SKILL.md"
        if not entry.is_dir() or not skill_file.is_file():
            continue
        meta = _frontmatter(skill_file)
        out.append(
            Capability(
                name=meta.get("name") or entry.name,
                kind="skill",
                description=meta.get("description") or _first_heading_or_line(skill_file),
                source=source,
            )
        )
    return out


def _scan_commands(root: Path, source: str) -> list[Capability]:
    out: list[Capability] = []
    base = root / ".claude" / "commands" if source == "workspace" else root / "commands"
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*.md")):
        # Nested directories namespace the command, mirroring Claude Code.
        rel = path.relative_to(base).with_suffix("")
        name = ":".join(rel.parts)
        meta = _frontmatter(path)
        out.append(
            Capability(
                name=name,
                kind="command",
                description=meta.get("description") or _first_heading_or_line(path),
                source=source,
            )
        )
    return out


def _scan_codex_prompts(home: Path) -> list[Capability]:
    base = home / ".codex" / "prompts"
    if not base.is_dir():
        return []
    return [
        Capability(
            name=path.stem,
            kind="command",
            description=_first_heading_or_line(path),
            source="user",
        )
        for path in sorted(base.glob("*.md"))
    ]


# Built-ins worth surfacing that are known to accept an argument in headless
# runs. Anything terminal-only (/login and friends) is deliberately absent.
CLAUDE_BUILTINS = [
    Capability(
        name="goal",
        kind="builtin",
        description="Set the objective for a long autonomous run, e.g. /goal ship the migration",
        source="built-in",
    ),
    Capability(
        name="model",
        kind="builtin",
        description="Switch model for this turn, e.g. /model opus",
        source="built-in",
    ),
    Capability(
        name="effort",
        kind="builtin",
        description="Set reasoning effort, e.g. /effort xhigh",
        source="built-in",
    ),
]


def discover(provider: str, workspace_path: str | None) -> list[Capability]:
    """Skills and commands available to a session, most specific first."""
    home = Path(os.path.expanduser("~"))
    found: list[Capability] = []

    if provider == "claude":
        if workspace_path and os.path.isdir(workspace_path):
            root = Path(workspace_path)
            found += _scan_skills(root, "workspace")
            found += _scan_commands(root, "workspace")
        claude_home = home / ".claude"
        found += _scan_skills(claude_home, "user")
        found += _scan_commands(claude_home, "user")
        found += CLAUDE_BUILTINS
    elif provider == "codex":
        found += _scan_codex_prompts(home)

    # A workspace definition shadows a user-level one of the same name.
    seen: set[tuple[str, str]] = set()
    unique: list[Capability] = []
    for cap in found:
        key = (cap.kind, cap.name)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cap)
    return unique
