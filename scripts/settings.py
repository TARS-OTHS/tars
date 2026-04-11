#!/usr/bin/env python3
"""T.A.R.S Settings Manager.

Interactive post-install tool for viewing and modifying T.A.R.S configuration.
Reads existing config files, presents a menu, writes changes back.

Usage: uv run python scripts/settings.py
"""

import getpass
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Resolve project root (parent of scripts/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Overlay deployments set TARS_OVERLAY — config lives there, not in core
_OVERLAY = os.environ.get("TARS_OVERLAY")
CONFIG_DIR = Path(_OVERLAY) / "config" if _OVERLAY else PROJECT_ROOT / "config"

try:
    import yaml
except ImportError:
    print("Run 'uv sync' first to install dependencies.")
    sys.exit(1)

try:
    from src.vault.fernet import FernetVault
except ImportError:
    print("Run 'uv sync' first to install dependencies.")
    sys.exit(1)


# --- Formatting (matches setup.py) ---

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def banner():
    print(f"""
{BOLD}{CYAN}╔══════════════════════════════════════╗
║       T.A.R.S Settings Manager       ║
║    The Agent Routing System          ║
╚══════════════════════════════════════╝{RESET}
""")


def header(text: str):
    print(f"\n{BOLD}{CYAN}── {text} ──{RESET}\n")


def ok(text: str):
    print(f"  {GREEN}✓{RESET} {text}")


def warn(text: str):
    print(f"  {YELLOW}!{RESET} {text}")


def err(text: str):
    print(f"  {RED}✗{RESET} {text}")


def info(text: str):
    print(f"  {DIM}{text}{RESET}")


def show(key: str, value, indent: int = 2):
    pad = " " * indent
    if isinstance(value, list):
        if not value:
            print(f"{pad}{BOLD}{key}:{RESET} {DIM}(empty){RESET}")
        else:
            print(f"{pad}{BOLD}{key}:{RESET}")
            for item in value:
                print(f"{pad}  - {item}")
    elif isinstance(value, dict):
        print(f"{pad}{BOLD}{key}:{RESET}")
        for k, v in value.items():
            show(k, v, indent + 2)
    else:
        print(f"{pad}{BOLD}{key}:{RESET} {value}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result or default


def ask_yn(prompt: str, default: bool = True) -> bool:
    yn = "[Y/n]" if default else "[y/N]"
    result = input(f"  {prompt} {yn}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"  {prompt}: ").strip()


def ask_choice(prompt: str, options: list[str], default: str = "") -> str:
    for i, opt in enumerate(options, 1):
        marker = f" {DIM}(current){RESET}" if opt == default else ""
        print(f"    {i}) {opt}{marker}")
    while True:
        result = input(f"  {prompt}: ").strip()
        if not result and default:
            return default
        if result in options:
            return result
        try:
            idx = int(result) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print(f"  Please enter 1-{len(options)} or a valid option.")


def ask_int(prompt: str, default: int) -> int:
    while True:
        result = input(f"  {prompt} [{default}]: ").strip()
        if not result:
            return default
        try:
            return int(result)
        except ValueError:
            print("  Please enter a number.")


# --- Config I/O ---

def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _display_path(path: Path) -> Path | str:
    try:
        return path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path


def save_yaml(path: Path, data: dict):
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    ok(f"Saved {_display_path(path)}")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    path.write_text(json.dumps(data, indent=2) + "\n")
    ok(f"Saved {_display_path(path)}")


def load_config() -> dict:
    return load_yaml(CONFIG_DIR / "config.yaml")


def save_config(cfg: dict):
    save_yaml(CONFIG_DIR / "config.yaml", cfg)


def load_agents() -> dict:
    return load_yaml(CONFIG_DIR / "agents.yaml")


def save_agents(agents: dict):
    save_yaml(CONFIG_DIR / "agents.yaml", agents)


def get_vault() -> FernetVault | None:
    """Try to open the vault. Returns None if unavailable."""
    vault_path = CONFIG_DIR / "secrets.enc"
    if not vault_path.exists():
        return None
    vault = FernetVault(str(vault_path))
    key_file = Path.home() / ".config" / "tars-vault-key"
    if key_file.exists():
        try:
            vault.unlock(key_file.read_text().strip())
            return vault
        except ValueError:
            pass
    for _ in range(3):
        passphrase = ask_secret("Vault passphrase")
        try:
            vault.unlock(passphrase)
            return vault
        except ValueError:
            err("Wrong passphrase.")
    return None


# --- Discord API ---

def validate_discord_token(token: str) -> dict | None:
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


# ==========================================================================
# Section editors
# ==========================================================================

def edit_llm(cfg: dict):
    header("LLM Defaults")
    llm = cfg.setdefault("defaults", {}).setdefault("llm", {})

    show("provider", llm.get("provider", "claude_code"))
    show("model", llm.get("model", "sonnet"))
    show("max_tokens", llm.get("max_tokens", 4096))
    show("mcp_config", llm.get("mcp_config", "(not set)"))
    print()

    if ask_yn("Edit LLM defaults?"):
        llm["provider"] = ask("Provider", llm.get("provider", "claude_code"))
        llm["model"] = ask_choice("Model", ["sonnet", "opus", "haiku"], default=llm.get("model", "sonnet"))
        llm["max_tokens"] = ask_int("Max tokens", llm.get("max_tokens", 4096))

        mcp = ask("MCP config path (empty to clear)", llm.get("mcp_config", ""))
        if mcp:
            llm["mcp_config"] = mcp
        elif "mcp_config" in llm:
            del llm["mcp_config"]

        save_config(cfg)


def edit_connectors(cfg: dict):
    header("Connectors")
    connectors = cfg.setdefault("connectors", {})

    # Show current state
    for name, conn in connectors.items():
        enabled = conn.get("enabled", False)
        status = f"{GREEN}enabled{RESET}" if enabled else f"{DIM}disabled{RESET}"
        print(f"  {BOLD}{name}{RESET}: {status}")
        if name == "discord" and "accounts" in conn:
            for bot_name, bot_cfg in conn["accounts"].items():
                print(f"    bot: {bot_name} (token: {bot_cfg.get('token_key', '?')})")
        if name == "http" and conn.get("port"):
            print(f"    port: {conn['port']}")

    print()
    print("  Options:")
    print("    1) Toggle connector on/off")
    print("    2) Add Discord bot account")
    print("    3) Remove Discord bot account")
    print("    4) Set HTTP port")
    print("    5) Back")
    print()

    while True:
        choice = ask("Choice", "5")
        if choice in ("5", "back", "b", ""):
            break
        elif choice == "1":
            name = ask("Connector name (discord/telegram/http)")
            if name in connectors:
                current = connectors[name].get("enabled", False)
                connectors[name]["enabled"] = not current
                state = "enabled" if not current else "disabled"
                ok(f"{name} {state}")
                save_config(cfg)
            else:
                err(f"Unknown connector: {name}")
        elif choice == "2":
            _add_bot_account(cfg)
        elif choice == "3":
            discord = connectors.get("discord", {})
            accounts = discord.get("accounts", {})
            if len(accounts) <= 1:
                err("Can't remove last bot account.")
            else:
                name = ask("Bot account name to remove")
                if name in accounts:
                    del accounts[name]
                    ok(f"Removed bot account '{name}'")
                    save_config(cfg)
                else:
                    err(f"No bot account named '{name}'")
        elif choice == "4":
            connectors.setdefault("http", {"enabled": False})
            connectors["http"]["port"] = ask_int("HTTP port", connectors["http"].get("port", 8080))
            save_config(cfg)


def _add_bot_account(cfg: dict):
    """Add a new Discord bot account."""
    bot_name = ask("Bot account name (internal, e.g. 'luna')")
    if not bot_name:
        return

    vault = get_vault()
    if vault is None:
        err("Vault unavailable — can't store token.")
        return

    token = ask_secret("Bot token")
    if not token:
        warn("Skipped.")
        return

    info("Validating token...")
    bot_info = validate_discord_token(token)
    if bot_info:
        ok(f"Bot verified: {bot_info.get('username', '?')}")
    else:
        if not ask_yn("Validation failed. Store anyway?", default=False):
            return

    vault_key = f"discord-{bot_name}"
    vault.set(vault_key, token)
    ok(f"Token stored as '{vault_key}'")

    discord = cfg.setdefault("connectors", {}).setdefault("discord", {"enabled": True})
    discord.setdefault("accounts", {})[bot_name] = {"token_key": vault_key}
    save_config(cfg)


def edit_session(cfg: dict):
    header("Session Defaults")
    session = cfg.setdefault("defaults", {}).setdefault("session", {})

    show("max_history", session.get("max_history", 50))
    show("summarize_after", session.get("summarize_after", 30))
    print()

    if ask_yn("Edit session settings?"):
        session["max_history"] = ask_int("Max history messages", session.get("max_history", 50))
        session["summarize_after"] = ask_int("Summarize after N messages", session.get("summarize_after", 30))
        save_config(cfg)


def edit_memory(cfg: dict):
    header("Memory")
    memory = cfg.setdefault("defaults", {}).setdefault("memory", {})

    show("backend", memory.get("backend", "sqlite"))
    show("semantic_search", memory.get("semantic_search", False))
    show("decay_enabled", memory.get("decay_enabled", False))
    show("max_results", memory.get("max_results", 10))
    print()

    if ask_yn("Edit memory settings?"):
        memory["backend"] = ask_choice("Backend", ["sqlite"], default=memory.get("backend", "sqlite"))

        current_semantic = memory.get("semantic_search", False)
        memory["semantic_search"] = ask_yn("Enable semantic search (requires embedding model)?", default=current_semantic)

        if memory["semantic_search"] and not current_semantic:
            info("Semantic search requires the BGE embedding model (~50MB).")
            info("It will be downloaded automatically on first use.")

        memory["decay_enabled"] = ask_yn("Enable memory decay?", default=memory.get("decay_enabled", False))
        memory["max_results"] = ask_int("Max results per search", memory.get("max_results", 10))
        save_config(cfg)


def edit_hitl(cfg: dict):
    header("Security — Human-in-the-Loop")
    hitl = cfg.setdefault("security", {}).setdefault("hitl", {})

    show("connector", hitl.get("connector", "discord"))
    show("channel", hitl.get("channel", "(not set)"))
    show("approvers", hitl.get("approvers", []))
    show("timeout", hitl.get("timeout", 1800))
    show("fail_mode", hitl.get("fail_mode", "closed"))
    show("poll_interval", hitl.get("poll_interval", 3))
    show("gated_tools", hitl.get("gated_tools", []))
    print()

    if ask_yn("Edit HITL settings?"):
        hitl["channel"] = ask("Approval channel ID", hitl.get("channel", ""))
        hitl["timeout"] = ask_int("Timeout (seconds)", hitl.get("timeout", 1800))
        hitl["fail_mode"] = ask_choice("Fail mode", ["closed", "open"], default=hitl.get("fail_mode", "closed"))
        hitl["poll_interval"] = ask_int("Poll interval (seconds)", hitl.get("poll_interval", 3))

        # Approvers
        print()
        current = hitl.get("approvers", [])
        if current:
            info(f"Current approvers: {', '.join(current)}")
        if ask_yn("Reset approver list?", default=False):
            current = []
        while ask_yn("Add an approver?", default=not current):
            uid = ask("Discord user ID")
            if uid and uid not in current:
                current.append(uid)
        hitl["approvers"] = current

        # Gated tools
        print()
        gated = hitl.get("gated_tools", [])
        if gated:
            info(f"Current gated tools: {', '.join(gated)}")
        if ask_yn("Reset gated tools list?", default=False):
            gated = []
        while ask_yn("Add a gated tool?", default=not gated):
            tool = ask("Tool name")
            if tool and tool not in gated:
                gated.append(tool)
        hitl["gated_tools"] = gated

        save_config(cfg)


def edit_rate_limits(cfg: dict):
    header("Rate Limits")
    rl = cfg.setdefault("security", {}).setdefault("rate_limits", {})

    show("mode", rl.get("mode", "log"))
    defaults = rl.get("defaults", {})
    show("max_per_hour", defaults.get("max_per_hour", 100))
    print()

    if ask_yn("Edit rate limits?"):
        rl["mode"] = ask_choice("Mode", ["log", "enforce"], default=rl.get("mode", "log"))
        rl.setdefault("defaults", {})["max_per_hour"] = ask_int(
            "Max requests per hour", defaults.get("max_per_hour", 100)
        )
        save_config(cfg)


def edit_compression(cfg: dict):
    header("Context Compression")
    comp = cfg.setdefault("security", {}).setdefault("compression", {})

    show("enabled", comp.get("enabled", False))
    show("level", comp.get("level", "standard"))
    show("memory_recall", comp.get("memory_recall", False))
    print()

    if ask_yn("Edit compression settings?"):
        comp["enabled"] = ask_yn("Enable context compression?", default=comp.get("enabled", False))
        if comp["enabled"]:
            comp["level"] = ask_choice("Level", ["lite", "standard"], default=comp.get("level", "standard"))
            comp["memory_recall"] = ask_yn(
                "Enable memory recall compression?", default=comp.get("memory_recall", False)
            )
        save_config(cfg)


def edit_admin_users(cfg: dict):
    header("Admin Users")
    admins = cfg.setdefault("admin_users", {})

    for platform, users in admins.items():
        show(platform, users)
    print()

    if ask_yn("Edit admin users?"):
        platform = ask("Platform", "discord")
        current = admins.get(platform, [])
        if current:
            info(f"Current: {', '.join(current)}")
        if ask_yn("Reset list?", default=False):
            current = []
        while ask_yn("Add a user ID?", default=not current):
            uid = ask("User ID")
            if uid and uid not in current:
                current.append(uid)
        admins[platform] = current
        save_config(cfg)


def edit_tars_identity(cfg: dict):
    header("T.A.R.S Identity")
    tars = cfg.setdefault("tars", {})

    show("name", tars.get("name", "T.A.R.S"))
    show("log_level", tars.get("log_level", "info"))
    show("data_dir", tars.get("data_dir", "./data"))
    print()

    if ask_yn("Edit identity settings?"):
        tars["name"] = ask("System name", tars.get("name", "T.A.R.S"))
        tars["log_level"] = ask_choice(
            "Log level", ["debug", "info", "warning", "error"], default=tars.get("log_level", "info")
        )
        tars["data_dir"] = ask("Data directory", tars.get("data_dir", "./data"))
        save_config(cfg)


def manage_vault():
    header("Vault Secrets")
    vault = get_vault()
    if vault is None:
        err("Could not open vault.")
        return

    keys = vault.list_keys()
    if keys:
        info(f"{len(keys)} secret(s) stored:")
        for key in sorted(keys):
            print(f"    - {key}")
    else:
        info("Vault is empty.")

    print()
    print("  Options:")
    print("    1) Add / update a secret")
    print("    2) Remove a secret")
    print("    3) Back")
    print()

    while True:
        choice = ask("Choice", "3")
        if choice in ("3", "back", "b", ""):
            break
        elif choice == "1":
            key = ask("Secret name (e.g. discord-luna, anthropic-key)")
            if not key:
                continue
            value = ask_secret(f"Value for '{key}'")
            if value:
                vault.set(key, value)
                ok(f"Stored '{key}'")
        elif choice == "2":
            key = ask("Secret name to remove")
            if key and key in vault.list_keys():
                vault.delete(key)
                ok(f"Removed '{key}'")
            else:
                err(f"Secret '{key}' not found.")


def create_agent():
    header("Create New Agent")
    cfg = load_config()
    agents_cfg = load_agents()

    existing = list(agents_cfg.get("agents", {}).keys())
    if existing:
        info(f"Existing agents: {', '.join(existing)}")
    print()

    agent_name = ask("Agent internal name (lowercase, no spaces)")
    if not agent_name:
        return
    if agent_name in agents_cfg.get("agents", {}):
        err(f"Agent '{agent_name}' already exists.")
        return

    display_name = ask("Display name", agent_name.capitalize())
    description = ask("One-line description", "")
    model = ask_choice("Model", ["sonnet", "opus", "haiku"], default="sonnet")

    # Tier selection drives defaults
    print()
    info("Tier determines default permissions:")
    info("  privileged — full file/shell access (ops/dev agents)")
    info("  coordinator — MCP tools only, no builtins (team leads)")
    info("  assistant — MCP tools only, no builtins (helpers)")
    tier = ask_choice("Tier", ["privileged", "coordinator", "assistant"], default="assistant")

    privileged = tier == "privileged"
    disallow = [] if privileged else ["Edit", "Write", "Bash", "MultiEdit"]

    # Bot account
    discord_accounts = list(
        cfg.get("connectors", {}).get("discord", {}).get("accounts", {}).keys()
    )
    if discord_accounts:
        info(f"Available bot accounts: {', '.join(discord_accounts)}")
        bot_account = ask("Bot account", discord_accounts[0])
    else:
        bot_account = ask("Bot account", "main")

    mentions = ask_yn("Only respond when @mentioned?", default=True)

    # Channel filtering
    channels = []
    if ask_yn("Restrict to specific channel IDs?", default=False):
        while True:
            ch = ask("Channel ID (empty to stop)")
            if not ch:
                break
            channels.append(ch)

    # Personality
    personality = ask("Personality (e.g. 'concise and direct')", "concise and direct")

    # Per-agent LLM override
    agent_llm = {"provider": "claude_code", "model": model}

    # Per-agent compression override
    comp_override = None
    if ask_yn("Set per-agent compression override?", default=False):
        comp_override = {
            "enabled": ask_yn("Enable compression for this agent?", default=False),
            "level": ask_choice("Level", ["lite", "standard"], default="standard"),
        }

    # Build config entry
    agent_entry = {
        "display_name": display_name,
        "description": description,
        "project_dir": f"./agents/{agent_name}",
        "llm": agent_llm,
        "tools": "all",
        "skills": "all",
    }
    if privileged:
        agent_entry["privileged"] = True
    if disallow:
        agent_entry["disallow_builtins"] = disallow
    if comp_override:
        agent_entry["compression"] = comp_override
    agent_entry["routing"] = {
        "discord": {
            "account": bot_account,
            "channels": channels,
            "mentions": mentions,
        }
    }

    # Write agents.yaml
    agents_cfg.setdefault("agents", {})[agent_name] = agent_entry
    save_agents(agents_cfg)

    # Create agent directory
    agent_dir = PROJECT_ROOT / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md = f"""# {display_name}

## Identity

You are **{display_name}**. {description}.

## Guidelines

- Be {personality}
- Search memory before asking the user for context you might already have
- Remember important things from conversations by storing them to memory
- When handling tasks, break them down and track progress

## Memory System

Use your MCP tools for memory — do NOT use curl or HTTP calls.

- `memory_search` — keyword/FTS5 search
- `memory_semantic_search` — embedding-based conceptual search
- `memory_store` — save important information
- `memory_forget` — remove a memory by ID

## Team

The team roster is at `config/team.json`. User context is injected before each message so you know who you're talking to.
"""
    claude_md_path = agent_dir / "CLAUDE.md"
    if claude_md_path.exists():
        warn(f"CLAUDE.md already exists — skipping (won't overwrite)")
    else:
        claude_md_path.write_text(claude_md)
        ok(f"Created {agent_dir.relative_to(PROJECT_ROOT)}/CLAUDE.md")

    # .mcp.json
    mcp_path = agent_dir / ".mcp.json"
    if mcp_path.exists():
        warn(f".mcp.json already exists — skipping (won't overwrite)")
    else:
        mcp_json = {
            "mcpServers": {
                "tars-tools": {
                    "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(PROJECT_ROOT),
                }
            }
        }
        mcp_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created {agent_dir.relative_to(PROJECT_ROOT)}/.mcp.json")

    # .claude/settings.json
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        warn(f".claude/settings.json already exists — skipping (won't overwrite)")
    else:
        settings = {
            "permissions": {
                "allow": ["mcp__tars-tools__*"],
                "deny": [],
            }
        }
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created {agent_dir.relative_to(PROJECT_ROOT)}/.claude/settings.json")

    print()
    ok(f"Agent '{display_name}' created — directory: agents/{agent_name}/")
    info("Customise the CLAUDE.md to set the agent's full identity and behaviour.")
    info("Restart T.A.R.S to pick up the new agent.")


def view_agents():
    header("Agents Overview")
    agents_cfg = load_agents()
    agents = agents_cfg.get("agents", {})

    if not agents:
        info("No agents configured.")
        return

    for name, agent in agents.items():
        priv = f" {YELLOW}[privileged]{RESET}" if agent.get("privileged") else ""
        model = agent.get("llm", {}).get("model", "?")
        routing = agent.get("routing", {}).get("discord", {})
        bot = routing.get("account", "?")
        mentions = "mentions" if routing.get("mentions") else "all messages"
        channels = routing.get("channels", [])
        ch_str = f", channels: {','.join(channels)}" if channels else ""

        print(f"  {BOLD}{name}{RESET}{priv}")
        print(f"    {agent.get('display_name', name)} — {agent.get('description', '')}")
        print(f"    model: {model}, bot: {bot}, {mentions}{ch_str}")

        # Per-agent overrides
        comp = agent.get("compression")
        if comp:
            print(f"    compression: {comp}")
        disallow = agent.get("disallow_builtins", [])
        if disallow:
            print(f"    disallow_builtins: {', '.join(disallow)}")
        print()


def create_ops_instance():
    """Guided setup for the dual-instance (sandboxed + privileged ops) pattern."""
    header("Create Privileged Ops Instance")
    info("This sets up the dual-instance deployment pattern:")
    info("  Main instance  — sandboxed, runs user-facing agents")
    info("  Ops instance   — unsandboxed, single privileged agent for dev/ops")
    info("")
    info("The ops agent can edit code, restart services, and run deploys.")
    info("Only the system owner should have access to it.")
    print()

    # Check if rescue profile already exists
    rescue_yaml = CONFIG_DIR / "agents.rescue.yaml"
    rescue_service = PROJECT_ROOT / "config" / "tars-rescue.service"

    if rescue_yaml.exists():
        warn(f"agents.rescue.yaml already exists at {rescue_yaml}")
        if not ask_yn("Overwrite and reconfigure?", default=False):
            return
    print()

    # Agent name
    agent_name = ask("Agent internal name", "engineer")
    display_name = ask("Display name", agent_name.capitalize() + " Bot")
    description = ask("Description", "Privileged ops agent — unsandboxed, owner-only")
    model = ask_choice("Model", ["sonnet", "opus", "haiku"], default="opus")

    # Bot account
    cfg = load_config()
    discord_accounts = list(
        cfg.get("connectors", {}).get("discord", {}).get("accounts", {}).keys()
    )

    print()
    info("The ops agent needs its own Discord bot account so it has a")
    info("separate identity from your user-facing agents.")
    print()

    if ask_yn("Create a new Discord bot account for this agent?", default=True):
        bot_name = ask("Bot account name", agent_name)
        vault = get_vault()
        if vault:
            token = ask_secret(f"Discord bot token for '{bot_name}'")
            if token:
                info("Validating token...")
                bot_info = validate_discord_token(token)
                if bot_info:
                    ok(f"Bot verified: {bot_info.get('username', '?')}")
                else:
                    if not ask_yn("Validation failed. Store anyway?", default=False):
                        warn("Skipped token — add it later via vault.")
                        token = None
                if token:
                    vault_key = f"discord-{bot_name}"
                    vault.set(vault_key, token)
                    ok(f"Token stored as '{vault_key}'")
                    cfg.setdefault("connectors", {}).setdefault("discord", {}).setdefault("accounts", {})[bot_name] = {"token_key": vault_key}
                    save_config(cfg)
            else:
                warn("No token provided — add it later via vault.")
        else:
            err("Vault unavailable — add the bot token later.")
            bot_name = ask("Bot account name (will configure token later)", agent_name)
    else:
        if discord_accounts:
            info(f"Available accounts: {', '.join(discord_accounts)}")
        bot_name = ask("Existing bot account to use", discord_accounts[0] if discord_accounts else "main")

    # Channel restriction
    print()
    channels = []
    if ask_yn("Restrict to specific channel IDs?", default=False):
        while True:
            ch = ask("Channel ID (empty to stop)")
            if not ch:
                break
            channels.append(ch)

    # Write agents.rescue.yaml
    rescue_agents = {
        "agents": {
            agent_name: {
                "display_name": display_name,
                "description": description,
                "project_dir": f"./agents/{agent_name}",
                "privileged": True,
                "llm": {"provider": "claude_code", "model": model},
                "tools": "all",
                "skills": "all",
                "routing": {
                    "discord": {
                        "account": bot_name,
                        "channels": channels,
                        "categories": [],
                        "guilds": [],
                        "mentions": True,
                    }
                },
            }
        }
    }
    save_yaml(rescue_yaml, rescue_agents)

    # Create agent directory + scaffolding
    agent_dir = PROJECT_ROOT / "agents" / agent_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    claude_md = f"""# {display_name}

## Identity

You are **{display_name}** — the unsandboxed ops and dev agent.

- You run under `tars-rescue.service` — a separate, unsandboxed instance of the engine.
- Your main counterpart runs inside the sandboxed `tars.service`. You handle what it can't: code edits, deploys, service restarts, infra debugging.
- Be surgical. You have full filesystem access. Think before you write.
- Bias toward reversible actions. Prefer git-tracked edits over raw file writes.

## Memory System

Use your MCP tools for memory — do NOT use curl or HTTP calls.

- `memory_search` — keyword/FTS5 search
- `memory_semantic_search` — embedding-based conceptual search
- `memory_store` — save important information
- `memory_forget` — remove a memory by ID
"""
    claude_md_path = agent_dir / "CLAUDE.md"
    if claude_md_path.exists():
        warn("CLAUDE.md already exists — skipping (won't overwrite)")
    else:
        claude_md_path.write_text(claude_md)
        ok(f"Created agents/{agent_name}/CLAUDE.md")

    mcp_path = agent_dir / ".mcp.json"
    if mcp_path.exists():
        warn(".mcp.json already exists — skipping (won't overwrite)")
    else:
        mcp_json = {
            "mcpServers": {
                "tars-tools": {
                    "command": str(PROJECT_ROOT / ".venv" / "bin" / "python3"),
                    "args": ["-m", "src.mcp_server"],
                    "cwd": str(PROJECT_ROOT),
                }
            }
        }
        mcp_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created agents/{agent_name}/.mcp.json")

    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if settings_path.exists():
        warn(".claude/settings.json already exists — skipping (won't overwrite)")
    else:
        settings = {"permissions": {"allow": ["mcp__tars-tools__*"], "deny": []}}
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created agents/{agent_name}/.claude/settings.json")

    # Check if service file exists
    print()
    if rescue_service.exists():
        ok("tars-rescue.service already exists in config/")
    else:
        warn("tars-rescue.service not found in config/")
        info("The service template ships with T.A.R.S core at config/tars-rescue.service")

    # Install instructions
    print()
    header("Next Steps")
    info("1. Customise the CLAUDE.md:  agents/" + agent_name + "/CLAUDE.md")
    info("2. Install the service unit:")
    info("     sudo cp config/tars-rescue.service /etc/systemd/system/")
    info("     sudo systemctl daemon-reload")
    info("     sudo systemctl enable --now tars-rescue.service")
    info("3. The ops agent loads profile 'rescue' — only agents in")
    info("   agents.rescue.yaml are started. Your main agents are unaffected.")
    info("")
    info("See ARCHITECTURE.md → Deployment Patterns for full details.")


# ==========================================================================
# Main menu
# ==========================================================================

MENU_ITEMS = [
    ("1", "LLM Defaults", edit_llm),
    ("2", "Connectors", edit_connectors),
    ("3", "Session", edit_session),
    ("4", "Memory", edit_memory),
    ("5", "Security / HITL", edit_hitl),
    ("6", "Rate Limits", edit_rate_limits),
    ("7", "Compression", edit_compression),
    ("8", "Admin Users", edit_admin_users),
    ("9", "Identity (name, log level)", edit_tars_identity),
]

# These don't take cfg as argument — handled separately
SPECIAL_ITEMS = [
    ("a", "View Agents", view_agents),
    ("c", "Create Agent", create_agent),
    ("p", "Create Privileged Ops Instance", create_ops_instance),
    ("v", "Vault Secrets", manage_vault),
    ("q", "Quit", None),
]


def main():
    os.chdir(PROJECT_ROOT)
    banner()

    config_path = CONFIG_DIR / "config.yaml"
    if not config_path.exists():
        err(f"No config found at {config_path}")
        info("Run setup.py first: uv run python setup.py")
        sys.exit(1)

    while True:
        print(f"\n  {BOLD}Settings{RESET}")
        print()
        for key, label, _ in MENU_ITEMS:
            print(f"    {BOLD}{key}){RESET} {label}")
        print()
        for key, label, _ in SPECIAL_ITEMS:
            print(f"    {BOLD}{key}){RESET} {label}")
        print()

        choice = ask("Choice", "q").lower()

        if choice in ("q", "quit", "exit"):
            print()
            ok("Done. Restart T.A.R.S to apply changes.")
            break

        # Config-based sections (reload each time for freshness)
        for key, _, handler in MENU_ITEMS:
            if choice == key:
                cfg = load_config()
                handler(cfg)
                break
        else:
            # Special items
            for key, _, handler in SPECIAL_ITEMS:
                if choice == key and handler:
                    handler()
                    break
            else:
                if choice not in ("q", "quit", "exit"):
                    err(f"Unknown option: {choice}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Exited.{RESET}\n")
        sys.exit(0)
