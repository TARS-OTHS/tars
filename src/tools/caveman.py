"""Caveman mode tool — toggle terse communication style per agent."""

import os
import re

from src.core.base import ToolContext
from src.core.tools import tool

VALID_LEVELS = ("off", "lite", "full", "ultra")
SECTION_HEADER = "## Communication Style"
CAVEMAN_PATTERN = re.compile(r"See @.*CAVEMAN\.md.*active \w+ mode\.")
ENFORCE_PATTERN = re.compile(r"CRITICAL: Apply caveman rules to EVERY response")


def _resolve_claude_md(agent_id: str) -> str | None:
    """Find the agent's overlay CLAUDE.md."""
    overlay = os.environ.get("TARS_OVERLAY", "")
    if overlay:
        path = os.path.join(overlay, "agents", agent_id, "CLAUDE.md")
        if os.path.isfile(path):
            return path
    tars_home = os.environ.get("TARS_HOME", "/opt/tars")
    path = os.path.join(tars_home, "agents", agent_id, "CLAUDE.md")
    if os.path.isfile(path):
        return path
    return None


def _resolve_caveman_md(claude_md_path: str) -> str | None:
    """Find CAVEMAN.md relative to the overlay config."""
    overlay = os.environ.get("TARS_OVERLAY", "")
    if overlay:
        path = os.path.join(overlay, "config", "CAVEMAN.md")
        if os.path.isfile(path):
            return path
    tars_home = os.environ.get("TARS_HOME", "/opt/tars")
    path = os.path.join(tars_home, "config", "CAVEMAN.md")
    if os.path.isfile(path):
        return path
    return None


@tool(
    name="caveman",
    description="Toggle caveman (terse) communication style for the current agent",
    category="ops",
)
async def caveman(ctx: ToolContext, level: str = "") -> str:
    """Set caveman communication level for the calling agent.

    Args:
        level: One of: off, lite, full, ultra. Empty to show current level.
    """
    agent_id = ctx.agent_id
    claude_md_path = _resolve_claude_md(agent_id)
    if not claude_md_path:
        return f"No CLAUDE.md found for agent '{agent_id}'"

    content = open(claude_md_path).read()

    # Show current level if no argument
    if not level:
        match = CAVEMAN_PATTERN.search(content)
        if match:
            for lv in ("lite", "full", "ultra"):
                if lv in match.group():
                    return f"Caveman: {lv} (agent: {agent_id})"
            return f"Caveman: on (agent: {agent_id})"
        return f"Caveman: off (agent: {agent_id})"

    level = level.lower().strip()
    if level not in VALID_LEVELS:
        return f"Invalid level '{level}'. Choose: {', '.join(VALID_LEVELS)}"

    lines = content.splitlines()

    # Remove all caveman-related lines (section headers, enforcement, references)
    new_lines = []
    for line in lines:
        if line.strip() == SECTION_HEADER:
            continue
        if CAVEMAN_PATTERN.search(line):
            continue
        if ENFORCE_PATTERN.search(line):
            continue
        new_lines.append(line)

    # Collapse runs of 3+ blank lines down to 2
    collapsed = []
    blank_run = 0
    for line in new_lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)
    new_lines = collapsed

    # Strip trailing blank lines
    while new_lines and new_lines[-1].strip() == "":
        new_lines.pop()

    if level != "off":
        caveman_md_path = _resolve_caveman_md(claude_md_path)
        if not caveman_md_path:
            return "CAVEMAN.md not found in config"
        rel_path = os.path.relpath(caveman_md_path, os.path.dirname(claude_md_path))
        ref_line = f"See @{rel_path} — active {level} mode."
        enforce = "CRITICAL: Apply caveman rules to EVERY response from first message. No normal prose. No exceptions."
        new_lines.extend(["", SECTION_HEADER, "", enforce, "", ref_line, ""])

    with open(claude_md_path, "w") as f:
        f.write("\n".join(new_lines))
        if not new_lines[-1].strip() == "":
            f.write("\n")

    if level == "off":
        return f"Caveman disabled for {agent_id}. Takes effect next session."
    return f"Caveman set to {level} for {agent_id}. Takes effect next session."
