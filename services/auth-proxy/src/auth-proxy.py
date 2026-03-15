#!/usr/bin/env python3
"""
Auth Proxy — Reverse proxy that injects API credentials from age-encrypted vault.

Binds to 172.17.0.1:9100 (reachable from Docker containers + host only).
Containers call e.g. http://172.17.0.1:9100/openai/v1/models and the proxy
strips the route prefix, injects the auth header, and forwards to the real API.

Secrets are decrypted from the age vault at startup (same vault as openclaw-proxy).

Routes:
  /openai/      -> https://api.openai.com        (Bearer token)
  /notion/      -> https://api.notion.com         (Bearer + Notion-Version)
  /cloudflare/  -> https://api.cloudflare.com     (Bearer token)
  /trello/      -> https://api.trello.com         (key/token query params)
  /joplin/      -> http://127.0.0.1:41184         (Joplin data API token)
"""

import asyncio
import json
import logging
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, parse_qs, urlparse, urlunparse

from aiohttp import web, ClientSession, ClientTimeout
from ops_green_yellow import register_green_yellow_routes
from hitl import check_hitl_gate, register_hitl_routes
from ops_red import register_red_routes
from content_safety import process_response, post_behavioral_alerts
import os
import shutil
import signal
from datetime import datetime, timezone
from pathlib import Path

# --- Environment-based configuration ---
TARS_HOME = Path(os.environ.get("TARS_HOME", str(Path.home())))
DOCKER_HOST_IP = os.environ.get("DOCKER_HOST_IP", "172.17.0.1")
AUTH_PROXY_PORT = int(os.environ.get("AUTH_PROXY_PORT", "9100"))
OPENCLAW_DIR = Path(os.environ.get("OPENCLAW_DIR", str(TARS_HOME / ".openclaw")))
SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", str(TARS_HOME / ".secrets")))
NPM_GLOBAL_BIN = os.environ.get("NPM_GLOBAL_BIN", str(TARS_HOME / ".npm-global/bin"))
_py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
PYTHON_SITE_PACKAGES = os.environ.get("PYTHON_SITE_PACKAGES", str(TARS_HOME / ".local" / "lib" / _py_ver / "site-packages"))
del _py_ver

# --- Config ---
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
BIND_PORT = AUTH_PROXY_PORT
VAULT_PATH = str(TARS_HOME / ".secrets-vault/secrets.age")
AGE_KEY_PATH = str(TARS_HOME / ".config/age/key.txt")
JOPLIN_UPSTREAM = os.environ.get("JOPLIN_UPSTREAM", "http://127.0.0.1:41184")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("auth-proxy")


# --- Auth injectors ---

def make_bearer_auth(secret_key, header="Authorization"):
    """Create auth injector that sets Bearer token header."""
    def inject(headers, params, ctx):
        val = ctx["secrets"].get(secret_key)
        if val:
            headers[header] = f"Bearer {val}"
        else:
            log.warning("No secret found for key: %s", secret_key)
    return inject


def make_notion_auth(secret_key):
    """Notion needs Bearer + Notion-Version header."""
    def inject(headers, params, ctx):
        val = ctx["secrets"].get(secret_key)
        if val:
            headers["Authorization"] = f"Bearer {val}"
            if "Notion-Version" not in headers and "notion-version" not in headers:
                headers["Notion-Version"] = "2022-06-28"
        else:
            log.warning("No secret found for key: %s", secret_key)
    return inject


def make_trello_auth(secret_key):
    """Trello uses key + token as query params (stored as JSON)."""
    def inject(headers, params, ctx):
        raw = ctx["secrets"].get(secret_key)
        if raw:
            try:
                creds = json.loads(raw) if isinstance(raw, str) else raw
                params["key"] = creds.get("key", "")
                params["token"] = creds.get("token", "")
            except (json.JSONDecodeError, AttributeError):
                log.warning("Trello credentials not valid JSON: %s", secret_key)
        else:
            log.warning("No secret found for key: %s", secret_key)
    return inject




def make_joplin_auth(api_token):
    """Joplin data API uses token as query param."""
    def inject(headers, params, ctx):
        params["token"] = api_token
    return inject



def load_discord_bot_token():
    """Read Talkie's bot token from openclaw.json."""
    config_path = str(OPENCLAW_DIR / "openclaw.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        token = cfg["channels"]["discord"]["accounts"]["talkie"]["token"]
        if token:
            log.info("Discord bot token loaded from openclaw config")
            return token
        else:
            log.warning("No Discord bot token found in openclaw config")
            return None
    except Exception as e:
        log.warning("Could not load Discord bot token: %s", e)
        return None


def make_discord_bot_auth(token):
    """Discord bot auth injects Bot token header."""
    def inject(headers, params, ctx):
        if token:
            headers["Authorization"] = f"Bot {token}"
        else:
            log.warning("No Discord bot token available")
    return inject



# --- SerpAPI rate limiting ---
# Rate limits: talkie + newsbot are unlimited, others get a daily cap.
# Global hard cap protects the monthly budget (free tier: 250/month).

SERPAPI_RATE_LIMITS = {
    "unlimited": {"main", "newsbot"},  # no per-agent cap
    "daily_cap": 3,          # per-agent daily cap for limited agents
    "global_daily_cap": 50,  # hard global cap across all agents
}
_serpapi_counters = {}  # {"YYYY-MM-DD": {"global": N, "agent_id": N, ...}}


def _serpapi_check_rate(agent_id):
    """Check if agent_id is within rate limits. Returns (allowed, reason)."""
    today = __import__("datetime").date.today().isoformat()
    if today not in _serpapi_counters:
        _serpapi_counters.clear()  # new day, reset all
        _serpapi_counters[today] = {"global": 0}
    counters = _serpapi_counters[today]

    # Global hard cap
    if counters["global"] >= SERPAPI_RATE_LIMITS["global_daily_cap"]:
        return False, f"Global daily cap reached ({counters['global']}/{SERPAPI_RATE_LIMITS['global_daily_cap']})"

    # Per-agent cap (skip for unlimited agents)
    if agent_id not in SERPAPI_RATE_LIMITS["unlimited"]:
        agent_count = counters.get(agent_id, 0)
        if agent_count >= SERPAPI_RATE_LIMITS["daily_cap"]:
            return False, f"Agent '{agent_id}' daily cap reached ({agent_count}/{SERPAPI_RATE_LIMITS['daily_cap']})"

    return True, "ok"


def _serpapi_record_use(agent_id):
    """Record a SerpAPI use for rate limiting."""
    today = __import__("datetime").date.today().isoformat()
    counters = _serpapi_counters.setdefault(today, {"global": 0})
    counters["global"] = counters.get("global", 0) + 1
    counters[agent_id] = counters.get(agent_id, 0) + 1
    log.info("SerpAPI usage: agent=%s, agent_today=%d, global_today=%d",
             agent_id, counters[agent_id], counters["global"])


def make_serpapi_auth(secret_key):
    """SerpAPI uses api_key as query param. Includes rate limiting."""
    def inject(headers, params, ctx):
        val = ctx["secrets"].get(secret_key)
        if val:
            params["api_key"] = val
        else:
            log.warning("No secret found for key: %s", secret_key)
    return inject


# --- Vault management ops endpoints ---

def _save_vault(secrets):
    """Re-encrypt and save the vault."""
    import tempfile
    age_key_path = AGE_KEY_PATH
    # Read public key from age key file
    with open(age_key_path) as f:
        for line in f:
            if line.startswith("# public key:"):
                recipient = line.split(":", 1)[1].strip()
                break
        else:
            raise ValueError("No public key found in age key file")

    # Write to temp, encrypt, replace
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
        json.dump(secrets, tmp, indent=2)
        tmp_path = tmp.name

    enc_tmp = tmp_path + ".age"
    try:
        __import__("subprocess").run(
            ["age", "-r", recipient, "-o", enc_tmp, tmp_path],
            check=True, capture_output=True, text=True,
        )
        __import__("shutil").move(enc_tmp, VAULT_PATH)
        log.info("Vault saved: %d secrets", len(secrets))
    finally:
        import os as _os
        _os.unlink(tmp_path)
        if _os.path.exists(enc_tmp):
            _os.unlink(enc_tmp)


async def handle_ops_vault_list(request):
    """List secret key names (never values)."""
    if _is_container_request(request):
        return web.json_response({"error": "Vault ops not available from containers"}, status=403)
    secrets = request.app["secrets"]
    keys = sorted(secrets.keys())
    return web.json_response({"count": len(keys), "keys": keys})


async def handle_ops_vault_add(request):
    """Add or update a secret in the vault. Body: {"key": "...", "value": "..."}"""
    if _is_container_request(request):
        return web.json_response({"error": "Vault ops not available from containers"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    key = data.get("key", "").strip()
    value = data.get("value", "")
    if not key:
        return web.json_response({"error": "key is required"}, status=400)
    if not value:
        return web.json_response({"error": "value is required"}, status=400)

    secrets = request.app["secrets"]
    is_update = key in secrets
    secrets[key] = value

    try:
        _save_vault(secrets)
    except Exception as e:
        log.error("Failed to save vault: %s", e)
        return web.json_response({"error": f"Failed to save vault: {e}"}, status=500)

    action = "updated" if is_update else "added"
    log.info("Vault secret %s: %s", action, key)
    return web.json_response({"status": action, "key": key})


async def handle_ops_vault_remove(request):
    """Remove a secret from the vault. Body: {"key": "..."}"""
    if _is_container_request(request):
        return web.json_response({"error": "Vault ops not available from containers"}, status=403)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    key = data.get("key", "").strip()
    if not key:
        return web.json_response({"error": "key is required"}, status=400)

    secrets = request.app["secrets"]
    if key not in secrets:
        return web.json_response({"error": f"Key not found: {key}"}, status=404)

    del secrets[key]
    try:
        _save_vault(secrets)
    except Exception as e:
        log.error("Failed to save vault: %s", e)
        return web.json_response({"error": f"Failed to save vault: {e}"}, status=500)

    log.info("Vault secret removed: %s", key)
    return web.json_response({"status": "removed", "key": key})


async def handle_ops_serpapi_usage(request):
    """Show SerpAPI rate limit usage."""
    if _is_container_request(request):
        return web.json_response({"error": "Not available from containers"}, status=403)
    today = __import__("datetime").date.today().isoformat()
    counters = _serpapi_counters.get(today, {"global": 0})
    return web.json_response({
        "date": today,
        "global_used": counters.get("global", 0),
        "global_cap": SERPAPI_RATE_LIMITS["global_daily_cap"],
        "per_agent": {k: v for k, v in counters.items() if k != "global"},
        "unlimited_agents": sorted(SERPAPI_RATE_LIMITS["unlimited"]),
        "limited_agent_cap": SERPAPI_RATE_LIMITS["daily_cap"],
    })


# --- Route definitions ---
ROUTE_DEFS = {
    "openai": {
        "upstream": "https://api.openai.com",
        "auth": make_bearer_auth("secrets/openai-api-key"),
    },
    "notion": {
        "upstream": "https://api.notion.com",
        "auth": make_notion_auth("secrets/notion-token"),
    },
    "cloudflare": {
        "upstream": "https://api.cloudflare.com",
        "auth": make_bearer_auth("secrets/cloudflare-token"),
    },
    "trello": {
        "upstream": "https://api.trello.com",
        "auth": make_trello_auth("secrets/trello-credentials.json"),
    },
    "anthropic": {
        "upstream": "https://api.anthropic.com",
        "auth": make_bearer_auth("secrets/anthropic-api-key", header="x-api-key"),
    },
    # Discord API — uses bot token from openclaw config (loaded at startup)
    # Joplin is special — populated at startup after reading config
    "tavily": {
        "upstream": "https://api.tavily.com",
        "auth": make_bearer_auth("secrets/tavily-api-key"),
    },
    "github": {
        "upstream": "https://api.github.com",
        "auth": make_bearer_auth("secrets/github-token"),
    },
    "serpapi": {
        "upstream": "https://serpapi.com",
        "auth": make_serpapi_auth("secrets/serpapi-api-key"),
    },
}


# --- Vault ---
def load_vault():
    """Decrypt the age vault and return secrets dict."""
    if not os.path.exists(VAULT_PATH):
        log.warning("Vault not found at %s — starting with empty secrets. Run setup.sh to configure.", VAULT_PATH)
        return {}
    if not os.path.exists(AGE_KEY_PATH):
        log.warning("Age key not found at %s — starting with empty secrets.", AGE_KEY_PATH)
        return {}
    try:
        result = subprocess.run(
            ["age", "-d", "-i", AGE_KEY_PATH, VAULT_PATH],
            capture_output=True, text=True, check=True,
        )
        secrets = json.loads(result.stdout)
        log.info("Vault loaded: %d secrets", len(secrets))
        return secrets
    except subprocess.CalledProcessError as e:
        log.warning("Failed to decrypt vault: %s — starting with empty secrets.", e.stderr.strip())
        return {}
    except json.JSONDecodeError as e:
        log.warning("Vault JSON parse error: %s — starting with empty secrets.", e)
        return {}


def load_joplin_token():
    """Read Joplin API token from its settings file."""
    settings_path = str(TARS_HOME / ".config/joplin/settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        token = settings.get("api.token")
        if token:
            log.info("Joplin API token loaded from %s", settings_path)
            return token
        else:
            log.warning("No api.token in Joplin settings")
            return None
    except Exception as e:
        log.warning("Could not load Joplin settings: %s", e)
        return None



# --- Ops endpoints (port exposure for sandbox containers) ---
OPS_PORT_MIN = 3000
OPS_PORT_MAX = 4999
OPS_MAX_FORWARDS = 20
OPS_ACTIVE_FORWARDS = {}  # port -> {"server": asyncio.Server, "container_ip": str, ...}


def _is_container_request(request):
    """Check if request comes from a Docker container."""
    peername = request.transport.get_extra_info("peername")
    if not peername:
        return False
    ip = peername[0]
    return ip.startswith("172.17.") or ip.startswith("172.18.")


def _get_container_ip(request):
    """Get the requesting container's IP."""
    peername = request.transport.get_extra_info("peername")
    return peername[0] if peername else None


# --- Agent identification ---
_agent_map = {}       # IP -> agent_name
_agent_map_lock = threading.Lock()
_AGENT_MAP_REFRESH = 60  # seconds

def _refresh_agent_map():
    """Query Docker bridge network to map container IPs to agent names."""
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", "bridge", "--format", "{{json .Containers}}"],
            capture_output=True, text=True, timeout=5
        )
        containers = json.loads(result.stdout)
        new_map = {}
        for cid, info in containers.items():
            name = info.get("Name", "")
            ip = info.get("IPv4Address", "").split("/")[0]
            if not ip:
                continue
            # Extract agent name from "openclaw-sbx-agent-{name}-{hash}"
            if name.startswith("openclaw-sbx-agent-"):
                parts = name.split("-")
                agent_name = parts[3] if len(parts) >= 5 else name
            else:
                agent_name = name
            new_map[ip] = agent_name
        with _agent_map_lock:
            _agent_map.clear()
            _agent_map.update(new_map)
        log.info("Agent map refreshed: %s", {v: k for k, v in new_map.items()})
    except Exception as e:
        log.warning("Failed to refresh agent map: %s", e)


def _get_agent_id(request):
    """Resolve request source IP to agent name."""
    ip = _get_container_ip(request)
    if not ip:
        return "host"
    if not (ip.startswith("172.17.") or ip.startswith("172.18.")):
        return "host"
    with _agent_map_lock:
        agent = _agent_map.get(ip)
    if agent:
        return agent
    log.warning("SECURITY: Request from unmapped container IP %s", ip)
    return f"unknown-{ip}"


async def _agent_map_refresh_loop():
    """Periodically refresh the container IP -> agent mapping."""
    while True:
        await asyncio.sleep(_AGENT_MAP_REFRESH)
        _refresh_agent_map()


# --- Structured audit logging ---
AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", str(TARS_HOME / ".rescue-bot/logs/audit.jsonl"))
_audit_fd = None

def _init_audit_log():
    """Open audit log file for append-only writing."""
    global _audit_fd
    log_dir = os.path.dirname(AUDIT_LOG_PATH)
    os.makedirs(log_dir, exist_ok=True)
    _audit_fd = open(AUDIT_LOG_PATH, "a", buffering=1)  # line-buffered
    log.info("Audit log opened: %s", AUDIT_LOG_PATH)


def _write_audit(entry: dict):
    """Write a single audit log entry."""
    if _audit_fd:
        try:
            _audit_fd.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except Exception as e:
            log.error("Failed to write audit log: %s", e)


def _audit_ops(request, status, detail=""):
    """Log an ops endpoint access to the audit log."""
    agent_id = _get_agent_id(request)
    source_ip = _get_container_ip(request) or "127.0.0.1"
    _write_audit({
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent": agent_id, "ip": source_ip,
        "method": request.method, "route": "ops",
        "path": request.path, "status": status,
        "bytes": 0, "ms": 0, "detail": detail,
    })


# --- Rate limiting & allowlists ---
RATE_CONFIG_PATH = os.environ.get("RATE_CONFIG_PATH", str(TARS_HOME / ".rescue-bot/rate-limits.json"))
_rate_config = {}
_rate_counters = {}  # "agent:rule_key" -> {"count": N, "window_start": timestamp}

def _load_rate_config():
    """Load rate limit and allowlist config from host-only JSON file."""
    global _rate_config
    try:
        with open(RATE_CONFIG_PATH) as f:
            _rate_config = json.load(f)
        log.info("Rate config loaded: %d route rules, %d allowlists, mode=%s",
                 len(_rate_config.get("routes", {})),
                 len(_rate_config.get("allowlists", {})),
                 _rate_config.get("allowlist_mode", "log"))
    except FileNotFoundError:
        log.warning("No rate config at %s — no rate limits or allowlists active", RATE_CONFIG_PATH)
        _rate_config = {}
    except Exception as e:
        log.error("Failed to load rate config: %s", e)
        _rate_config = {}


def _check_rate_limit(agent_id, route, path):
    """Check if request is within rate limits.
    Returns (allowed: bool, desc: str, limit: int, count: int)."""
    if not _rate_config.get("routes"):
        return True, "", 0, 0

    full_path = f"{route}{path}"

    # Check agent overrides first (most specific)
    rule = None
    rule_key = None
    overrides = _rate_config.get("agent_overrides", {}).get(agent_id, {})
    for pattern, cfg in overrides.items():
        if full_path.startswith(pattern):
            rule = cfg
            rule_key = f"{agent_id}:{pattern}"
            break

    # Then check route rules
    if not rule:
        for pattern, cfg in _rate_config.get("routes", {}).items():
            if full_path.startswith(pattern):
                rule = cfg
                rule_key = pattern
                break

    # Fall back to defaults
    if not rule:
        defaults = _rate_config.get("defaults", {})
        if not defaults.get("max_per_hour"):
            return True, "", 0, 0
        rule = defaults
        rule_key = "default"

    # Determine window and limit
    if "max_per_minute" in rule:
        window = 60
        limit = rule["max_per_minute"]
    elif "max_per_hour" in rule:
        window = 3600
        limit = rule["max_per_hour"]
    elif "max_per_day" in rule:
        window = 86400
        limit = rule["max_per_day"]
    else:
        return True, "", 0, 0

    # Check counter
    counter_key = f"{agent_id}:{rule_key}"
    now = time.time()
    counter = _rate_counters.get(counter_key)
    if not counter or (now - counter["window_start"]) > window:
        _rate_counters[counter_key] = {"count": 1, "window_start": now}
        return True, rule.get("desc", rule_key), limit, 1

    counter["count"] += 1
    if counter["count"] > limit:
        return False, rule.get("desc", rule_key), limit, counter["count"]

    return True, rule.get("desc", rule_key), limit, counter["count"]


def _check_allowlist(agent_id, route):
    """Check if agent is allowed to access route.
    Returns (allowed: bool, mode: str).
    Mode is 'log' (log-only, don't block) or 'enforce' (block)."""
    allowlists = _rate_config.get("allowlists", {})
    if not allowlists:
        return True, "none"

    mode = _rate_config.get("allowlist_mode", "log")
    allowed_routes = allowlists.get(agent_id, allowlists.get("_default", ["*"]))

    if "*" in allowed_routes:
        return True, mode
    if not allowed_routes:
        return False, mode
    for pattern in allowed_routes:
        if route == pattern or route.startswith(pattern):
            return True, mode
    return False, mode


def _make_tcp_forwarder(target_ip, target_port):
    """Create an asyncio TCP forwarding handler (pure Python, no socat needed)."""
    async def handle_client(reader, writer):
        try:
            t_reader, t_writer = await asyncio.wait_for(
                asyncio.open_connection(target_ip, target_port), timeout=5
            )
        except Exception as e:
            log.warning("Forward connect failed -> %s:%d: %s", target_ip, target_port, e)
            writer.close()
            return

        async def pipe(src, dst, label):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pipe(reader, t_writer, "client->target"),
            pipe(t_reader, writer, "target->client"),
        )

    return handle_client


async def handle_ops_expose_port(request):
    """Expose a container port on the host (Tailscale-accessible). Pure Python TCP forward."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    port = data.get("port")
    if not isinstance(port, int) or not (OPS_PORT_MIN <= port <= OPS_PORT_MAX):
        return web.json_response({
            "error": f"Port must be integer in range {OPS_PORT_MIN}-{OPS_PORT_MAX}"
        }, status=400)

    container_ip = _get_container_ip(request)
    target_ip = data.get("target_ip", container_ip)
    if not target_ip or not (target_ip.startswith("172.17.") or target_ip.startswith("172.18.")):
        return web.json_response({"error": "Invalid target IP"}, status=400)

    target_port = data.get("target_port", port)
    if not isinstance(target_port, int) or not (1 <= target_port <= 65535):
        return web.json_response({"error": "Invalid target_port"}, status=400)

    if len(OPS_ACTIVE_FORWARDS) >= OPS_MAX_FORWARDS and port not in OPS_ACTIVE_FORWARDS:
        return web.json_response({
            "error": f"Maximum {OPS_MAX_FORWARDS} active forwards reached"
        }, status=429)

    # Stop existing forward for this host port if any
    if port in OPS_ACTIVE_FORWARDS:
        old = OPS_ACTIVE_FORWARDS[port]
        old["server"].close()
        await old["server"].wait_closed()
        log.info("Stopped existing forward on port %d", port)
        del OPS_ACTIVE_FORWARDS[port]

    # Start asyncio TCP server as forwarder
    handler = _make_tcp_forwarder(target_ip, target_port)
    try:
        server = await asyncio.start_server(handler, "0.0.0.0", port)
    except OSError as e:
        return web.json_response({"error": f"Cannot bind port {port}: {e}"}, status=409)

    OPS_ACTIVE_FORWARDS[port] = {
        "server": server,
        "container_ip": target_ip,
        "container_port": target_port,
        "started": datetime.now(timezone.utc).isoformat(),
        "requested_by": container_ip,
    }

    log.info("Port forward: host:%d -> %s:%d (requested by %s)",
             port, target_ip, target_port, container_ip)

    return web.json_response({
        "status": "ok",
        "port": port,
        "target": f"{target_ip}:{target_port}",
        "tailscale_url": f"http://clawd:{port}",
    })


async def handle_ops_release_port(request):
    """Release a port forward."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    port = data.get("port")
    if not isinstance(port, int):
        return web.json_response({"error": "Port required (integer)"}, status=400)

    if port not in OPS_ACTIVE_FORWARDS:
        return web.json_response({"error": f"No active forward on port {port}"}, status=404)

    info = OPS_ACTIVE_FORWARDS.pop(port)
    info["server"].close()
    await info["server"].wait_closed()
    log.info("Released port %d", port)

    return web.json_response({"status": "ok", "port": port, "released": True})


async def handle_ops_list_ports(request):
    """List active port forwards."""
    forwards = []
    for port, info in sorted(OPS_ACTIVE_FORWARDS.items()):
        forwards.append({
            "host_port": port,
            "target": f"{info['container_ip']}:{info['container_port']}",
            "started": info["started"],
            "tailscale_url": f"http://clawd:{port}",
        })
    return web.json_response({"forwards": forwards, "count": len(forwards)})



# --- Ops endpoints: Cloudflare tunnel route management ---
OPS_ALLOWED_DOMAIN = "offthehooksolutions.com"
TUNNEL_CONFIG = os.environ.get("TUNNEL_CONFIG", str(TARS_HOME / ".cloudflared/config.yml"))
FORWARDS_CONFIG = os.environ.get("FORWARDS_CONFIG", str(TARS_HOME / ".cloudflared/forwards.json"))
CLOUDFLARED_BIN = os.environ.get("CLOUDFLARED_BIN", str(TARS_HOME / ".local/bin/cloudflared"))

import yaml


def _load_tunnel_config():
    with open(TUNNEL_CONFIG) as f:
        return yaml.safe_load(f)


def _save_tunnel_config(cfg):
    with open(TUNNEL_CONFIG, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def _load_forwards():
    try:
        with open(FORWARDS_CONFIG) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _save_forwards(forwards):
    with open(FORWARDS_CONFIG, "w") as f:
        json.dump(forwards, f, indent=4)


async def handle_ops_add_route(request):
    """Add a subdomain route to the Cloudflare tunnel + port forward."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    subdomain = data.get("subdomain", "").strip().lower()
    port = data.get("port")
    label = data.get("label", subdomain)

    if not subdomain:
        return web.json_response({"error": "subdomain required"}, status=400)
    if not isinstance(port, int) or not (OPS_PORT_MIN <= port <= OPS_PORT_MAX):
        return web.json_response({
            "error": f"Port must be integer in range {OPS_PORT_MIN}-{OPS_PORT_MAX}"
        }, status=400)

    # Validate subdomain (alphanumeric + hyphens only, must be under allowed domain)
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", subdomain):
        return web.json_response({"error": "Invalid subdomain (alphanumeric and hyphens only)"}, status=400)

    hostname = f"{subdomain}.{OPS_ALLOWED_DOMAIN}"
    container_ip = _get_container_ip(request)
    target_ip = data.get("target_ip", container_ip)
    target_port = data.get("target_port", port)

    # 1. Add DNS route via cloudflared
    proc = await asyncio.create_subprocess_exec(
        CLOUDFLARED_BIN, "tunnel", "route", "dns", "oths", hostname,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = (stdout.decode() + stderr.decode()).strip()
    if proc.returncode != 0 and "already exists" not in output.lower():
        log.error("Failed to add DNS route for %s: %s", hostname, output)
        return web.json_response({"error": f"DNS route failed: {output}"}, status=500)

    # 2. Update tunnel config
    cfg = _load_tunnel_config()
    ingress = cfg.get("ingress", [])
    # Remove catch-all, add new entry, re-add catch-all
    catch_all = ingress[-1] if ingress and "hostname" not in ingress[-1] else {"service": "http_status:404"}
    ingress = [e for e in ingress if e.get("hostname") != hostname and "hostname" in e]
    ingress.append({"hostname": hostname, "service": f"http://localhost:{port}"})
    ingress.append(catch_all)
    cfg["ingress"] = ingress
    _save_tunnel_config(cfg)

    # 3. Add/update port forward config
    forwards = _load_forwards()
    forwards = [f for f in forwards if f.get("host_port") != port]
    forwards.append({
        "host_port": port,
        "target_ip": target_ip,
        "target_port": target_port,
        "label": label,
    })
    _save_forwards(forwards)

    # 4. Restart cloudflared to pick up new config
    restart = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "restart", "cloudflared",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await restart.communicate()

    # 5. Restart port forwarder to pick up new forwards
    restart2 = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "restart", "port-forwarder",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await restart2.communicate()

    log.info("Route added: %s -> localhost:%d -> %s:%d", hostname, port, target_ip, target_port)

    return web.json_response({
        "status": "ok",
        "hostname": hostname,
        "url": f"https://{hostname}",
        "port": port,
        "target": f"{target_ip}:{target_port}",
    })


async def handle_ops_remove_route(request):
    """Remove a subdomain route from the tunnel."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    subdomain = data.get("subdomain", "").strip().lower()
    if not subdomain:
        return web.json_response({"error": "subdomain required"}, status=400)

    hostname = f"{subdomain}.{OPS_ALLOWED_DOMAIN}"

    # Remove from tunnel config
    cfg = _load_tunnel_config()
    ingress = cfg.get("ingress", [])
    port = None
    new_ingress = []
    for entry in ingress:
        if entry.get("hostname") == hostname:
            # Extract port for forward cleanup
            svc = entry.get("service", "")
            if ":" in svc:
                try:
                    port = int(svc.rsplit(":", 1)[1])
                except ValueError:
                    pass
        else:
            new_ingress.append(entry)
    cfg["ingress"] = new_ingress
    _save_tunnel_config(cfg)

    # Remove from forwards config
    if port:
        forwards = _load_forwards()
        forwards = [f for f in forwards if f.get("host_port") != port]
        _save_forwards(forwards)

    # Restart services
    for svc in ["cloudflared", "port-forwarder"]:
        p = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", svc,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await p.communicate()

    log.info("Route removed: %s", hostname)
    return web.json_response({"status": "ok", "hostname": hostname, "removed": True})


async def handle_ops_list_routes(request):
    """List active tunnel routes."""
    cfg = _load_tunnel_config()
    routes = []
    for entry in cfg.get("ingress", []):
        if "hostname" in entry:
            routes.append({
                "hostname": entry["hostname"],
                "service": entry["service"],
                "url": f"https://{entry['hostname']}",
            })
    return web.json_response({"routes": routes, "count": len(routes)})


async def ops_cleanup(app):
    """Stop all TCP forwarders on shutdown."""
    for port, info in OPS_ACTIVE_FORWARDS.items():
        info["server"].close()
        log.info("Shutdown: stopped forward on port %d", port)
    OPS_ACTIVE_FORWARDS.clear()


# --- Ops endpoints: Agent lifecycle management ---

OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", str(Path(NPM_GLOBAL_BIN) / "openclaw"))
OPENCLAW_CONFIG = str(OPENCLAW_DIR / "openclaw.json")
CREATE_T2_SCRIPT = os.environ.get("CREATE_T2_SCRIPT", str(OPENCLAW_DIR / "scripts/create-t2-agent.sh"))
AGENT_OPS_CHANNEL = "1477655430841765888"
PROTECTED_AGENTS = {"main"}
MANAGED_AGENTS = {"luna", "nova"}  # can update/configure, cannot delete
_gateway_restart_last = 0  # module-level timestamp for rate limiting


DASHBOARD_ALERTS_URL = "http://100.82.23.99:8766/ops-alerts"


async def _notify_agent_ops(app, message):
    """Send a notification to #agent-ops Discord channel and dashboard."""
    session = app["client_session"]
    # Discord notification
    try:
        token_path = str(SECRETS_DIR / "rescue-discord-token")
        with open(token_path) as f:
            token = f.read().strip()
        url = f"https://discord.com/api/v10/channels/{AGENT_OPS_CHANNEL}/messages"
        async with session.post(url,
            headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
            json={"content": message},
        ) as resp:
            if resp.status < 300:
                log.info("Notified #agent-ops: %s", message[:80])
            else:
                log.warning("Failed to notify #agent-ops: %d", resp.status)
    except Exception as e:
        log.warning("Agent-ops notification failed: %s", e)
    # Dashboard alert
    try:
        # Determine alert level from message content
        level = "info"
        if any(w in message.lower() for w in ["error", "fail", "crash", "❌", "🚨"]):
            level = "error"
        elif any(w in message.lower() for w in ["warn", "⚠", "restart", "stopped"]):
            level = "warning"
        async with session.post(DASHBOARD_ALERTS_URL,
            headers={"Content-Type": "application/json"},
            json={"level": level, "source": "ops-proxy", "message": message},
        ) as resp:
            if resp.status < 300:
                log.info("Dashboard alert posted: %s", message[:80])
            else:
                log.warning("Dashboard alert failed: %d", resp.status)
    except Exception as e:
        log.warning("Dashboard alert failed: %s", e)


async def handle_ops_agent_create(request):
    """Create a new T2 agent. Calls create-t2-agent.sh on the host."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    # HITL gate — requires Peter's approval
    agent_id = _get_agent_id(request)
    body = await request.read()
    hitl_resp = await check_hitl_gate(request, agent_id, "POST", "ops", "/agent-create", body)
    if hitl_resp is not None:
        return hitl_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent_id = data.get("id", "").strip().lower()
    model = data.get("model", "anthropic/claude-sonnet-4-20250514")
    binding = data.get("binding", "")

    if not agent_id:
        return web.json_response({"error": "id required"}, status=400)

    if not re.match(r"^[a-z0-9-]+$", agent_id):
        return web.json_response({"error": "id must be lowercase alphanumeric + hyphens"}, status=400)

    requester = _get_container_ip(request)
    log.info("Agent create request: %s (model=%s) from %s", agent_id, model, requester)

    args = [CREATE_T2_SCRIPT, agent_id, model]
    if binding:
        args.append(binding)

    env = os.environ.copy()
    env["PATH"] = NPM_GLOBAL_BIN + ":" + env.get("PATH", "")

    proc = await asyncio.create_subprocess_exec(
        "bash", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() or stdout.decode().strip()
        log.error("Agent create failed: %s", error_msg)
        return web.json_response({"error": error_msg}, status=500)

    output = stdout.decode().strip()
    log.info("Agent created: %s", agent_id)

    await _notify_agent_ops(request.app,
        f"**Agent created:** `{agent_id}` (model: `{model}`)\nRequested by container `{requester}`")

    return web.json_response({"status": "created", "agent_id": agent_id, "model": model, "output": output})


async def handle_ops_agent_list(request):
    """List all configured agents."""
    try:
        with open(OPENCLAW_CONFIG) as f:
            cfg = json.load(f)
        default_model = cfg.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "unknown")
        agents = []
        for a in cfg.get("agents", {}).get("list", []):
            model = a.get("model")
            if isinstance(model, dict):
                model = model.get("primary", default_model)
            elif not model:
                model = default_model
            agents.append({"id": a.get("id"), "model": model})
        return web.json_response({"agents": agents, "count": len(agents)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_agent_delete(request):
    """Delete an agent."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent_id = data.get("id", "").strip().lower()
    if not agent_id:
        return web.json_response({"error": "id required"}, status=400)

    if agent_id in PROTECTED_AGENTS:
        return web.json_response({"error": f"Cannot delete protected agent: {agent_id}"}, status=403)

    if agent_id in MANAGED_AGENTS:
        return web.json_response({
            "error": f"Cannot delete managed agent: {agent_id} (use agent-update to reconfigure)"
        }, status=403)

    requester = _get_container_ip(request)
    log.info("Agent delete request: %s from %s", agent_id, requester)

    env = os.environ.copy()
    env["PATH"] = NPM_GLOBAL_BIN + ":" + env.get("PATH", "")

    proc = await asyncio.create_subprocess_exec(
        OPENCLAW_BIN, "agents", "delete", agent_id, "--force", "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    if proc.returncode != 0:
        error_msg = stderr.decode().strip() or stdout.decode().strip()
        log.error("Agent delete failed: %s", error_msg)
        return web.json_response({"error": error_msg}, status=500)

    log.info("Agent deleted: %s", agent_id)

    await _notify_agent_ops(request.app,
        f"**Agent deleted:** `{agent_id}`\nRequested by container `{requester}`")

    try:
        result = json.loads(stdout.decode())
        return web.json_response({"status": "deleted", "agent_id": agent_id, "details": result})
    except Exception:
        return web.json_response({"status": "deleted", "agent_id": agent_id})


AGENT_UPDATE_ALLOWED_FIELDS = {"name", "model", "binding"}
AGENT_UPDATE_BLOCKED_FIELDS = {"sandbox", "workspace", "subagents", "tools"}


async def handle_ops_agent_update(request):
    """Update an agent's config. Whitelist of allowed fields only."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent_id = data.get("id", "").strip().lower()
    if not agent_id:
        return web.json_response({"error": "id required"}, status=400)

    # Check for blocked fields
    blocked_present = AGENT_UPDATE_BLOCKED_FIELDS & set(data.keys())
    if blocked_present:
        return web.json_response({
            "error": f"Cannot modify blocked fields: {', '.join(sorted(blocked_present))}"
        }, status=403)

    # Read current config
    try:
        with open(OPENCLAW_CONFIG) as f:
            cfg = json.load(f)
    except Exception as e:
        return web.json_response({"error": f"Failed to read config: {e}"}, status=500)

    # Find agent in list
    agent_list = cfg.get("agents", {}).get("list", [])
    agent_entry = None
    agent_idx = None
    for i, a in enumerate(agent_list):
        if a.get("id") == agent_id:
            agent_entry = a
            agent_idx = i
            break

    if agent_entry is None:
        return web.json_response({"error": f"Agent not found: {agent_id}"}, status=404)

    changes = []
    requester = _get_container_ip(request)

    # Apply name
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not (1 <= len(name) <= 32):
            return web.json_response({"error": "name must be a string, 1-32 chars"}, status=400)
        agent_entry["name"] = name
        changes.append(f"name=`{name}`")

    # Apply model (blocked for protected agents)
    if "model" in data:
        if agent_id in PROTECTED_AGENTS:
            return web.json_response({
                "error": f"Cannot modify model for protected agent: {agent_id}"
            }, status=403)
        model = data["model"]
        if not isinstance(model, str) or "/" not in model:
            return web.json_response({"error": "model must be in provider/model format"}, status=400)
        agent_entry["model"] = model
        changes.append(f"model=`{model}`")

    # Apply binding updates (guildId, accountId only)
    if "binding" in data:
        binding_data = data["binding"]
        if not isinstance(binding_data, dict):
            return web.json_response({"error": "binding must be an object"}, status=400)
        allowed_binding_keys = {"guildId", "accountId"}
        bad_keys = set(binding_data.keys()) - allowed_binding_keys
        if bad_keys:
            return web.json_response({
                "error": f"Only guildId and accountId allowed in binding, got: {', '.join(sorted(bad_keys))}"
            }, status=400)

        # Find or create binding entry for this agent
        bindings = cfg.get("bindings", [])
        agent_binding = None
        for b in bindings:
            if b.get("agentId") == agent_id:
                agent_binding = b
                break
        if agent_binding is None:
            agent_binding = {"agentId": agent_id, "match": {"channel": "discord"}}
            bindings.append(agent_binding)
            cfg["bindings"] = bindings

        if "guildId" in binding_data:
            agent_binding["match"]["guildId"] = str(binding_data["guildId"])
            changes.append(f"binding.guildId=`{binding_data['guildId']}`")
        if "accountId" in binding_data:
            agent_binding["match"]["accountId"] = str(binding_data["accountId"])
            changes.append(f"binding.accountId=`{binding_data['accountId']}`")

    if not changes:
        return web.json_response({"error": "No valid fields to update"}, status=400)

    # Write back
    agent_list[agent_idx] = agent_entry
    try:
        with open(OPENCLAW_CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        return web.json_response({"error": f"Failed to write config: {e}"}, status=500)

    log.info("Agent updated: %s [%s] by %s", agent_id, ", ".join(changes), requester)

    await _notify_agent_ops(request.app,
        f"**Agent updated:** `{agent_id}`\nChanges: {', '.join(changes)}\nRequested by container `{requester}`")

    return web.json_response({"status": "updated", "agent_id": agent_id, "changes": changes})


async def handle_ops_agent_cleanup(request):
    """Clean up orphaned agent data for agents no longer in openclaw.json."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    # Read config to get active agent IDs
    try:
        with open(OPENCLAW_CONFIG) as f:
            cfg = json.load(f)
    except Exception as e:
        return web.json_response({"error": f"Failed to read config: {e}"}, status=500)

    active_agents = {a.get("id") for a in cfg.get("agents", {}).get("list", []) if a.get("id")}
    requester = _get_container_ip(request)
    log.info("Agent cleanup request from %s, active agents: %s", requester, active_agents)

    cleaned = []

    # Scan agent dirs
    agents_dir = str(OPENCLAW_DIR / "agents")
    if os.path.isdir(agents_dir):
        for name in os.listdir(agents_dir):
            full = os.path.join(agents_dir, name)
            if os.path.isdir(full) and name not in active_agents:
                shutil.rmtree(full)
                cleaned.append(f"agents/{name}")
                log.info("Cleaned orphaned agent dir: %s", full)

    # Scan workspace dirs
    openclaw_base = str(OPENCLAW_DIR)
    if os.path.isdir(openclaw_base):
        for name in os.listdir(openclaw_base):
            if not name.startswith("workspace-"):
                continue
            # workspace-<agent_id> format
            agent_id = name[len("workspace-"):]
            full = os.path.join(openclaw_base, name)
            if os.path.isdir(full) and agent_id not in active_agents:
                shutil.rmtree(full)
                cleaned.append(name)
                log.info("Cleaned orphaned workspace: %s", full)

    if cleaned:
        await _notify_agent_ops(request.app,
            f"**Agent cleanup:** removed {len(cleaned)} orphaned dir(s): {', '.join(f'`{c}`' for c in cleaned)}\nRequested by container `{requester}`")
    else:
        log.info("Agent cleanup: nothing to clean")

    return web.json_response({"status": "ok", "cleaned": cleaned, "active_agents": sorted(active_agents)})


async def handle_ops_gateway_restart(request):
    """Restart the openclaw gateway. Rate limited to once per 60s."""
    global _gateway_restart_last

    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    now = time.monotonic()
    elapsed = now - _gateway_restart_last
    if _gateway_restart_last > 0 and elapsed < 60:
        remaining = int(60 - elapsed)
        return web.json_response({
            "error": f"Rate limited. Try again in {remaining}s"
        }, status=429)

    requester = _get_container_ip(request)
    log.info("Gateway restart requested by %s", requester)

    await _notify_agent_ops(request.app,
        f"**Gateway restarting** — requested by container `{requester}`")

    _gateway_restart_last = now

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "openclaw-gateway",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            log.error("Gateway restart failed: %s", error_msg)
            await _notify_agent_ops(request.app,
                f"**Gateway restart FAILED:** `{error_msg}`")
            return web.json_response({"error": error_msg}, status=500)

        log.info("Gateway restarted successfully")
        await _notify_agent_ops(request.app,
            f"**Gateway restarted** successfully")

        return web.json_response({"status": "restarted"})

    except asyncio.TimeoutError:
        log.error("Gateway restart timed out")
        await _notify_agent_ops(request.app,
            f"**Gateway restart TIMED OUT** (30s)")
        return web.json_response({"error": "Restart timed out"}, status=504)


# --- Ops endpoints: Cron management (wraps openclaw cron CLI) ---

CRON_MAX_JOBS = 20
CRON_MIN_INTERVAL_MINUTES = 15
GATEWAY_TOKEN_PATH = str(OPENCLAW_DIR / "openclaw.json")


def _get_gateway_token():
    """Read the gateway auth token from openclaw config."""
    try:
        with open(OPENCLAW_CONFIG) as f:
            cfg = json.load(f)
        return cfg.get("gateway", {}).get("auth", {}).get("token", "")
    except Exception:
        return ""


def _build_cron_base_args():
    """Build base args for openclaw cron commands with gateway auth."""
    token = _get_gateway_token()
    args = [OPENCLAW_BIN, "cron"]
    return args, token


def _parse_interval_minutes(cron_expr=None, every=None):
    """Estimate the minimum interval in minutes from a cron expression or --every duration."""
    if every:
        every = every.strip().lower()
        if every.endswith("m"):
            try:
                return int(every[:-1])
            except ValueError:
                pass
        elif every.endswith("h"):
            try:
                return int(every[:-1]) * 60
            except ValueError:
                pass
        elif every.endswith("d"):
            try:
                return int(every[:-1]) * 1440
            except ValueError:
                pass
        return 0  # can't parse, will be rejected

    if cron_expr:
        parts = cron_expr.strip().split()
        if len(parts) >= 5:
            minute_field = parts[0]
            # */N pattern
            if minute_field.startswith("*/"):
                try:
                    return int(minute_field[2:])
                except ValueError:
                    pass
            # If minute is specific and hour is *, that's every 60min at most
            if parts[1] == "*" and minute_field != "*":
                return 60
            # If both minute and hour are specific, it's daily = fine
            if parts[1] != "*" and minute_field != "*":
                return 1440
            # * * * * * = every minute
            if minute_field == "*" and parts[1] == "*":
                return 1
        return 0

    return 0


async def handle_ops_cron_list(request):
    """List all OpenClaw cron jobs."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    env = os.environ.copy()
    env["PATH"] = NPM_GLOBAL_BIN + ":" + env.get("PATH", "")
    token = _get_gateway_token()

    args = [OPENCLAW_BIN, "cron", "list", "--all", "--json"]
    if token:
        args.extend(["--token", token])

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            return web.json_response({"error": error_msg}, status=500)

        result = json.loads(stdout.decode())
        return web.json_response(result)
    except asyncio.TimeoutError:
        return web.json_response({"error": "Timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_cron_add(request):
    """Add a cron job. Agent-scoped to T2 agents only."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent_id = data.get("agent", "").strip().lower()
    if not agent_id:
        return web.json_response({"error": "agent required"}, status=400)

    if agent_id in PROTECTED_AGENTS:
        return web.json_response({
            "error": f"Cannot create crons for protected agent: {agent_id}"
        }, status=403)

    name = data.get("name", "").strip()
    agent = data.get("agent", "main").strip().lower()
    if not name:
        return web.json_response({"error": "name required"}, status=400)

    message = data.get("message", "").strip()
    if not message:
        return web.json_response({"error": "message required"}, status=400)

    cron_expr = data.get("cron", "").strip()
    every = data.get("every", "").strip()
    at = data.get("at", "").strip()

    if not cron_expr and not every and not at:
        return web.json_response({"error": "One of cron, every, or at required"}, status=400)

    # Validate minimum interval (skip for one-shot --at jobs)
    if not at:
        interval = _parse_interval_minutes(cron_expr=cron_expr or None, every=every or None)
        if interval < CRON_MIN_INTERVAL_MINUTES:
            return web.json_response({
                "error": f"Minimum interval is {CRON_MIN_INTERVAL_MINUTES} minutes (got ~{interval}min)"
            }, status=400)

    # Check max jobs
    env = os.environ.copy()
    env["PATH"] = NPM_GLOBAL_BIN + ":" + env.get("PATH", "")
    token = _get_gateway_token()

    list_args = [OPENCLAW_BIN, "cron", "list", "--all", "--json"]
    if token:
        list_args.extend(["--token", token])

    try:
        proc = await asyncio.create_subprocess_exec(
            *list_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        existing = json.loads(stdout.decode())
        if existing.get("total", 0) >= CRON_MAX_JOBS:
            return web.json_response({
                "error": f"Maximum {CRON_MAX_JOBS} cron jobs reached"
            }, status=429)
    except Exception:
        pass  # proceed anyway if list fails

    # Build add command
    args = [OPENCLAW_BIN, "cron", "add", "--agent", agent_id, "--name", name, "--message", message, "--json"]
    if cron_expr:
        args.extend(["--cron", cron_expr])
    if every:
        args.extend(["--every", every])
    if at:
        args.extend(["--at", at])
    if data.get("tz"):
        args.extend(["--tz", data["tz"]])
    if data.get("session"):
        args.extend(["--session", data["session"]])
    if data.get("deleteAfterRun"):
        args.append("--delete-after-run")
    if token:
        args.extend(["--token", token])

    requester = _get_container_ip(request)
    log.info("Cron add request: agent=%s name=%s from %s", agent_id, name, requester)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            log.error("Cron add failed: %s", error_msg)
            return web.json_response({"error": error_msg}, status=500)

        output = stdout.decode().strip()
        log.info("Cron added: agent=%s name=%s", agent_id, name)

        schedule_desc = cron_expr or every or f"at {at}"
        await _notify_agent_ops(request.app,
            f"**Cron added:** `{name}` for agent `{agent_id}` ({schedule_desc})\nRequested by container `{requester}`")

        try:
            result = json.loads(output)
            return web.json_response({"status": "created", "job": result})
        except Exception:
            return web.json_response({"status": "created", "output": output})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Timed out"}, status=504)


async def handle_ops_cron_delete(request):
    """Delete a cron job. Blocked for jobs targeting protected agents."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    job_id = data.get("id", "").strip()
    if not job_id:
        return web.json_response({"error": "id required"}, status=400)

    env = os.environ.copy()
    env["PATH"] = NPM_GLOBAL_BIN + ":" + env.get("PATH", "")
    token = _get_gateway_token()

    # Check if job targets a protected agent
    list_args = [OPENCLAW_BIN, "cron", "list", "--all", "--json"]
    if token:
        list_args.extend(["--token", token])

    try:
        proc = await asyncio.create_subprocess_exec(
            *list_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        existing = json.loads(stdout.decode())
        for job in existing.get("jobs", []):
            if job.get("id") == job_id:
                if job.get("agentId") in PROTECTED_AGENTS:
                    return web.json_response({
                        "error": f"Cannot delete cron for protected agent: {job.get('agentId')}"
                    }, status=403)
                break
        else:
            return web.json_response({"error": f"Job not found: {job_id}"}, status=404)
    except Exception as e:
        log.warning("Could not verify job before delete: %s", e)

    # Delete
    args = [OPENCLAW_BIN, "cron", "rm", job_id, "--json"]
    if token:
        args.extend(["--token", token])

    requester = _get_container_ip(request)
    log.info("Cron delete request: id=%s from %s", job_id, requester)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            log.error("Cron delete failed: %s", error_msg)
            return web.json_response({"error": error_msg}, status=500)

        log.info("Cron deleted: id=%s", job_id)
        await _notify_agent_ops(request.app,
            f"**Cron deleted:** job `{job_id}`\nRequested by container `{requester}`")

        return web.json_response({"status": "deleted", "job_id": job_id})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Timed out"}, status=504)


async def _handle_cron_toggle(request, action):
    """Enable or disable a cron job. Blocked for jobs targeting protected agents."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    job_id = data.get("id", "").strip()
    if not job_id:
        return web.json_response({"error": "id required"}, status=400)

    env = os.environ.copy()
    env["PATH"] = NPM_GLOBAL_BIN + ":" + env.get("PATH", "")
    token = _get_gateway_token()

    # Check if job targets a protected agent
    list_args = [OPENCLAW_BIN, "cron", "list", "--all", "--json"]
    if token:
        list_args.extend(["--token", token])

    try:
        proc = await asyncio.create_subprocess_exec(
            *list_args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        existing = json.loads(stdout.decode())
        for job in existing.get("jobs", []):
            if job.get("id") == job_id:
                if job.get("agentId") in PROTECTED_AGENTS:
                    return web.json_response({
                        "error": f"Cannot {action} cron for protected agent: {job.get('agentId')}"
                    }, status=403)
                break
        else:
            return web.json_response({"error": f"Job not found: {job_id}"}, status=404)
    except Exception as e:
        log.warning("Could not verify job before %s: %s", action, e)

    args = [OPENCLAW_BIN, "cron", action, job_id, "--json"]
    if token:
        args.extend(["--token", token])

    requester = _get_container_ip(request)
    log.info("Cron %s request: id=%s from %s", action, job_id, requester)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            log.error("Cron %s failed: %s", action, error_msg)
            return web.json_response({"error": error_msg}, status=500)

        past = "enabled" if action == "enable" else "disabled"
        log.info("Cron %s: id=%s", past, job_id)
        await _notify_agent_ops(request.app,
            f"**Cron {past}:** job `{job_id}`\nRequested by container `{requester}`")

        return web.json_response({"status": past, "job_id": job_id})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Timed out"}, status=504)


async def handle_ops_cron_enable(request):
    """Enable a cron job."""
    return await _handle_cron_toggle(request, "enable")


async def handle_ops_cron_disable(request):
    """Disable a cron job."""
    return await _handle_cron_toggle(request, "disable")

# --- Ops endpoints: System crontab management (host crontab for workspace scripts) ---

SYSCRON_WORKSPACE_BASE = str(OPENCLAW_DIR)
SYSCRON_ALLOWED_WORKSPACES = {
    "main": str(OPENCLAW_DIR / "workspace"),
    # workspace-<agent> pattern for all other agents
}


def _resolve_workspace(agent_id=None):
    """Resolve workspace path for an agent. None or main = Talkie."""
    if not agent_id or agent_id == "main":
        return str(OPENCLAW_DIR / "workspace")
    # Validate agent_id
    if not re.match(r"^[a-z0-9-]+$", agent_id):
        return None
    ws = str(OPENCLAW_DIR / f"workspace-{agent_id}")
    if os.path.isdir(ws):
        return ws
    return None
SYSCRON_MAX_ENTRIES = 50
SYSCRON_MANAGED_TAG = "# managed:"


def _read_crontab_lines():
    """Read clawd's crontab, return list of lines."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return []
        return result.stdout.strip().split("\n") if result.stdout.strip() else []
    except Exception:
        return []


def _write_crontab_lines(lines):
    """Write lines to clawd's crontab."""
    content = "\n".join(lines) + "\n" if lines else ""
    result = subprocess.run(
        ["crontab", "-"], input=content, capture_output=True, text=True
    )
    return result.returncode == 0


def _parse_managed_crons(lines):
    """Parse managed cron entries. Format: comment line then cron line."""
    managed = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(SYSCRON_MANAGED_TAG):
            name = line[len(SYSCRON_MANAGED_TAG):].strip()
            if i + 1 < len(lines) and not lines[i + 1].strip().startswith("#"):
                cron_line = lines[i + 1].strip()
                parts = cron_line.split(None, 5)
                if len(parts) >= 6:
                    managed.append({
                        "name": name,
                        "schedule": " ".join(parts[:5]),
                        "command": parts[5],
                        "comment_idx": i,
                        "cron_idx": i + 1,
                    })
                i += 2
                continue
        i += 1
    return managed


async def handle_ops_sys_crons(request):
    """List managed system cron entries for clawd."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    lines = _read_crontab_lines()
    managed = _parse_managed_crons(lines)

    crons = [{"name": m["name"], "schedule": m["schedule"], "command": m["command"]} for m in managed]
    return web.json_response({"crons": crons, "count": len(crons)})


async def handle_ops_sys_cron_add(request):
    """Add a system cron entry. Only scripts under workspace are allowed."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    # HITL gate — requires Peter's approval
    agent_id = _get_agent_id(request)
    body = await request.read()
    hitl_resp = await check_hitl_gate(request, agent_id, "POST", "ops", "/sys-cron-add", body)
    if hitl_resp is not None:
        return hitl_resp

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    schedule = data.get("schedule", "").strip()
    script = data.get("script", "").strip()
    name = data.get("name", "").strip()
    agent = data.get("agent", "main").strip().lower()

    if not schedule:
        return web.json_response({"error": "schedule required (5-field cron expression)"}, status=400)
    if not script:
        return web.json_response({"error": "script required (relative path from workspace)"}, status=400)
    if not name:
        return web.json_response({"error": "name required (unique identifier)"}, status=400)

    # Resolve workspace for target agent
    workspace = _resolve_workspace(agent)
    if not workspace:
        return web.json_response({"error": f"Unknown agent or workspace not found: {agent}"}, status=404)

    # Validate name
    if not re.match(r"^[a-z0-9][a-z0-9_-]*$", name):
        return web.json_response({"error": "name must be lowercase alphanumeric, hyphens, underscores"}, status=400)

    # Validate schedule (5 fields)
    sched_parts = schedule.split()
    if len(sched_parts) != 5:
        return web.json_response({"error": "schedule must be a 5-field cron expression"}, status=400)

    # Validate script path — block traversal, absolute paths, shell metacharacters
    if ".." in script or script.startswith("/") or any(c in script for c in ";|&`$(){}[]<>!\\"):
        return web.json_response({"error": "Invalid script path (no .., absolute paths, or shell metacharacters)"}, status=400)

    full_path = os.path.join(workspace, script)
    resolved = os.path.realpath(full_path)
    if not resolved.startswith(workspace + "/"):
        return web.json_response({"error": "Script must be within workspace directory"}, status=400)

    if not os.path.isfile(resolved):
        return web.json_response({"error": f"Script not found: {script} (in {agent} workspace)"}, status=404)

    # Read current crontab
    lines = _read_crontab_lines()
    managed = _parse_managed_crons(lines)

    if len(managed) >= SYSCRON_MAX_ENTRIES:
        return web.json_response({"error": f"Maximum {SYSCRON_MAX_ENTRIES} managed cron entries reached"}, status=429)

    # Check duplicate name
    for m in managed:
        if m["name"] == name:
            return web.json_response({"error": f"Cron '{name}' already exists. Remove it first."}, status=409)

    # Build command
    command = f"/bin/bash {resolved}"

    # Append to crontab
    lines.append(f"{SYSCRON_MANAGED_TAG}{name}")
    lines.append(f"{schedule} {command}")

    if not _write_crontab_lines(lines):
        return web.json_response({"error": "Failed to write crontab"}, status=500)

    requester = _get_container_ip(request)
    log.info("Sys-cron added: name=%s schedule='%s' script=%s by %s", name, schedule, script, requester)

    return web.json_response({
        "ok": True,
        "name": name,
        "schedule": schedule,
        "full_command": command,
    })


async def handle_ops_sys_cron_remove(request):
    """Remove a managed system cron entry by name."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    name = data.get("name", "").strip()
    agent = data.get("agent", "main").strip().lower()
    if not name:
        return web.json_response({"error": "name required"}, status=400)

    lines = _read_crontab_lines()
    managed = _parse_managed_crons(lines)

    target = None
    for m in managed:
        if m["name"] == name:
            target = m
            break

    if not target:
        return web.json_response({"error": f"No managed cron with name '{name}'"}, status=404)

    # Remove the comment + cron lines (work from end to avoid index shift)
    indices_to_remove = {target["comment_idx"], target["cron_idx"]}
    new_lines = [line for i, line in enumerate(lines) if i not in indices_to_remove]

    if not _write_crontab_lines(new_lines):
        return web.json_response({"error": "Failed to write crontab"}, status=500)

    requester = _get_container_ip(request)
    log.info("Sys-cron removed: name=%s by %s", name, requester)

    return web.json_response({"ok": True, "removed": name})



# --- Tool management endpoints ---

TOOL_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9._-]+(\[[\w,]+\])?$")
TOOL_PROTECTED_AGENTS = {"main"}  # T1 manages its own tools directly
async def handle_ops_tool_install(request):
    """Install a Python package into a T2 agent's workspace."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent_id = data.get("agent", "").strip().lower()
    package = data.get("package", "").strip()
    extras = data.get("extras", "").strip()

    if not agent_id:
        return web.json_response({"error": "agent required"}, status=400)
    if not package:
        return web.json_response({"error": "package required"}, status=400)

    if agent_id in TOOL_PROTECTED_AGENTS:
        return web.json_response({"error": f"Cannot install tools for protected agent '{agent_id}'"}, status=403)

    workspace = _resolve_workspace(agent_id)
    if not workspace:
        return web.json_response({"error": f"Agent '{agent_id}' not found or invalid workspace"}, status=404)

    # Build full package spec with extras
    pkg_spec = package + extras if extras else package
    if not TOOL_PACKAGE_RE.match(pkg_spec):
        return web.json_response({"error": f"Invalid package spec: {pkg_spec}"}, status=400)

    user_base = os.path.join(workspace, ".local")
    os.makedirs(user_base, exist_ok=True)

    requester = _get_container_ip(request)
    log.info("Tool install request: %s for agent %s from %s", pkg_spec, agent_id, requester)

    env = {**os.environ, "PYTHONUSERBASE": user_base, "PYTHONPATH": PYTHON_SITE_PACKAGES}
    cmd = ["/usr/bin/python3", "-m", "pip", "install", "--user", "--break-system-packages", pkg_spec]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        output = stdout.decode(errors="replace")

        if proc.returncode != 0:
            log.warning("Tool install failed for %s/%s: rc=%d", agent_id, pkg_spec, proc.returncode)
            return web.json_response({
                "error": "pip install failed",
                "returncode": proc.returncode,
                "output": output[-2000:],
            }, status=500)

        log.info("Tool installed: %s for agent %s", pkg_spec, agent_id)
        await _notify_agent_ops(request.app, f"🔧 Installed `{pkg_spec}` for agent **{agent_id}** (from {requester})")

        return web.json_response({
            "ok": True,
            "agent": agent_id,
            "package": pkg_spec,
            "output": output[-2000:],
        })

    except asyncio.TimeoutError:
        log.warning("Tool install timed out for %s/%s", agent_id, pkg_spec)
        return web.json_response({"error": "pip install timed out (300s)"}, status=504)
    except Exception as e:
        log.error("Tool install error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_tool_list(request):
    """List installed Python packages in a T2 agent's workspace."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    agent_id = request.query.get("agent", "").strip().lower()
    if not agent_id:
        return web.json_response({"error": "agent query param required"}, status=400)

    workspace = _resolve_workspace(agent_id)
    if not workspace:
        return web.json_response({"error": f"Agent '{agent_id}' not found or invalid workspace"}, status=404)

    user_base = os.path.join(workspace, ".local")

    # Find site-packages dir
    import glob as globmod
    site_dirs = globmod.glob(os.path.join(user_base, "lib", "python3.*", "site-packages"))
    if not site_dirs:
        return web.json_response({"ok": True, "agent": agent_id, "packages": []})

    cmd = ["/usr/bin/python3", "-m", "pip", "list", "--path", site_dirs[0], "--format=json"]
    env_list = {**os.environ, "PYTHONPATH": PYTHON_SITE_PACKAGES}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        packages = json.loads(stdout.decode(errors="replace")) if stdout else []
        return web.json_response({"ok": True, "agent": agent_id, "packages": packages})
    except Exception as e:
        log.error("Tool list error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_tool_remove(request):
    """Uninstall a Python package from a T2 agent's workspace."""
    if not _is_container_request(request):
        return web.json_response({"error": "Must be called from a sandbox container"}, status=403)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent_id = data.get("agent", "").strip().lower()
    package = data.get("package", "").strip()

    if not agent_id:
        return web.json_response({"error": "agent required"}, status=400)
    if not package:
        return web.json_response({"error": "package required"}, status=400)

    if agent_id in TOOL_PROTECTED_AGENTS:
        return web.json_response({"error": f"Cannot remove tools from protected agent '{agent_id}'"}, status=403)

    workspace = _resolve_workspace(agent_id)
    if not workspace:
        return web.json_response({"error": f"Agent '{agent_id}' not found or invalid workspace"}, status=404)

    # Validate package name (no extras for uninstall)
    if not re.match(r"^[a-zA-Z0-9._-]+$", package):
        return web.json_response({"error": f"Invalid package name: {package}"}, status=400)

    user_base = os.path.join(workspace, ".local")

    # Find site-packages to target
    import glob as globmod
    site_dirs = globmod.glob(os.path.join(user_base, "lib", "python3.*", "site-packages"))
    if not site_dirs:
        return web.json_response({"error": "No packages installed for this agent"}, status=404)

    requester = _get_container_ip(request)
    log.info("Tool remove request: %s for agent %s from %s", package, agent_id, requester)

    # pip uninstall needs to find package metadata in the target site-packages
    import glob as globmod2
    target_sites = globmod2.glob(os.path.join(user_base, "lib", "python3.*", "site-packages"))
    if not target_sites:
        return web.json_response({"error": "No packages installed for this agent"}, status=404)
    env = {**os.environ, "PYTHONUSERBASE": user_base, "PYTHONPATH": PYTHON_SITE_PACKAGES + ":" + target_sites[0]}
    cmd = ["/usr/bin/python3", "-m", "pip", "uninstall", "-y", "--break-system-packages", package]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode(errors="replace")

        if proc.returncode != 0:
            log.warning("Tool remove failed for %s/%s: rc=%d", agent_id, package, proc.returncode)
            return web.json_response({
                "error": "pip uninstall failed",
                "returncode": proc.returncode,
                "output": output[-2000:],
            }, status=500)

        log.info("Tool removed: %s from agent %s", package, agent_id)
        await _notify_agent_ops(request.app, f"🔧 Removed `{package}` from agent **{agent_id}** (from {requester})")

        return web.json_response({
            "ok": True,
            "agent": agent_id,
            "package": package,
            "output": output[-2000:],
        })

    except asyncio.TimeoutError:
        return web.json_response({"error": "pip uninstall timed out (60s)"}, status=504)
    except Exception as e:
        log.error("Tool remove error: %s", e)
        return web.json_response({"error": str(e)}, status=500)




# --- Proxy handler ---

async def handle_joplin_sync(request):
    """Trigger joplin sync on the host."""
    log.info("Triggering joplin sync...")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(Path(NPM_GLOBAL_BIN) / "joplin"), "sync",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        output = stdout.decode().strip()
        log.info("Joplin sync completed (exit %d)", proc.returncode)
        return web.json_response({
            "status": "ok" if proc.returncode == 0 else "error",
            "exit_code": proc.returncode,
            "output": output,
        })
    except asyncio.TimeoutError:
        log.error("Joplin sync timed out")
        return web.json_response({"status": "error", "error": "sync timed out"}, status=504)
    except Exception as e:
        log.error("Joplin sync failed: %s", e)
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def handle_index(request):
    """Show available routes (no secrets)."""
    routes = []
    for prefix, defn in ROUTE_DEFS.items():
        routes.append({"prefix": f"/{prefix}/", "upstream": defn["upstream"]})
    return web.json_response({
        "service": "auth-proxy",
        "routes": routes,
        "ops": [
            {"method": "POST", "path": "/ops/expose-port", "desc": "Expose container port on host (3000-4999)"},
            {"method": "POST", "path": "/ops/release-port", "desc": "Release a port forward"},
            {"method": "GET", "path": "/ops/ports", "desc": "List active port forwards"},
            {"method": "POST", "path": "/ops/add-route", "desc": "Add subdomain route (*.offthehooksolutions.com)"},
            {"method": "POST", "path": "/ops/remove-route", "desc": "Remove a subdomain route"},
            {"method": "GET", "path": "/ops/routes", "desc": "List active tunnel routes"},
            {"method": "POST", "path": "/ops/agent-create", "desc": "Create a new T2 agent"},
            {"method": "GET", "path": "/ops/agents", "desc": "List all agents"},
            {"method": "POST", "path": "/ops/agent-delete", "desc": "Delete an agent (protected + managed agents blocked)"},
            {"method": "POST", "path": "/ops/agent-update", "desc": "Update agent config (name, model, binding)"},
            {"method": "POST", "path": "/ops/agent-cleanup", "desc": "Clean up orphaned agent dirs not in openclaw.json"},
            {"method": "POST", "path": "/ops/gateway-restart", "desc": "Restart openclaw gateway (60s cooldown)"},
            {"method": "GET", "path": "/ops/cron-list", "desc": "List all OpenClaw cron jobs"},
            {"method": "POST", "path": "/ops/cron-add", "desc": "Add cron job (T2 agents only, 15min min interval)"},
            {"method": "POST", "path": "/ops/cron-delete", "desc": "Delete a cron job (protected agents blocked)"},
            {"method": "POST", "path": "/ops/cron-enable", "desc": "Enable a cron job"},
            {"method": "POST", "path": "/ops/cron-disable", "desc": "Disable a cron job"},
            {"method": "GET", "path": "/ops/sys-crons", "desc": "List managed system cron entries (workspace scripts)"},
            {"method": "POST", "path": "/ops/sys-cron-add", "desc": "Add system cron (any agent workspace, clawd user)"},
            {"method": "POST", "path": "/ops/sys-cron-remove", "desc": "Remove a managed system cron entry"},
            {"method": "POST", "path": "/ops/tool-install", "desc": "Install Python package for a T2 agent"},
            {"method": "GET", "path": "/ops/tool-list", "desc": "List installed packages for an agent"},
            {"method": "POST", "path": "/ops/tool-remove", "desc": "Uninstall Python package from a T2 agent"},
            # --- Green tier (read-only observability) ---
            {"method": "GET", "path": "/ops/health", "desc": "System health overview (CPU, memory, disk, Docker)"},
            {"method": "GET", "path": "/ops/services", "desc": "List all managed services and status"},
            {"method": "GET", "path": "/ops/health/services", "desc": "Per-service health details"},
            {"method": "GET", "path": "/ops/containers", "desc": "Docker containers with stats"},
            {"method": "GET", "path": "/ops/logs", "desc": "Sanitized log tail (?service=X&lines=50&since=1h)"},
            {"method": "GET", "path": "/ops/disk", "desc": "Disk usage breakdown"},
            {"method": "GET", "path": "/ops/network", "desc": "Network connectivity status"},
            {"method": "GET", "path": "/ops/watchdog", "desc": "Health check of all critical services"},
            {"method": "GET", "path": "/ops/auth-routes", "desc": "List auth proxy routes"},
            {"method": "GET", "path": "/ops/versions", "desc": "Component versions"},
            {"method": "GET", "path": "/ops/agent-status", "desc": "Agent state (?agent=X)"},
            {"method": "GET", "path": "/ops/agent-config", "desc": "Read agent config (?agent=X)"},
            {"method": "GET", "path": "/ops/agent-workspace-list", "desc": "List workspace files (?agent=X&path=/)"},
            {"method": "GET", "path": "/ops/secret-list", "desc": "List secret names (?scope=X)"},
            # --- Yellow tier (controlled mutations, cooldowns + audit) ---
            {"method": "POST", "path": "/ops/service-restart", "desc": "Restart allowlisted service (60s cooldown)"},
            {"method": "POST", "path": "/ops/container-restart", "desc": "Restart Docker container (60s cooldown)"},
            {"method": "POST", "path": "/ops/agent-message", "desc": "Send message to another agent"},
            {"method": "POST", "path": "/ops/agent-skill-install", "desc": "Install skill into agent workspace"},
            {"method": "POST", "path": "/ops/agent-skill-remove", "desc": "Remove skill from agent workspace"},
            {"method": "POST", "path": "/ops/agent-workspace-init", "desc": "Initialize agent workspace from templates"},
            {"method": "POST", "path": "/ops/tunnel-restart", "desc": "Restart Cloudflare tunnel (60s cooldown)"},
            {"method": "GET/POST", "path": "/ops/tailscale-status", "desc": "Check/reconnect Tailscale"},
            {"method": "POST", "path": "/ops/auth-route-test", "desc": "Test auth proxy route works"},
            {"method": "POST", "path": "/ops/agent-config", "desc": "Update agent config"},
            # --- HITL gates (approval workflow for high-impact actions) ---
            {"method": "GET", "path": "/ops/hitl-status/{hitl_id}", "desc": "Check HITL approval status"},
            {"method": "GET", "path": "/ops/hitl-list", "desc": "List pending HITL approval requests"},
            # --- Red tier (HITL-gated, sensitive/destructive) ---
            {"method": "POST", "path": "/ops/secret-set", "desc": "Set vault secret (HITL)"},
            {"method": "POST", "path": "/ops/secret-delete", "desc": "Delete vault secret (HITL)"},
            {"method": "POST", "path": "/ops/secret-inject", "desc": "Inject secret into agent env (HITL)"},
            {"method": "POST", "path": "/ops/service-stop", "desc": "Stop a service (HITL)"},
            {"method": "POST", "path": "/ops/service-start", "desc": "Start a service (HITL)"},
            {"method": "POST", "path": "/ops/auth-route-add", "desc": "Add auth proxy route (HITL)"},
            {"method": "POST", "path": "/ops/auth-route-remove", "desc": "Remove auth proxy route (HITL)"},
            {"method": "POST", "path": "/ops/docker-prune", "desc": "Prune Docker resources (HITL)"},
            {"method": "POST", "path": "/ops/docker-build", "desc": "Build Docker image (HITL)"},
            {"method": "POST", "path": "/ops/update-openclaw", "desc": "Update OpenClaw (HITL)"},
            {"method": "POST", "path": "/ops/rebuild-sandbox", "desc": "Rebuild sandbox + restart (HITL)"},
            {"method": "POST", "path": "/ops/disk-cleanup", "desc": "Clean up disk space (HITL)"},
            {"method": "POST", "path": "/ops/agent-backup", "desc": "Backup agent (HITL)"},
            {"method": "POST", "path": "/ops/agent-restore", "desc": "Restore agent from backup (HITL)"},
            {"method": "GET", "path": "/ops/backups", "desc": "List available backups"},
            {"method": "POST", "path": "/ops/watchdog-configure", "desc": "Configure watchdog rules (HITL)"},
        ],
        "hitl_gated": [
            "POST/PATCH notion/v1/pages — Create/modify Notion pages",
            "POST/PUT/PATCH/DELETE cloudflare/ — Any Cloudflare mutation",
            "POST ops/agent-create — Create agent",
            "POST ops/agent-delete — Delete agent",
            "POST ops/sys-cron-add — Add system cron",
            "POST ops/secret-set — Set vault secret",
            "POST ops/secret-delete — Delete vault secret",
            "POST ops/secret-inject — Inject secret into agent",
            "POST ops/service-stop — Stop service",
            "POST ops/service-start — Start service",
            "POST ops/auth-route-add — Add auth proxy route",
            "POST ops/auth-route-remove — Remove auth proxy route",
            "POST ops/docker-prune — Docker resource cleanup",
            "POST ops/docker-build — Docker image build",
            "POST ops/update-openclaw — Update OpenClaw",
            "POST ops/rebuild-sandbox — Rebuild sandbox",
            "POST ops/disk-cleanup — Disk space cleanup",
            "POST ops/agent-backup — Backup agent",
            "POST ops/agent-restore — Restore agent",
            "POST ops/watchdog-configure — Configure watchdog",
        ],
        "note": "Append path after prefix, e.g. /openai/v1/models",
    })


async def handle_proxy(request):
    """Proxy request to upstream with credential injection."""
    t0 = time.monotonic()
    agent_id = _get_agent_id(request)
    source_ip = _get_container_ip(request) or "127.0.0.1"

    path = request.path
    parts = path.strip("/").split("/", 1)
    prefix = parts[0]
    remainder = "/" + parts[1] if len(parts) > 1 else "/"

    defn = ROUTE_DEFS.get(prefix)
    if not defn:
        _write_audit({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent_id, "ip": source_ip,
            "method": request.method, "route": prefix, "path": remainder,
            "status": 404, "bytes": 0, "ms": 0,
        })
        return web.json_response(
            {"error": f"Unknown route prefix: {prefix}"},
            status=404,
        )

    # Allowlist check
    al_allowed, al_mode = _check_allowlist(agent_id, prefix)
    if not al_allowed:
        _write_audit({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent_id, "ip": source_ip,
            "method": request.method, "route": prefix, "path": remainder,
            "status": 403 if al_mode == "enforce" else 200,
            "bytes": 0, "ms": 0, "allowlist_violation": True,
        })
        log.warning("ALLOWLIST [%s]: agent=%s route=%s (mode=%s)", al_mode, agent_id, prefix, al_mode)
        if al_mode == "enforce":
            return web.json_response(
                {"error": f"Route /{prefix}/ not allowed for agent {agent_id}"},
                status=403,
            )

    # Generic rate limiting
    rl_allowed, rl_desc, rl_limit, rl_count = _check_rate_limit(agent_id, prefix, remainder)
    if not rl_allowed:
        log.warning("RATE_LIMIT: agent=%s route=%s/%s desc=%s (%d/%d)",
                     agent_id, prefix, remainder, rl_desc, rl_count, rl_limit)
        _write_audit({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent_id, "ip": source_ip,
            "method": request.method, "route": prefix, "path": remainder,
            "status": 429, "bytes": 0,
            "ms": round((time.monotonic() - t0) * 1000),
            "rate_limited": True, "desc": rl_desc,
        })
        return web.json_response(
            {"error": f"Rate limited: {rl_desc} ({rl_count}/{rl_limit})", "retry_after": 60},
            status=429,
            headers={"Retry-After": "60"},
        )

    # HITL gate check (high-impact actions require Peter's approval)
    hitl_response = await check_hitl_gate(request, agent_id, request.method, prefix, remainder, await request.read())
    if hitl_response is not None:
        return hitl_response

    # SerpAPI rate limiting (legacy, kept for backward compat)
    if prefix == "serpapi":
        serp_agent = request.query.get("agent", agent_id)
        allowed, reason = _serpapi_check_rate(serp_agent)
        if not allowed:
            log.warning("SerpAPI rate limited: agent=%s reason=%s", serp_agent, reason)
            _write_audit({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "agent": agent_id, "ip": source_ip,
                "method": request.method, "route": prefix, "path": remainder,
                "status": 429, "bytes": 0,
                "ms": round((time.monotonic() - t0) * 1000),
            })
            return web.json_response(
                {"error": f"Rate limited: {reason}. Use web_fetch for known URLs instead."},
                status=429,
            )
        _serpapi_record_use(serp_agent)

    upstream_url = defn["upstream"].rstrip("/") + remainder

    # Build headers (forward most, skip hop-by-hop)
    skip_headers = {"host", "transfer-encoding", "connection", "keep-alive"}
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in skip_headers
    }

    # Build query params
    params = dict(request.query)

    # Inject auth
    ctx = {"secrets": request.app["secrets"]}
    defn["auth"](headers, params, ctx)

    # Add query params to URL
    if params:
        parsed = urlparse(upstream_url)
        existing = parse_qs(parsed.query)
        existing.update({k: [v] for k, v in params.items()})
        flat = {k: v[0] if len(v) == 1 else v for k, v in existing.items()}
        new_query = urlencode(flat, doseq=True)
        upstream_url = urlunparse(parsed._replace(query=new_query))

    body = await request.read()
    method = request.method
    log.info("[%s] %s /%s%s -> %s", agent_id, method, prefix, remainder, defn["upstream"])

    try:
        session = request.app["client_session"]
        async with session.request(
            method, upstream_url, headers=headers, data=body,
            allow_redirects=False,
        ) as resp:
            resp_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in {"transfer-encoding", "connection", "content-encoding"}
            }

            # Strip injected credentials from Location headers on redirects
            # to prevent leaking secrets (e.g. Trello key/token in query params)
            if resp.status in (301, 302, 303, 307, 308):
                location = resp_headers.get("Location") or resp_headers.get("location")
                if location:
                    _sensitive_params = {"key", "token", "api_key", "apikey", "access_token"}
                    loc_parsed = urlparse(location)
                    loc_qs = parse_qs(loc_parsed.query, keep_blank_values=True)
                    if any(p in loc_qs for p in _sensitive_params):
                        cleaned_qs = {k: v for k, v in loc_qs.items() if k not in _sensitive_params}
                        cleaned_url = urlunparse(loc_parsed._replace(
                            query=urlencode(cleaned_qs, doseq=True) if cleaned_qs else ""
                        ))
                        for hdr in ("Location", "location"):
                            if hdr in resp_headers:
                                resp_headers[hdr] = cleaned_url
                        log.warning("SECURITY: Stripped credentials from redirect Location header")

            resp_body = await resp.read()
            elapsed = round((time.monotonic() - t0) * 1000)
            log.info("  <- %d (%d bytes, %dms)", resp.status, len(resp_body), elapsed)

            # Content safety pipeline (sanitize, scan, monitor)
            content_type = resp_headers.get("Content-Type", resp_headers.get("content-type", ""))
            safety = process_response(agent_id, method, prefix, remainder, resp.status, resp_body, content_type)
            final_body = safety["sanitized_body"]

            # Audit log with injection scoring
            audit_entry = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "agent": agent_id, "ip": source_ip,
                "method": method, "route": prefix, "path": remainder,
                "status": resp.status, "bytes": len(resp_body), "ms": elapsed,
            }
            if safety["injection_score"] > 0:
                audit_entry["injection_score"] = safety["injection_score"]
                audit_entry["injection_categories"] = safety["injection_categories"]
            if safety["sanitization_changes"]:
                audit_entry["sanitized"] = list(set(safety["sanitization_changes"]))[:5]
            _write_audit(audit_entry)

            # Post behavioral alerts (async, non-blocking)
            if safety["alerts"]:
                asyncio.ensure_future(post_behavioral_alerts(request.app, safety["alerts"]))

            return web.Response(
                status=resp.status,
                headers=resp_headers,
                body=final_body,
            )
    except Exception as e:
        elapsed = round((time.monotonic() - t0) * 1000)
        log.error("  <- ERROR: %s (%dms)", e, elapsed)
        _write_audit({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent_id, "ip": source_ip,
            "method": method, "route": prefix, "path": remainder,
            "status": 502, "bytes": 0, "ms": elapsed, "error": str(e),
        })
        return web.json_response(
            {"error": f"Upstream request failed: {str(e)}"},
            status=502,
        )


async def on_startup(app):
    timeout = ClientTimeout(total=120)
    app["client_session"] = ClientSession(timeout=timeout)
    # Init audit logging
    _init_audit_log()
    # Init agent identification
    _refresh_agent_map()
    asyncio.create_task(_agent_map_refresh_loop())
    # Load rate limits and allowlists
    _load_rate_config()


async def on_cleanup(app):
    await app["client_session"].close()


def _handle_sighup(*args):
    """Reload rate limit config on SIGHUP."""
    log.info("SIGHUP received — reloading rate config")
    _load_rate_config()


def main():
    signal.signal(signal.SIGHUP, _handle_sighup)
    secrets = load_vault()

    # Add Joplin route (reads token from host config, not vault)
    joplin_token = load_joplin_token()
    if joplin_token:
        ROUTE_DEFS["joplin"] = {
            "upstream": JOPLIN_UPSTREAM,
            "auth": make_joplin_auth(joplin_token),
        }

    # Add Discord API route (bot token from openclaw config)
    discord_token = load_discord_bot_token()
    if discord_token:
        ROUTE_DEFS["discord-api"] = {
            "upstream": "https://discord.com/api/v10",
            "auth": make_discord_bot_auth(discord_token),
        }


    # Add Discord News Bot API route (token from vault)
    newsbot_token = secrets.get("openclaw/discord-newsbot-token")
    if newsbot_token:
        ROUTE_DEFS["discord-newsbot-api"] = {
            "upstream": "https://discord.com/api/v10",
            "auth": make_discord_bot_auth(newsbot_token),
        }
        log.info("Discord News Bot API route added")
    @web.middleware
    async def audit_middleware(request, handler):
        """Log all requests to the structured audit log."""
        t0 = time.monotonic()
        agent_id = _get_agent_id(request)
        source_ip = _get_container_ip(request) or "127.0.0.1"
        parts = request.path.strip("/").split("/", 1)
        route = parts[0] if parts else ""
        subpath = "/" + parts[1] if len(parts) > 1 else "/"
        try:
            response = await handler(request)
            elapsed = round((time.monotonic() - t0) * 1000)
            # Skip double-logging for handle_proxy (it logs its own audit entries)
            if route not in ROUTE_DEFS:
                _write_audit({
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "agent": agent_id, "ip": source_ip,
                    "method": request.method, "route": route, "path": subpath,
                    "status": response.status, "bytes": response.content_length or 0,
                    "ms": elapsed,
                })
            return response
        except Exception as e:
            elapsed = round((time.monotonic() - t0) * 1000)
            _write_audit({
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "agent": agent_id, "ip": source_ip,
                "method": request.method, "route": route, "path": subpath,
                "status": 500, "bytes": 0, "ms": elapsed, "error": str(e),
            })
            raise

    app = web.Application(middlewares=[audit_middleware])
    app["secrets"] = secrets
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Index route
    app.router.add_get("/", handle_index)
    app.router.add_post("/joplin-sync", handle_joplin_sync)
    app.router.add_get("/joplin-sync", handle_joplin_sync)

    # Ops routes (port management for sandbox containers)
    app.router.add_post("/ops/expose-port", handle_ops_expose_port)
    app.router.add_post("/ops/release-port", handle_ops_release_port)
    app.router.add_get("/ops/ports", handle_ops_list_ports)
    app.on_cleanup.append(ops_cleanup)

    # Ops routes (tunnel/subdomain management for sandbox containers)
    app.router.add_post("/ops/add-route", handle_ops_add_route)
    app.router.add_post("/ops/remove-route", handle_ops_remove_route)
    app.router.add_get("/ops/routes", handle_ops_list_routes)

    # Ops routes (agent lifecycle management)
    app.router.add_post("/ops/agent-create", handle_ops_agent_create)
    app.router.add_get("/ops/agents", handle_ops_agent_list)
    app.router.add_post("/ops/agent-delete", handle_ops_agent_delete)
    app.router.add_post("/ops/agent-update", handle_ops_agent_update)
    app.router.add_post("/ops/agent-cleanup", handle_ops_agent_cleanup)
    app.router.add_post("/ops/gateway-restart", handle_ops_gateway_restart)

    # Ops routes (cron management)
    app.router.add_get("/ops/cron-list", handle_ops_cron_list)
    app.router.add_post("/ops/cron-add", handle_ops_cron_add)
    app.router.add_post("/ops/cron-delete", handle_ops_cron_delete)
    app.router.add_post("/ops/cron-enable", handle_ops_cron_enable)
    app.router.add_post("/ops/cron-disable", handle_ops_cron_disable)

    # Ops routes (system crontab management for workspace scripts)
    app.router.add_get("/ops/sys-crons", handle_ops_sys_crons)
    app.router.add_post("/ops/sys-cron-add", handle_ops_sys_cron_add)
    app.router.add_post("/ops/sys-cron-remove", handle_ops_sys_cron_remove)

    # Ops routes (tool management for T2 agent workspaces)
    app.router.add_post("/ops/tool-install", handle_ops_tool_install)
    app.router.add_get("/ops/tool-list", handle_ops_tool_list)
    app.router.add_post("/ops/tool-remove", handle_ops_tool_remove)

    # Ops routes (vault / secrets management — host-only)
    app.router.add_get("/ops/vault-list", handle_ops_vault_list)
    app.router.add_post("/ops/vault-add", handle_ops_vault_add)
    app.router.add_post("/ops/vault-remove", handle_ops_vault_remove)

    # Ops routes (SerpAPI usage monitoring)
    app.router.add_get("/ops/serpapi-usage", handle_ops_serpapi_usage)

    # Green + Yellow tier ops endpoints (observability + controlled mutations)
    register_green_yellow_routes(app, ROUTE_DEFS, _get_agent_id, _notify_agent_ops)

    # HITL gate system (approval workflow for high-impact actions)
    register_hitl_routes(app, ROUTE_DEFS, _get_agent_id, _write_audit, _get_container_ip)

    # Red tier ops endpoints (sensitive/destructive, HITL-gated)
    register_red_routes(app, ROUTE_DEFS, _get_agent_id, _notify_agent_ops, _save_vault)

    # Catch-all proxy routes
    for prefix in ROUTE_DEFS:
        app.router.add_route("*", f"/{prefix}/{{path:.*}}", handle_proxy)
        app.router.add_route("*", f"/{prefix}", handle_proxy)

    log.info("Starting auth proxy on %s:%d", BIND_HOST, BIND_PORT)
    log.info("Routes: %s", ", ".join(f"/{p}/ -> {d['upstream']}" for p, d in ROUTE_DEFS.items()))
    web.run_app(app, host=BIND_HOST, port=BIND_PORT, print=None)


if __name__ == "__main__":
    main()
