#!/usr/bin/env python3
"""T.A.R.S Interactive Setup Wizard.

Guides new users through first-time configuration:
  1. Vault (encrypted secrets)
  2. Discord bot connection
  3. Team (owner profile)
  4. First agent
  5. HITL (human-in-the-loop approval)
  6. Config file generation

Usage: uv run python setup.py
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

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

# --- Formatting ---

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
║          T.A.R.S Setup Wizard        ║
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
        marker = " (default)" if opt == default else ""
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


# --- Discord API ---

def validate_discord_token(token: str) -> dict | None:
    """Validate a Discord bot token by calling the API. Returns bot info or None."""
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


# --- Steps ---

def step_vault(state: dict):
    header("Step 1: Vault Setup")
    info("The vault encrypts your API tokens and secrets at rest.")
    info("You'll set a passphrase that's needed to unlock the vault at startup.")

    vault_path = Path("config/secrets.enc")
    key_file = Path.home() / ".config" / "tars-vault-key"
    vault = FernetVault(str(vault_path))

    if vault_path.exists():
        info("Existing vault found.")
        # Try key file first
        if key_file.exists():
            try:
                passphrase = key_file.read_text().strip()
                vault.unlock(passphrase)
                ok(f"Vault unlocked ({len(vault.list_keys())} secrets)")
                state["vault"] = vault
                state["vault_existed"] = True
                return
            except ValueError:
                warn("Key file passphrase didn't work.")

        for attempt in range(3):
            passphrase = ask_secret("Enter vault passphrase")
            try:
                vault.unlock(passphrase)
                ok(f"Vault unlocked ({len(vault.list_keys())} secrets)")
                if ask_yn("Save passphrase to key file for auto-unlock?"):
                    key_file.parent.mkdir(parents=True, exist_ok=True)
                    key_file.write_text(passphrase + "\n")
                    key_file.chmod(0o600)
                    ok(f"Key file saved to {key_file}")
                state["vault"] = vault
                state["vault_existed"] = True
                return
            except ValueError:
                err("Wrong passphrase.")

        err("Failed to unlock vault after 3 attempts.")
        sys.exit(1)

    else:
        while True:
            passphrase = ask_secret("Create a vault passphrase")
            if len(passphrase) < 4:
                err("Passphrase must be at least 4 characters.")
                continue
            confirm = ask_secret("Confirm passphrase")
            if passphrase != confirm:
                err("Passphrases don't match.")
                continue
            break

        vault.unlock(passphrase)
        # Persist empty vault to create the file
        vault.set("_setup", "true")
        vault.delete("_setup")
        ok("Vault created")

        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(passphrase + "\n")
        key_file.chmod(0o600)
        ok(f"Key file saved to {key_file}")

        state["vault"] = vault
        state["vault_existed"] = False


def step_discord(state: dict):
    header("Step 2: Discord Bot")
    info("T.A.R.S connects to Discord via a bot account.")
    info("Create one at: https://discord.com/developers/applications")
    info("Required intents: Message Content, Server Members, Guild Messages")
    print()

    if not ask_yn("Do you have a Discord bot token ready?"):
        warn("Skipped — you'll need to add the token later via vault-manage.py")
        state["discord_skip"] = True
        return

    vault: FernetVault = state["vault"]

    # Token
    while True:
        token = ask_secret("Bot token")
        if not token:
            warn("Skipped Discord setup.")
            state["discord_skip"] = True
            return

        info("Validating token...")
        bot_info = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}#{bot_info.get('discriminator', '0')}")
            break
        else:
            if ask_yn("Token validation failed. Use it anyway?", default=False):
                break
            continue

    vault.set("discord-token", token)
    ok("Token stored in vault")

    # Guild ID
    guild_id = ask("Server (guild) ID")
    state["guild_id"] = guild_id

    # User ID
    user_id = ask("Your Discord user ID (for admin access)")
    state["owner_discord_id"] = user_id
    state["discord_skip"] = False

    # Bot account name
    bot_name = ask("Internal name for this bot account", "main")
    state["bot_name"] = bot_name
    state["bot_token_key"] = "discord-token"


def step_team(state: dict):
    header("Step 3: Team — Owner Profile")
    info("Set up your profile so the system knows who you are.")
    print()

    name = ask("Your name")
    role = ask("Your role", "Founder")
    timezone = ask("Timezone", "UTC")
    context = ask("Short context (what you do)")

    discord_id = state.get("owner_discord_id", "")
    if not discord_id:
        discord_id = ask("Discord user ID")

    state["owner"] = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "human",
        "access": "owner",
        "role": role,
        "responsibilities": ["Everything"],
        "context": context,
        "contact": {"discord": discord_id},
        "preferences": {"timezone": timezone},
    }
    state["team_members"] = [state["owner"]]
    ok(f"Owner profile: {name} ({role})")


def step_agent(state: dict):
    header("Step 4: First Agent")
    info("Configure your primary AI agent.")
    print()

    agent_name = ask("Agent internal name (lowercase, no spaces)", "main")
    display_name = ask("Display name (shown in Discord)", agent_name.upper())
    description = ask("One-line description", "Primary agent")
    model = ask_choice("LLM model", ["sonnet", "opus"], default="sonnet")
    mentions_only = ask_yn("Only respond when @mentioned?", default=True)

    state["agent"] = {
        "name": agent_name,
        "display_name": display_name,
        "description": description,
        "model": model,
        "mentions": mentions_only,
    }

    # Agent personality
    print()
    info("Optionally set a personality for the agent's CLAUDE.md.")
    personality = ask("Personality (e.g., 'concise and direct', 'friendly and detailed')", "concise and direct")
    state["agent"]["personality"] = personality

    ok(f"Agent: {display_name} ({model})")


def step_hitl(state: dict):
    header("Step 5: Human-in-the-Loop Approval")
    info("Some tools require human approval before executing.")
    info("Approvals are sent to a Discord channel as reaction prompts.")
    print()

    if state.get("discord_skip"):
        warn("Discord not configured — using defaults for HITL.")
        state["hitl"] = {
            "channel": "",
            "approvers": [],
            "gated_tools": ["send_email", "install_mcp"],
        }
        return

    channel_id = ask("Discord channel ID for approvals (ops/alerts channel)")
    approvers = [state.get("owner_discord_id", "")]
    approvers = [a for a in approvers if a]

    more = ask_yn("Add more approvers?", default=False)
    while more:
        uid = ask("Discord user ID")
        if uid:
            approvers.append(uid)
        more = ask_yn("Add another?", default=False)

    print()
    info("Default gated tools: send_email, install_mcp")
    gated = ["send_email", "install_mcp"]
    if ask_yn("Add more gated tools?", default=False):
        while True:
            tool = ask("Tool name (empty to stop)")
            if not tool:
                break
            gated.append(tool)

    state["hitl"] = {
        "channel": channel_id,
        "approvers": approvers,
        "gated_tools": gated,
    }
    ok(f"HITL: {len(approvers)} approver(s), {len(gated)} gated tool(s)")


def step_compression(state: dict):
    header("Step 6: Context Compression (optional)")
    info("T.A.R.S can compress verbose context files (codex docs, skill prompts)")
    info("to reduce input tokens per agent message. Rule-based, no LLM calls.")
    info("CLAUDE.md files are excluded — they're carefully tuned prompts.")
    print()

    compression = {"enabled": False, "level": "standard"}

    if ask_yn("Enable context compression?", default=False):
        compression["enabled"] = True
        compression["level"] = ask_choice(
            "Compression level",
            ["lite", "standard"],
            default="standard",
        )
        ok(f"Context compression enabled ({compression['level']})")

        print()
        info("Memory recall compression strips filler from memories before")
        info("injecting them into agent context. Same rules, applied at runtime.")
        print()
        if ask_yn("Enable memory recall compression?", default=False):
            compression["memory_recall"] = True
            ok("Memory recall compression enabled")
        else:
            compression["memory_recall"] = False
    else:
        info("Skipped — can be enabled later in config.yaml")

    state["compression"] = compression


def step_generate(state: dict):
    header("Step 7: Generating Config Files")

    agent = state["agent"]
    owner = state["owner"]
    hitl = state["hitl"]
    bot_name = state.get("bot_name", "main")
    guild_id = state.get("guild_id", "YOUR_GUILD_ID")
    owner_discord = owner["contact"]["discord"] or "YOUR_DISCORD_USER_ID"
    project_root = Path.cwd().resolve()

    # --- config.yaml ---
    config = {
        "tars": {"name": "T.A.R.S", "log_level": "info", "data_dir": "./data"},
        "connectors": {
            "discord": {
                "enabled": True,
                "accounts": {
                    bot_name: {"token_key": state.get("bot_token_key", "discord-token")},
                },
            },
            "telegram": {"enabled": False},
            "http": {"enabled": False, "port": 8080},
        },
        "defaults": {
            "llm": {"provider": "claude_code", "model": agent["model"], "max_tokens": 4096},
            "session": {"max_history": 50, "summarize_after": 30},
            "memory": {"backend": "sqlite", "semantic_search": False, "decay_enabled": False, "max_results": 10},
        },
        "security": {
            "hitl": {
                "connector": "discord",
                "channel": hitl["channel"],
                "approvers": hitl["approvers"],
                "timeout": 1800,
                "fail_mode": "closed",
                "poll_interval": 3,
                "gated_tools": hitl["gated_tools"],
            },
            "rate_limits": {"mode": "log", "defaults": {"max_per_hour": 100}},
            "compression": state.get("compression", {"enabled": False, "level": "standard"}),
        },
        "admin_users": {"discord": [owner_discord]},
    }
    _write_yaml("config/config.yaml", config, state)

    # --- agents.yaml ---
    # disallow_builtins blocks Claude Code's built-in file/shell tools for
    # non-privileged agents. They still have full MCP tool access, but can't
    # edit code, run shell commands, or write arbitrary files. Privileged
    # ops/dev agents should remove this block.
    agents = {
        "agents": {
            agent["name"]: {
                "display_name": agent["display_name"],
                "description": agent["description"],
                "project_dir": f"./agents/{agent['name']}",
                "llm": {"provider": "claude_code", "model": agent["model"]},
                "tools": "all",
                "skills": "all",
                "disallow_builtins": ["Edit", "Write", "Bash", "MultiEdit"],
                "routing": {
                    "discord": {
                        "account": bot_name,
                        "channels": [],
                        "mentions": agent["mentions"],
                    }
                },
            }
        }
    }
    _write_yaml("config/agents.yaml", agents, state)

    # --- team.json ---
    team = {"humans": state["team_members"], "agents": []}
    _write_json("config/team.json", team, state)

    # --- Agent directory ---
    agent_dir = Path(f"agents/{agent['name']}")
    agent_dir.mkdir(parents=True, exist_ok=True)

    # CLAUDE.md
    claude_md = f"""# {agent['display_name']}

## Identity

You are **{agent['display_name']}**. {agent['description']}.

## Guidelines

- Be {agent['personality']}
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
    _write_file(agent_dir / "CLAUDE.md", claude_md, state)

    # .mcp.json
    mcp_json = {
        "mcpServers": {
            "tars-tools": {
                "command": str(project_root / ".venv" / "bin" / "python3"),
                "args": ["-m", "src.mcp_server"],
                "cwd": str(project_root),
            }
        }
    }
    _write_json(str(agent_dir / ".mcp.json"), mcp_json, state)

    # .claude/settings.json
    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "permissions": {
            "allow": [
                f"mcp__tars-tools__*",
                f"Bash(uv run python:*)",
            ],
            "deny": [],
        }
    }
    _write_json(str(claude_dir / "settings.json"), settings, state)

    ok(f"Agent directory created: {agent_dir}")


def step_ops_instance(state: dict):
    header("Step 8: Privileged Ops Instance (optional)")
    info("T.A.R.S supports a dual-instance deployment pattern:")
    info("  Main instance  — sandboxed, runs your user-facing agents")
    info("  Ops instance   — unsandboxed, single privileged agent for dev/ops")
    info("")
    info("The ops agent can edit code, restart services, and run deploys.")
    info("Only the system owner should have access to it.")
    info("See ARCHITECTURE.md → Deployment Patterns for full details.")
    print()

    if not ask_yn("Set up a privileged ops instance?", default=False):
        info("Skipped — you can set this up later via scripts/settings.py")
        return

    vault: FernetVault = state["vault"]

    agent_name = ask("Ops agent internal name", "engineer")
    display_name = ask("Display name", agent_name.capitalize() + " Bot")
    description = ask("Description", "Privileged ops agent — unsandboxed, owner-only")
    model = ask_choice("Model", ["sonnet", "opus"], default="opus")

    # Bot account
    print()
    info("The ops agent needs its own Discord bot account.")
    bot_name = ask("Bot account name", agent_name)
    token = ask_secret(f"Discord bot token for '{bot_name}' (empty to skip)")

    if token:
        info("Validating token...")
        bot_info = validate_discord_token(token)
        if bot_info:
            ok(f"Bot verified: {bot_info.get('username', '?')}")
        else:
            if not ask_yn("Validation failed. Store anyway?", default=False):
                token = None

    if token:
        vault_key = f"discord-{bot_name}"
        vault.set(vault_key, token)
        ok(f"Token stored as '{vault_key}'")
        state.setdefault("extra_bots", {})[bot_name] = vault_key
    else:
        warn("No token — add it later via vault-manage.py")

    # Channel restriction
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
    rescue_path = Path("config/agents.rescue.yaml")
    rescue_path.write_text(yaml.dump(rescue_agents, default_flow_style=False, sort_keys=False))
    ok("Created config/agents.rescue.yaml")

    # Add bot account to main config.yaml if token was stored
    if token:
        config_path = Path("config/config.yaml")
        if config_path.exists():
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            cfg.setdefault("connectors", {}).setdefault("discord", {}).setdefault("accounts", {})[bot_name] = {"token_key": f"discord-{bot_name}"}
            config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
            ok(f"Added bot '{bot_name}' to config.yaml")

    # Create agent directory
    project_root = Path.cwd().resolve()
    agent_dir = Path(f"agents/{agent_name}")
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
    if not claude_md_path.exists():
        claude_md_path.write_text(claude_md)
        ok(f"Created agents/{agent_name}/CLAUDE.md")

    mcp_json = {
        "mcpServers": {
            "tars-tools": {
                "command": str(project_root / ".venv" / "bin" / "python3"),
                "args": ["-m", "src.mcp_server"],
                "cwd": str(project_root),
            }
        }
    }
    mcp_path = agent_dir / ".mcp.json"
    if not mcp_path.exists():
        mcp_path.write_text(json.dumps(mcp_json, indent=2) + "\n")
        ok(f"Created agents/{agent_name}/.mcp.json")

    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    if not settings_path.exists():
        settings = {"permissions": {"allow": ["mcp__tars-tools__*"], "deny": []}}
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        ok(f"Created agents/{agent_name}/.claude/settings.json")

    print()
    ok(f"Ops instance configured — agent: {display_name}")
    info("To start it:")
    info("  sudo cp config/tars-rescue.service /etc/systemd/system/")
    info("  sudo systemctl daemon-reload")
    info("  sudo systemctl enable --now tars-rescue.service")


def step_extras(state: dict):
    header("Step 9: Additional Setup (optional)")

    while True:
        print()
        print("  What would you like to add?")
        print("    1) Another team member")
        print("    2) Another agent")
        print("    3) Another Discord bot")
        print("    4) Done — finish setup")
        print()
        choice = ask("Choice", "4")

        if choice in ("4", "done", "d", ""):
            break
        elif choice in ("1", "team"):
            _add_team_member(state)
        elif choice in ("2", "agent"):
            _add_agent(state)
        elif choice in ("3", "bot"):
            _add_bot(state)


def _add_team_member(state: dict):
    name = ask("Name")
    role = ask("Role")
    discord_id = ask("Discord user ID")
    access = ask_choice("Access level", ["owner", "admin", "staff", "viewer"], default="staff")

    member = {
        "id": name.lower().replace(" ", "-"),
        "name": name,
        "type": "human",
        "access": access,
        "role": role,
        "responsibilities": [],
        "context": "",
        "contact": {"discord": discord_id},
        "preferences": {"timezone": "UTC"},
    }
    state["team_members"].append(member)

    # Update team.json
    team = {"humans": state["team_members"], "agents": []}
    Path("config/team.json").write_text(json.dumps(team, indent=2) + "\n")
    ok(f"Added {name} ({role}, {access})")


def _add_agent(state: dict):
    agent_name = ask("Agent internal name")
    display_name = ask("Display name", agent_name.upper())
    description = ask("Description")
    model = ask_choice("Model", ["sonnet", "opus"], default="sonnet")
    bot_account = ask("Bot account name (from existing bots)", state.get("bot_name", "main"))

    # Load current agents.yaml and add
    agents_path = Path("config/agents.yaml")
    with open(agents_path) as f:
        agents_cfg = yaml.safe_load(f) or {}

    agents_cfg.setdefault("agents", {})[agent_name] = {
        "display_name": display_name,
        "description": description,
        "project_dir": f"./agents/{agent_name}",
        "llm": {"provider": "claude_code", "model": model},
        "tools": "all",
        "skills": "all",
        "disallow_builtins": ["Edit", "Write", "Bash", "MultiEdit"],
        "routing": {
            "discord": {
                "account": bot_account,
                "channels": [],
                "mentions": True,
            }
        },
    }

    agents_path.write_text(yaml.dump(agents_cfg, default_flow_style=False, sort_keys=False))

    # Create agent directory
    project_root = Path.cwd().resolve()
    agent_dir = Path(f"agents/{agent_name}")
    agent_dir.mkdir(parents=True, exist_ok=True)

    claude_md = f"# {display_name}\n\n## Identity\n\nYou are **{display_name}**. {description}.\n"
    (agent_dir / "CLAUDE.md").write_text(claude_md)

    mcp_json = {
        "mcpServers": {
            "tars-tools": {
                "command": str(project_root / ".venv" / "bin" / "python3"),
                "args": ["-m", "src.mcp_server"],
                "cwd": str(project_root),
            }
        }
    }
    (agent_dir / ".mcp.json").write_text(json.dumps(mcp_json, indent=2) + "\n")

    claude_dir = agent_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = {"permissions": {"allow": ["mcp__tars-tools__*"], "deny": []}}
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    ok(f"Agent '{display_name}' added")


def _add_bot(state: dict):
    vault: FernetVault = state["vault"]
    bot_name = ask("Bot account name (internal)")
    token = ask_secret("Bot token")

    vault_key = f"discord-{bot_name}"
    vault.set(vault_key, token)
    ok(f"Token stored as '{vault_key}'")

    # Add to config.yaml
    config_path = Path("config/config.yaml")
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    cfg["connectors"]["discord"]["accounts"][bot_name] = {"token_key": vault_key}
    config_path.write_text(yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    ok(f"Bot '{bot_name}' added to config")


def step_browser(state: dict):
    header("Step 10: Browser Tool (optional)")
    info("The browse_url tool uses a headless Chromium browser via Playwright")
    info("to fetch JavaScript-rendered pages. The Python package is already")
    info("installed; Chromium itself is a separate ~170MB download.")
    print()

    if not ask_yn("Install Chromium for the browse_url tool now?", default=True):
        warn("Skipped. browse_url will return an error until you run:")
        info("    uv run playwright install chromium")
        return

    # Idempotent: playwright install chromium is a fast no-op if already present.
    cmd = ["uv", "run", "playwright", "install", "chromium"]
    try:
        print()
        info("Running: " + " ".join(cmd))
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            ok("Chromium installed — browse_url tool is ready.")
        else:
            err("Chromium install failed (exit " + str(result.returncode) + ").")
            info("Run manually later: uv run playwright install chromium")
    except FileNotFoundError:
        err("'uv' not found on PATH.")
        info("Run manually later: uv run playwright install chromium")


def step_summary(state: dict):
    header("Setup Complete")

    agent = state["agent"]
    owner = state["owner"]
    vault: FernetVault = state["vault"]

    print(f"  {BOLD}Vault:{RESET}      {len(vault.list_keys())} secret(s)")
    print(f"  {BOLD}Team:{RESET}       {len(state['team_members'])} member(s)")
    print(f"  {BOLD}Agent:{RESET}      {agent['display_name']} ({agent['model']})")

    if state.get("discord_skip"):
        print(f"  {BOLD}Discord:{RESET}    {YELLOW}not configured{RESET} — add token via vault-manage.py")
    else:
        print(f"  {BOLD}Discord:{RESET}    connected (guild: {state.get('guild_id', '?')})")

    print(f"""
  {BOLD}Generated files:{RESET}
    config/config.yaml
    config/agents.yaml
    config/team.json
    agents/{agent['name']}/CLAUDE.md
    agents/{agent['name']}/.mcp.json

  {BOLD}Next steps:{RESET}
    1. Review and customise agents/{agent['name']}/CLAUDE.md
    2. Start T.A.R.S:  {CYAN}uv run python -m src.main{RESET}
    3. Manage secrets:  {CYAN}uv run python vault-manage.py{RESET}
    4. Add team members in conversation: @{agent['display_name']} add Bob as admin

  {DIM}Config files are gitignored — your setup stays private.{RESET}
""")


# --- File writers ---

def _write_yaml(path: str, data: dict, state: dict):
    p = Path(path)
    if p.exists() and state.get("vault_existed"):
        if not ask_yn(f"  {path} exists. Overwrite?", default=False):
            warn(f"Skipped {path}")
            return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    ok(f"Created {path}")


def _write_json(path: str, data: dict, state: dict):
    p = Path(path)
    if p.exists() and state.get("vault_existed"):
        if not ask_yn(f"  {path} exists. Overwrite?", default=False):
            warn(f"Skipped {path}")
            return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")
    ok(f"Created {path}")


def _write_file(path: Path, content: str, state: dict):
    if path.exists() and state.get("vault_existed"):
        if not ask_yn(f"  {path} exists. Overwrite?", default=False):
            warn(f"Skipped {path}")
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    ok(f"Created {path}")


# --- Main ---

def main():
    banner()

    # Pre-checks
    if not shutil.which("claude"):
        warn("Claude Code CLI not found — install it: npm install -g @anthropic-ai/claude-code")

    if Path("config/config.yaml").exists():
        print(f"  {YELLOW}Existing setup detected.{RESET} This wizard can update your configuration.")
        if not ask_yn("Continue?"):
            print("  Exited.")
            return

    state: dict = {}

    steps = [
        step_vault,
        step_discord,
        step_team,
        step_agent,
        step_hitl,
        step_compression,
        step_generate,
        step_ops_instance,
        step_extras,
        step_browser,
        step_summary,
    ]

    for step in steps:
        step(state)

    print(f"  {GREEN}{BOLD}T.A.R.S is ready.{RESET}\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {YELLOW}Setup interrupted. Files written so far are preserved.{RESET}\n")
        sys.exit(0)
