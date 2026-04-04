"""Startup context injection — provides key context on first message of a session.

Injects team roster, system profile, and codex index so agents always have
foundational knowledge without relying on memory search or LLM instructions.
Only fires on the first message of a session; subsequent messages in the same
session inherit context via Claude Code's --resume.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TEAM_FILE = PROJECT_ROOT / "config" / "team.json"
CODEX_INDEX = PROJECT_ROOT / "codex" / "_index.md"


def _build_team_summary() -> str | None:
    """Build a compact team roster block from team.json."""
    if not TEAM_FILE.exists():
        return None

    try:
        team = json.loads(TEAM_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    lines = ["<team-roster>"]

    for h in team.get("humans", []):
        name = h.get("name", "?")
        role = h.get("role", "")
        access = h.get("access", "")
        discord = h.get("contact", {}).get("discord", "")
        line = f"  - {name}: {role} ({access})"
        if discord:
            line += f" [discord:{discord}]"
        lines.append(line)

    for a in team.get("agents", []):
        name = a.get("name", "?")
        role = a.get("role", "")
        domain = a.get("domain", "")
        discord = a.get("discord", "")
        line = f"  - {name}: {role} — {domain}"
        if discord:
            line += f" [discord:{discord}]"
        lines.append(line)

    lines.append("</team-roster>")
    return "\n".join(lines)


def _build_codex_index() -> str | None:
    """Build a codex awareness block from the index file."""
    if not CODEX_INDEX.exists():
        return None

    try:
        content = CODEX_INDEX.read_text()
    except OSError:
        return None

    return f"<codex-index>\n{content.strip()}\n</codex-index>"


async def build_startup_context(agent_id: str, memory=None) -> str | None:
    """Assemble startup context for a new agent session.

    Returns a context string to prepend to the first message, or None.
    """
    blocks = []

    # Team roster
    team = _build_team_summary()
    if team:
        blocks.append(team)

    # Pinned memories from DB (operational knowledge only — NOT team/codex data)
    if memory:
        try:
            pinned = await memory.context(agent_id, limit=10)
            if pinned:
                # Filter out team-member entries (team.json is the source of truth)
                filtered = [
                    m for m in pinned
                    if "team-member" not in (m.get("tags", "") or "")
                    and not (m.get("content", "")).startswith("Team member:")
                ]
                if filtered:
                    lines = ["<pinned-context>"]
                    for m in filtered:
                        content = m.get("content", "")
                        if len(content) > 500:
                            content = content[:500] + "..."
                        lines.append(f"  - {content}")
                    lines.append("</pinned-context>")
                    blocks.append("\n".join(lines))
        except Exception as e:
            logger.debug(f"Pinned memory fetch failed: {e}")

    # Codex index (awareness of available business knowledge)
    codex = _build_codex_index()
    if codex:
        blocks.append(codex)

    if not blocks:
        return None

    return "\n\n".join(blocks)
