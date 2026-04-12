"""Hot-reload and capability ingestion system.

Watches skills/, src/tools/, and config/mcp.yaml for changes across all layers
(Core, OTHS, overlay). Reloads without full restart.
"""

import importlib
import importlib.util
import json
import logging
import os
import time
from pathlib import Path

import yaml

from src.core.base import PROJECT_ROOT
from src.core.skills import load_skills, get_all_skills, _skill_registry
from src.core.tools import _tool_registry

logger = logging.getLogger(__name__)


def _build_watch_paths() -> dict[str, Path]:
    """Build watch paths across all layers: Core, OTHS ($TARS_OTHS), overlay ($TARS_OVERLAY)."""
    paths: dict[str, Path] = {
        "skills": PROJECT_ROOT / "skills",
        "tools": PROJECT_ROOT / "src" / "tools",
        "mcp_config": PROJECT_ROOT / "config" / "mcp.yaml",
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
    _reload_tools_dir(PROJECT_ROOT / "src" / "tools", prefix="src.tools.")

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

    skill_path = PROJECT_ROOT / "skills" / f"{name}.yaml"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    with open(skill_path, "w") as f:
        yaml.dump(skill_data, f, default_flow_style=False, sort_keys=False)

    # Hot-reload immediately
    reload_skills()
    return skill_path


def ingest_mcp_server(
    name: str,
    *,
    transport: str = "sse",
    url: str = "",
    command: str = "",
    args: list[str] | None = None,
    cwd: str = "",
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    description: str = "",
) -> None:
    """Add an MCP server to config/mcp.yaml and regenerate .mcp.json files.

    Supports both remote (SSE) and local (stdio) servers.
    """
    mcp_path = _resolve_mcp_yaml()
    if mcp_path.exists():
        with open(mcp_path) as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
        mcp_path.parent.mkdir(parents=True, exist_ok=True)

    entry: dict = {"transport": transport}
    if transport == "stdio":
        if not command:
            raise ValueError("stdio transport requires 'command'")
        entry["command"] = command
        if args:
            entry["args"] = args
        if cwd:
            entry["cwd"] = cwd
        if env:
            entry["env"] = env
    else:
        if not url:
            raise ValueError(f"{transport} transport requires 'url'")
        entry["url"] = url
        if headers:
            entry["headers"] = headers
    if description:
        entry["description"] = description

    config.setdefault("servers", {})[name] = entry

    with open(mcp_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    logger.info(f"Added MCP server: {name} ({transport})")

    # Regenerate .mcp.json files for all agents
    regenerate_mcp_json()


def _resolve_mcp_yaml() -> Path:
    """Find mcp.yaml — overlay takes precedence over Core."""
    overlay = os.environ.get("TARS_OVERLAY")
    if overlay:
        p = Path(overlay) / "config" / "mcp.yaml"
        if p.exists():
            return p
    return PROJECT_ROOT / "config" / "mcp.yaml"


def _load_mcp_servers() -> dict:
    """Load all MCP server definitions from mcp.yaml."""
    mcp_path = _resolve_mcp_yaml()
    if not mcp_path.exists():
        return {}
    with open(mcp_path) as f:
        config = yaml.safe_load(f) or {}
    return config.get("servers", {})


def _build_mcp_json(servers: dict, agent_env: dict | None = None) -> dict:
    """Convert mcp.yaml servers to Claude Code .mcp.json format.

    Args:
        servers: Server definitions from mcp.yaml.
        agent_env: Per-agent env vars to merge into stdio servers.
    """
    mcp_servers = {}
    for name, cfg in servers.items():
        transport = cfg.get("transport", "sse")
        if transport == "stdio":
            entry = {"command": cfg["command"]}
            if cfg.get("args"):
                entry["args"] = cfg["args"]
            if cfg.get("cwd"):
                entry["cwd"] = cfg["cwd"]
            env = dict(cfg.get("env", {}))
            if agent_env:
                env.update(agent_env)
            if env:
                entry["env"] = env
        else:
            entry = {"type": "http", "url": cfg["url"]}
            if cfg.get("headers"):
                entry["headers"] = cfg["headers"]
        mcp_servers[name] = entry
    return {"mcpServers": mcp_servers}


def regenerate_mcp_json() -> list[Path]:
    """Regenerate .mcp.json files for all agents from mcp.yaml.

    Injects per-agent env vars (TARS_PROFILE, TARS_BOT_ACCOUNT) into
    stdio servers from agent config.

    Returns list of paths that were updated.
    """
    servers = _load_mcp_servers()
    if not servers:
        logger.info("No MCP servers in mcp.yaml — nothing to generate")
        return []

    agents = _load_all_agents()
    updated = []

    for agent_id, agent_cfg, agent_dir in agents:
        # Build per-agent env overrides for stdio servers
        bot_account = (agent_cfg.get("routing", {}).get("discord", {}).get("account", ""))
        profile = agent_cfg.get("profile", agent_id if agent_id != agent_cfg.get("name", agent_id) else "")
        agent_env = {}
        if bot_account:
            agent_env["TARS_BOT_ACCOUNT"] = bot_account
        if profile:
            agent_env["TARS_PROFILE"] = profile

        mcp_json = _build_mcp_json(servers, agent_env=agent_env)
        target = agent_dir / ".mcp.json"
        target.write_text(json.dumps(mcp_json, indent=2) + "\n")
        updated.append(target)
        logger.info(f"Updated {target}")

    return updated


def _load_all_agents() -> list[tuple[str, dict, Path]]:
    """Load all agents from agents*.yaml files. Returns (agent_id, config, project_dir)."""
    overlay = os.environ.get("TARS_OVERLAY")

    agent_files = []
    for base in [Path(overlay) if overlay else None, PROJECT_ROOT]:
        if base is None:
            continue
        for config_dir in [base, base / "config"]:
            if not config_dir.is_dir():
                continue
            for f in config_dir.iterdir():
                if f.name == "agents.yaml" or (
                    f.name.startswith("agents.") and f.name.endswith(".yaml") and f.name != "agents.yaml.example"
                ):
                    agent_files.append(f)

    all_agents = {}
    for af in agent_files:
        with open(af) as fh:
            config = yaml.safe_load(fh) or {}
        all_agents.update(config.get("agents", {}))

    results = []
    for agent_id, agent_cfg in all_agents.items():
        project_dir = agent_cfg.get("project_dir", f"./agents/{agent_id}")
        resolved = Path(project_dir).resolve()
        if not resolved.is_dir() and overlay:
            overlay_dir = Path(overlay) / "agents" / agent_id
            if overlay_dir.is_dir():
                resolved = overlay_dir
        if resolved.is_dir():
            results.append((agent_id, agent_cfg, resolved))
    return results
