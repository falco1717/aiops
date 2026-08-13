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


# Commands that only make sense in an interactive terminal; filtered out of the
# CLI-reported list so the composer does not offer something that cannot work.
TERMINAL_ONLY = {
    "login", "logout", "clear", "exit", "quit", "help", "vim", "terminal-setup",
    "heapdump", "doctor", "install-github-app", "bug", "release-notes", "resume",
    "upgrade", "privacy-settings", "theme", "color", "statusline",
}

# Fallback descriptions, and the list used when the CLI has not reported one yet
# (a brand-new session that has not run a turn).
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
    Capability(
        name="context",
        kind="builtin",
        description="Report what is currently filling the context window",
        source="built-in",
    ),
    Capability(
        name="usage",
        kind="builtin",
        description="Show plan usage and limits for the signed-in account",
        source="built-in",
    ),
    Capability(
        name="compact",
        kind="builtin",
        description="Summarise the conversation so far to free up context",
        source="built-in",
    ),
    Capability(
        name="init",
        kind="builtin",
        description="Write a CLAUDE.md describing this codebase",
        source="built-in",
    ),
    Capability(
        name="security-review",
        kind="builtin",
        description="Review the pending changes for security problems",
        source="built-in",
    ),
]


def discover(
    provider: str,
    workspace_path: str | None,
    reported_commands: list[str] | None = None,
) -> list[Capability]:
    """Skills and commands available to a session, most specific first.

    `reported_commands` is the list the CLI itself advertised in its startup
    event. It is authoritative — it reflects the installed version, plugins and
    skills — so it is preferred over the hardcoded fallback below.
    """
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
        if reported_commands:
            described = {c.name: c.description for c in CLAUDE_BUILTINS}
            found += [
                Capability(
                    name=name,
                    kind="builtin",
                    description=described.get(name, ""),
                    source="CLI",
                )
                for name in reported_commands
                if name not in TERMINAL_ONLY
            ]
        else:
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
