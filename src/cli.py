"""T.A.R.S CLI — migrate, vault, run, and manage.

Usage:
    python -m src.cli migrate --from /path/to/tars
    python -m src.cli vault init
    python -m src.cli vault migrate --from /path/to/secrets.age
    python -m src.cli run
    python -m src.cli healthcheck
"""

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("tars-cli")

SCRIPT_DIR = Path(__file__).parent.parent  # project root


# ============================================================
# MIGRATE — Import TARS config into T.A.R.S
# ============================================================

def cmd_migrate(args):
    """Migrate config from an existing TARS installation."""
    tars_dir = Path(args.source)
    if not tars_dir.exists():
        logger.error(f"TARS directory not found: {tars_dir}")
        sys.exit(1)

    tc_dir = SCRIPT_DIR
    dry_run = args.dry_run

    logger.info(f"Migrating from TARS at {tars_dir}")
    if dry_run:
        logger.info("(dry run — no files will be written)")
    logger.info("")

    steps = [
        ("Team config", migrate_team),
        ("Agent config", migrate_agents),
        ("Skills", migrate_skills),
        ("MCP config", migrate_mcp),
        ("Discord routing", migrate_routing),
        ("Agent workspaces", migrate_workspaces),
    ]

    results = []
    for name, func in steps:
        try:
            result = func(tars_dir, tc_dir, dry_run)
            results.append((name, result))
            if result:
                logger.info(f"  [ok] {name}: {result}")
            else:
                logger.info(f"  [--] {name}: nothing to migrate")
        except Exception as e:
            logger.error(f"  [!!] {name}: {e}")
            results.append((name, f"ERROR: {e}"))

    # Print summary
    logger.info("")
    logger.info("=" * 50)
    logger.info("Migration Summary")
    logger.info("=" * 50)
    for name, result in results:
        logger.info(f"  {name}: {result or 'skipped'}")

    if not dry_run:
        logger.info("")
        logger.info("Next steps:")
        logger.info("  1. Review config/agents.yaml and config/config.yaml")
        logger.info("  2. Add Discord bot token(s) to .env or vault")
        logger.info("  3. Test: uv run python -m src.main")
        logger.info("  4. Test on separate Discord channels alongside TARS")
        logger.info("  5. When ready: stop OpenClaw, start T.A.R.S")


def migrate_team(tars_dir: Path, tc_dir: Path, dry_run: bool) -> str:
    """Copy team.json as-is (same format)."""
    src = tars_dir / "config" / "team.json"
    dst = tc_dir / "config" / "team.json"

    if not src.exists():
        return ""

    with open(src) as f:
        team = json.load(f)

    humans = len(team.get("humans", []))
    agents = len(team.get("agents", []))

    if not dry_run:
        shutil.copy2(src, dst)

    return f"{humans} humans, {agents} agents"


def migrate_agents(tars_dir: Path, tc_dir: Path, dry_run: bool) -> str:
    """Convert TARS team.json agents + any agents.json into agents.yaml."""
    # TARS stores agents in team.json
    team_file = tars_dir / "config" / "team.json"
    agents_yaml_path = tc_dir / "config" / "agents.yaml"

    tars_agents = []

    # Read from team.json
    if team_file.exists():
        with open(team_file) as f:
            team = json.load(f)
        tars_agents.extend(team.get("agents", []))

    # Read from agents.json if it exists
    agents_json = tars_dir / "config" / "agents.json"
    if agents_json.exists():
        with open(agents_json) as f:
            data = json.load(f)
        if isinstance(data, list):
            tars_agents.extend(data)
        elif isinstance(data, dict) and "agents" in data:
            tars_agents.extend(data["agents"])

    # Memory is now inline — no external agents.json to check

    if not tars_agents:
        return ""

    # Convert to T.A.R.S agents.yaml format
    agents_out = {}
    for agent in tars_agents:
        agent_id = agent.get("id", "unknown")
        agents_out[agent_id] = {
            "display_name": agent.get("name", agent_id),
            "description": agent.get("domain", agent.get("role", "")),
            "system_prompt_file": f"./agents/{agent_id}/CLAUDE.md",
            "project_dir": f"./agents/{agent_id}/workspace",
            "llm": {
                "provider": "claude_code",
                "model": _map_model(agent.get("model")),
            },
            "tools": _map_capabilities(agent.get("capabilities", [])),
            "routing": {
                "discord": {
                    "channels": [],
                    "mentions": True,
                },
            },
        }

    if not dry_run:
        with open(agents_yaml_path, "w") as f:
            yaml.dump({"agents": agents_out}, f, default_flow_style=False, sort_keys=False)

    return f"{len(agents_out)} agents"


def migrate_skills(tars_dir: Path, tc_dir: Path, dry_run: bool) -> str:
    """Convert TARS skills/registry.json + skill .md files to skills/*.yaml."""
    registry_file = tars_dir / "skills" / "registry.json"
    skills_dir = tc_dir / "skills"

    if not registry_file.exists():
        return ""

    with open(registry_file) as f:
        registry = json.load(f)

    skills = registry.get("skills", [])
    converted = 0

    for skill in skills:
        skill_id = skill.get("id", "")
        if not skill_id:
            continue

        # Check for corresponding .md file
        md_file = tars_dir / "skills" / f"{skill_id}.md"
        prompt = ""
        if md_file.exists():
            with open(md_file) as f:
                prompt = f.read()

        # Build YAML skill
        skill_yaml = {
            "name": skill_id,
            "description": skill.get("description", ""),
            "prompt": prompt or f"Run the {skill.get('name', skill_id)} skill.",
            "tools": _skill_deps_to_tools(skill),
        }

        # Add category as a comment via description
        category = skill.get("category", "")
        if category:
            skill_yaml["description"] = f"[{category}] {skill_yaml['description']}"

        yaml_path = skills_dir / f"{skill_id}.yaml"
        if not dry_run:
            skills_dir.mkdir(parents=True, exist_ok=True)
            with open(yaml_path, "w") as f:
                yaml.dump(skill_yaml, f, default_flow_style=False, sort_keys=False)

        converted += 1

    return f"{converted} skills"


def migrate_mcp(tars_dir: Path, tc_dir: Path, dry_run: bool) -> str:
    """Create MCP config from known TARS services."""
    mcp_yaml_path = tc_dir / "config" / "mcp.yaml"

    # Check for MCP-related config in TARS
    mcp_json = tars_dir / "config" / "mcp-servers.json"
    servers = {}

    if mcp_json.exists():
        with open(mcp_json) as f:
            data = json.load(f)
        for name, cfg in data.items():
            servers[name] = {
                "url": cfg.get("url", ""),
                "transport": cfg.get("transport", "sse"),
            }

    # Also check docker-compose for known MCP services
    compose_file = tars_dir / "docker-compose.yml"
    if compose_file.exists():
        with open(compose_file) as f:
            compose = yaml.safe_load(f) or {}
        services = compose.get("services", {})
        if "mcp-gateway" in services:
            svc = services["mcp-gateway"]
            ports = svc.get("ports", [])
            port = "12008"
            for p in ports:
                if isinstance(p, str) and ":" in p:
                    port = p.split(":")[0]
            servers.setdefault("google-workspace", {
                "url": f"http://localhost:{port}",
                "transport": "sse",
            })

    if not servers:
        return ""

    if not dry_run:
        with open(mcp_yaml_path, "w") as f:
            yaml.dump({"servers": servers}, f, default_flow_style=False, sort_keys=False)

    return f"{len(servers)} MCP servers"


def migrate_routing(tars_dir: Path, tc_dir: Path, dry_run: bool) -> str:
    """Extract Discord routing from TARS config."""
    # TARS routing is typically in OpenClaw config or agent config
    # We look for channel mappings
    oc_dir = Path.home() / ".openclaw"
    if not oc_dir.exists():
        return "no OpenClaw config found (set routing manually in agents.yaml)"

    # Check for openclaw agent routing config
    oc_config = oc_dir / "config.json"
    if not oc_config.exists():
        oc_config = oc_dir / "config.yaml"

    if oc_config.exists():
        return f"found OpenClaw config at {oc_config} — review and set routing in agents.yaml"

    return "set routing manually in agents.yaml"


def migrate_workspaces(tars_dir: Path, tc_dir: Path, dry_run: bool) -> str:
    """Copy agent workspace files (CLAUDE.md, templates) to agents/ dirs."""
    agents_dir = tc_dir / "agents"
    created = 0

    # Check for TARS agent templates
    template_dir = tars_dir / "templates"
    roles_dir = template_dir / "roles" if template_dir.exists() else None

    # Get list of agents we're migrating
    team_file = tars_dir / "config" / "team.json"
    if not team_file.exists():
        return ""

    with open(team_file) as f:
        team = json.load(f)

    for agent in team.get("agents", []):
        agent_id = agent.get("id", "")
        if not agent_id:
            continue

        agent_dir = agents_dir / agent_id
        workspace_dir = agent_dir / "workspace"

        if not dry_run:
            agent_dir.mkdir(parents=True, exist_ok=True)
            workspace_dir.mkdir(parents=True, exist_ok=True)

        # Look for existing CLAUDE.md or identity files
        soul_sources = [
            tars_dir / "agents" / agent_id / "CLAUDE.md",
            tars_dir / "agents" / agent_id / "workspace" / "CLAUDE.md",
            tars_dir / "templates" / "AGENTS.md",
        ]

        soul_found = False
        for src in soul_sources:
            if src.exists():
                if not dry_run:
                    shutil.copy2(src, agent_dir / "CLAUDE.md")
                soul_found = True
                break

        # Generate a starter CLAUDE.md if none found
        if not soul_found and not dry_run:
            soul_content = _generate_soul(agent)
            with open(agent_dir / "CLAUDE.md", "w") as f:
                f.write(soul_content)

        created += 1

    return f"{created} agent workspaces"


# ============================================================
# VAULT commands
# ============================================================

def cmd_vault(args):
    """Vault management commands."""
    if args.vault_cmd == "init":
        vault_init(args)
    elif args.vault_cmd == "migrate":
        vault_migrate(args)
    else:
        logger.error(f"Unknown vault command: {args.vault_cmd}")


def vault_init(args):
    """Create a new encrypted vault from .env secrets."""
    env_file = SCRIPT_DIR / ".env"
    vault_file = SCRIPT_DIR / "config" / "secrets.enc"

    if vault_file.exists():
        logger.error(f"Vault already exists at {vault_file}")
        logger.info("To re-create, delete it first: rm config/secrets.enc")
        sys.exit(1)

    # Read secrets from .env
    secrets = {}
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    if value:
                        secrets[key.strip()] = value.strip()

    if not secrets:
        logger.info("No secrets found in .env — creating empty vault")

    # Get passphrase
    import getpass
    passphrase = getpass.getpass("  Vault passphrase: ")
    confirm = getpass.getpass("  Confirm passphrase: ")
    if passphrase != confirm:
        logger.error("Passphrases don't match")
        sys.exit(1)

    # Encrypt
    from cryptography.fernet import Fernet
    import hashlib
    import base64

    # Derive key from passphrase
    key = base64.urlsafe_b64encode(hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode(), b"tarsclaw-vault-salt", 100_000, dklen=32
    ))
    fernet = Fernet(key)

    payload = json.dumps(secrets).encode()
    encrypted = fernet.encrypt(payload)

    vault_file.parent.mkdir(parents=True, exist_ok=True)
    with open(vault_file, "wb") as f:
        f.write(encrypted)
    vault_file.chmod(0o600)

    logger.info(f"  [ok] Vault created: {vault_file} ({len(secrets)} secrets)")
    logger.info("  Passphrase is NOT stored anywhere. Don't lose it.")


def vault_migrate(args):
    """Migrate secrets from TARS age vault to T.A.R.S Fernet vault."""
    logger.info("Age vault migration not yet implemented.")
    logger.info("Manual steps:")
    logger.info("  1. Decrypt age vault: age -d ~/.secrets-vault/secrets.age > /tmp/secrets.txt")
    logger.info("  2. Add each key=value to .env")
    logger.info("  3. Run: python -m src.cli vault init")
    logger.info("  4. Delete /tmp/secrets.txt")


# ============================================================
# RUN
# ============================================================

def cmd_run(args):
    """Run T.A.R.S."""
    import asyncio
    from src.main import main
    asyncio.run(main())


# ============================================================
# HEALTHCHECK
# ============================================================

def cmd_healthcheck(args):
    """Check that all configured services are reachable."""
    import urllib.request

    config_file = SCRIPT_DIR / "config" / "config.yaml"
    if not config_file.exists():
        logger.error("No config found. Run setup.sh first.")
        sys.exit(1)

    with open(config_file) as f:
        config = yaml.safe_load(f)

    checks = [
        ("T.A.R.S config", lambda: config is not None),
    ]

    # Check inline memory backend
    mem_db = Path(SCRIPT_DIR / "data" / "memory.db")
    if mem_db.exists():
        size_mb = mem_db.stat().st_size / (1024 * 1024)
        logger.info(f"  [ok] memory (SQLite): {size_mb:.1f}MB ({mem_db})")
    else:
        logger.info(f"  [--] memory (SQLite): not found at {mem_db}")

    # Check embedding model
    model_dir = Path(SCRIPT_DIR / "data" / "models" / "bge-small-en-v1.5")
    if model_dir.exists():
        logger.info(f"  [ok] embedding model: {model_dir}")
    else:
        logger.info(f"  [--] embedding model: not found (will download on first use)")

    # Check vault
    vault_file = SCRIPT_DIR / "config" / "secrets.enc"
    if vault_file.exists():
        logger.info(f"  [ok] Vault: {vault_file} ({vault_file.stat().st_size} bytes)")
    else:
        logger.info("  [--] Vault: not created (using .env)")

    # Check .env
    env_file = SCRIPT_DIR / ".env"
    if env_file.exists():
        with open(env_file) as f:
            keys = [l.split("=")[0] for l in f if l.strip() and not l.startswith("#") and "=" in l]
        logger.info(f"  [ok] .env: {len(keys)} keys ({', '.join(keys)})")
    else:
        logger.info("  [--] .env: not found")


# ============================================================
# HELPERS
# ============================================================

def _map_model(model: str | None) -> str:
    """Map TARS model config to T.A.R.S model name."""
    if not model:
        return "sonnet"
    model_lower = model.lower()
    if "opus" in model_lower:
        return "opus"
    if "haiku" in model_lower:
        return "haiku"
    return "sonnet"


def _map_capabilities(caps: list[str]) -> list[str]:
    """Map TARS capabilities to T.A.R.S tool names."""
    mapping = {
        "web search": ["web_search"],
        "memory": ["memory_store", "memory_search"],
        "exec": ["run_command"],
        "browser": ["http_request"],
        "sub-agents": ["ask_agent", "send_to_agent"],
        "cron": [],  # handled by scheduler
    }
    tools = []
    for cap in caps:
        tools.extend(mapping.get(cap, []))
    # Always include send_message
    if "send_message" not in tools:
        tools.append("send_message")
    return tools


def _skill_deps_to_tools(skill: dict) -> list[str]:
    """Convert TARS skill dependencies to tool names."""
    tools = []
    deps = skill.get("dependencies", {})

    if "TAVILY_API_KEY" in deps.get("api_keys", []):
        tools.append("web_search")
    if "SERPAPI_KEY" in deps.get("api_keys", []):
        tools.append("web_search")

    integrations = deps.get("integrations", [])
    if "google-calendar" in integrations:
        tools.append("google_calendar")

    # Default tools
    if not tools:
        tools = ["memory_search"]

    return tools


def _generate_soul(agent: dict) -> str:
    """Generate a starter CLAUDE.md for an agent."""
    name = agent.get("name", agent.get("id", "Agent"))
    role = agent.get("role", "assistant")
    domain = agent.get("domain", "")
    caps = agent.get("capabilities", [])

    return f"""# {name}

## Identity

You are **{name}**, a {role}.

## Domain

{domain or 'General-purpose assistant.'}

## Capabilities

{chr(10).join(f'- {c}' for c in caps) if caps else '- General conversation and task completion'}

## Guidelines

- Be concise and direct
- Ask for clarification when needed
- Use your tools effectively
- Remember context from previous conversations
"""


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="tars",
        description="T.A.R.S — Lightweight Agent System",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # migrate
    p_migrate = subparsers.add_parser("migrate", help="Migrate from TARS")
    p_migrate.add_argument("--from", dest="source", required=True, help="Path to TARS installation")
    p_migrate.add_argument("--dry-run", action="store_true", help="Show what would be done")
    p_migrate.set_defaults(func=cmd_migrate)

    # vault
    p_vault = subparsers.add_parser("vault", help="Vault management")
    vault_sub = p_vault.add_subparsers(dest="vault_cmd", required=True)
    vault_sub.add_parser("init", help="Create vault from .env")
    p_vault_migrate = vault_sub.add_parser("migrate", help="Migrate from age vault")
    p_vault_migrate.add_argument("--from", dest="source", help="Path to secrets.age")
    p_vault.set_defaults(func=cmd_vault)

    # run
    p_run = subparsers.add_parser("run", help="Run T.A.R.S")
    p_run.set_defaults(func=cmd_run)

    # healthcheck
    p_health = subparsers.add_parser("healthcheck", help="Check service health")
    p_health.set_defaults(func=cmd_healthcheck)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
