"""Hot-reload and capability ingestion system.

Watches skills/, src/tools/, and config/mcp.yaml for changes.
Reloads without full restart. Can also ingest from URLs and conversation.
"""

import importlib
import logging
import os
import time
from pathlib import Path

import yaml

from src.core.skills import load_skills, get_all_skills, _skill_registry
from src.core.tools import _tool_registry

logger = logging.getLogger(__name__)


class Digest:
    """Watches for changes and hot-reloads modules."""

    def __init__(self, check_interval: float = 5.0):
        self.check_interval = check_interval
        self._mtimes: dict[str, float] = {}
        self._running = False

        # Directories to watch
        self._watch_paths = {
            "skills": Path("skills"),
            "tools": Path("src/tools"),
            "mcp_config": Path("config/mcp.yaml"),
        }

    async def start(self, on_reload=None):
        """Start the file watcher loop."""
        import asyncio
        self._running = True
        self._on_reload = on_reload
        self._snapshot_mtimes()

        while self._running:
            await asyncio.sleep(self.check_interval)
            changes = self._check_changes()
            if changes:
                await self._handle_changes(changes)

    def stop(self):
        self._running = False

    def _snapshot_mtimes(self) -> None:
        """Record current modification times for all watched files."""
        for category, path in self._watch_paths.items():
            if path.is_dir():
                for f in path.glob("*.yaml" if category == "skills" else "*.py"):
                    self._mtimes[str(f)] = f.stat().st_mtime
            elif path.is_file():
                self._mtimes[str(path)] = path.stat().st_mtime

    def _check_changes(self) -> dict[str, list[str]]:
        """Check for new/modified/deleted files. Returns changes by category."""
        changes: dict[str, list[str]] = {}

        for category, path in self._watch_paths.items():
            if path.is_dir():
                pattern = "*.yaml" if category == "skills" else "*.py"
                current_files = {str(f): f.stat().st_mtime for f in path.glob(pattern)}
            elif path.is_file() and path.exists():
                current_files = {str(path): path.stat().st_mtime}
            else:
                current_files = {}

            # Check for new or modified files
            for filepath, mtime in current_files.items():
                old_mtime = self._mtimes.get(filepath)
                if old_mtime is None or mtime > old_mtime:
                    changes.setdefault(category, []).append(filepath)
                    self._mtimes[filepath] = mtime

            # Check for deleted files (only for directories)
            if path.is_dir():
                for filepath in list(self._mtimes.keys()):
                    if filepath.startswith(str(path)) and filepath not in current_files:
                        changes.setdefault(category, []).append(f"DELETED:{filepath}")
                        del self._mtimes[filepath]

        return changes

    async def _handle_changes(self, changes: dict[str, list[str]]) -> None:
        """Process detected changes."""
        if "skills" in changes:
            count = reload_skills()
            logger.info(f"Hot-reload: {count} skills reloaded ({changes['skills']})")

        if "tools" in changes:
            count = reload_tools()
            logger.info(f"Hot-reload: {count} tools reloaded ({changes['tools']})")

        if "mcp_config" in changes:
            logger.info(f"Hot-reload: MCP config changed — reconnect needed")

        if self._on_reload:
            await self._on_reload(changes)


def reload_skills() -> int:
    """Re-scan skills directory and update the registry."""
    _skill_registry.clear()
    skills = load_skills("skills")
    return len(skills)


def reload_tools() -> int:
    """Re-import all tool modules to pick up new/changed @tool functions."""
    # Clear existing tools (except builtins that might be imported differently)
    tool_names_before = set(_tool_registry.keys())

    # Re-import all modules in src/tools/
    tools_dir = Path("src/tools")
    for py_file in tools_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        module_name = f"src.tools.{py_file.stem}"
        try:
            if module_name in importlib.sys.modules:
                importlib.reload(importlib.sys.modules[module_name])
            else:
                importlib.import_module(module_name)
        except Exception as e:
            logger.error(f"Failed to reload {module_name}: {e}")

    new_tools = set(_tool_registry.keys()) - tool_names_before
    if new_tools:
        logger.info(f"New tools discovered: {new_tools}")

    return len(_tool_registry)


def ingest_skill_from_text(name: str, description: str, prompt: str,
                           tools: list[str], parameters: dict | None = None) -> Path:
    """Create a skill YAML file from provided text. Returns the file path."""
    skill_data = {
        "name": name,
        "description": description,
        "prompt": prompt,
        "tools": tools,
    }
    if parameters:
        skill_data["parameters"] = parameters

    skill_path = Path("skills") / f"{name}.yaml"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    with open(skill_path, "w") as f:
        yaml.dump(skill_data, f, default_flow_style=False, sort_keys=False)

    # Hot-reload immediately
    reload_skills()
    return skill_path


def ingest_mcp_server(name: str, url: str, transport: str = "sse") -> None:
    """Add an MCP server to config/mcp.yaml."""
    mcp_path = Path("config/mcp.yaml")
    if mcp_path.exists():
        with open(mcp_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    config.setdefault("servers", {})[name] = {
        "url": url,
        "transport": transport,
    }

    with open(mcp_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Added MCP server: {name} at {url}")
