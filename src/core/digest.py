"""Hot-reload and capability ingestion system.

Watches skills/, src/tools/, and config/mcp.yaml for changes across all layers
(Core, OTHS, overlay). Reloads without full restart.
"""

import importlib
import importlib.util
import logging
import os
import time
from pathlib import Path

import yaml

from src.core.skills import load_skills, get_all_skills, _skill_registry
from src.core.tools import _tool_registry

logger = logging.getLogger(__name__)


def _build_watch_paths() -> dict[str, Path]:
    """Build watch paths across all layers: Core, OTHS ($TARS_OTHS), overlay ($TARS_OVERLAY)."""
    paths: dict[str, Path] = {
        "skills": Path("skills"),
        "tools": Path("src/tools"),
        "mcp_config": Path("config/mcp.yaml"),
    }

    oths_raw = os.environ.get("TARS_OTHS", "")
    for oths in oths_raw.split(":"):
        if not oths.strip():
            continue
        oths_path = Path(oths.strip())
        oths_label = oths_path.name
        for subdir in ("tools", "services"):
            d = oths_path / subdir
            if d.is_dir():
                paths[f"tools_oths_{oths_label}_{subdir}"] = d
        d = oths_path / "skills"
        if d.is_dir():
            paths[f"skills_oths_{oths_label}"] = d

    overlay = os.environ.get("TARS_OVERLAY")
    if overlay:
        overlay_path = Path(overlay)
        d = overlay_path / "tools"
        if d.is_dir():
            paths["tools_overlay"] = d
        d = overlay_path / "skills"
        if d.is_dir():
            paths["skills_overlay"] = d

    return paths


class Digest:
    """Watches for changes and hot-reloads modules."""

    def __init__(self, check_interval: float = 5.0):
        self.check_interval = check_interval
        self._mtimes: dict[str, float] = {}
        self._running = False

        # Directories to watch (across all layers)
        self._watch_paths = _build_watch_paths()

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
        # Any skills category (core, oths, overlay) triggers full skills reload
        skills_changed = any(k.startswith("skills") for k in changes)
        tools_changed = any(k.startswith("tools") for k in changes)

        if skills_changed:
            count = reload_skills()
            skills_files = [f for k, v in changes.items() if k.startswith("skills") for f in v]
            logger.info(f"Hot-reload: {count} skills reloaded ({skills_files})")

        if tools_changed:
            count = reload_tools()
            tools_files = [f for k, v in changes.items() if k.startswith("tools") for f in v]
            logger.info(f"Hot-reload: {count} tools reloaded ({tools_files})")

        if "mcp_config" in changes:
            logger.info(f"Hot-reload: MCP config changed — reconnect needed")

        if self._on_reload:
            # Normalise category names for callback (just "skills" and "tools")
            normalised = {}
            for k, v in changes.items():
                if k.startswith("skills"):
                    normalised.setdefault("skills", []).extend(v)
                elif k.startswith("tools"):
                    normalised.setdefault("tools", []).extend(v)
                else:
                    normalised[k] = v
            await self._on_reload(normalised)


def reload_skills() -> int:
    """Re-scan skills directories across all layers and update the registry."""
    _skill_registry.clear()

    # Core skills
    load_skills("skills")

    # OTHS skills
    oths_raw = os.environ.get("TARS_OTHS", "")
    for oths in oths_raw.split(":"):
        if not oths.strip():
            continue
        oths_skills = Path(oths.strip()) / "skills"
        if oths_skills.is_dir():
            load_skills(oths_skills)

    # Overlay skills
    overlay = os.environ.get("TARS_OVERLAY")
    if overlay:
        overlay_skills = Path(overlay) / "skills"
        if overlay_skills.is_dir():
            load_skills(overlay_skills)

    return len(_skill_registry)


def reload_tools() -> int:
    """Re-import all tool modules across all layers."""
    tool_names_before = set(_tool_registry.keys())

    # Core tools
    _reload_tools_dir(Path("src/tools"), prefix="src.tools.")

    # OTHS tools
    oths_raw = os.environ.get("TARS_OTHS", "")
    for oths in oths_raw.split(":"):
        if not oths.strip():
            continue
        oths_path = Path(oths.strip())
        oths_label = oths_path.name
        for subdir in ("tools", "services"):
            d = oths_path / subdir
            if d.is_dir():
                _reload_tools_dir(d, prefix=f"tars_oths_{oths_label}_{subdir}_")

    # Overlay tools
    overlay = os.environ.get("TARS_OVERLAY")
    if overlay:
        d = Path(overlay) / "tools"
        if d.is_dir():
            _reload_tools_dir(d, prefix="tars_overlay_")

    new_tools = set(_tool_registry.keys()) - tool_names_before
    if new_tools:
        logger.info(f"New tools discovered: {new_tools}")

    return len(_tool_registry)


def _reload_tools_dir(tools_dir: Path, prefix: str) -> None:
    """Re-import all .py files in a tools directory."""
    for py_file in tools_dir.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        if prefix.startswith("src."):
            module_name = f"{prefix}{py_file.stem}"
        else:
            module_name = f"{prefix}{py_file.stem}"
        try:
            if module_name in importlib.sys.modules:
                importlib.reload(importlib.sys.modules[module_name])
            else:
                if prefix.startswith("src."):
                    importlib.import_module(module_name)
                else:
                    # Layer file — import by file path
                    spec = importlib.util.spec_from_file_location(module_name, py_file)
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        importlib.sys.modules[module_name] = mod
                        spec.loader.exec_module(mod)
        except Exception as e:
            logger.error(f"Failed to reload {module_name}: {e}")


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
