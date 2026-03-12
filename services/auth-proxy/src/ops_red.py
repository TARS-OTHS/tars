"""
Red tier ops proxy endpoints — sensitive/destructive operations.
Every endpoint requires HITL (Human-in-the-Loop) approval before execution.

Endpoints:
  Secret management: secret-set, secret-delete, secret-inject
  Service lifecycle: service-stop, service-start
  Auth proxy routes: auth-route-add, auth-route-remove
  Docker management: docker-prune, docker-build
  System updates: update-openclaw, rebuild-sandbox
  Disk management: disk/cleanup
  Backup operations: agent-backup, agent-restore, backups (list)
  Watchdog: watchdog/configure
  Google OAuth: google-reauth
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

from aiohttp import web
from hitl import check_hitl_gate
import os as _os_param
from pathlib import Path as _Path_param
_TARS_HOME = _Path_param(_os_param.environ.get('TARS_HOME', str(_Path_param.home())))
_DOCKER_HOST_IP = _os_param.environ.get('DOCKER_HOST_IP', '172.17.0.1')
_MEMORY_API_PORT = _os_param.environ.get('MEMORY_API_PORT', '8897')
_AUTH_PROXY_PORT = _os_param.environ.get('AUTH_PROXY_PORT', '9100')
_NPM_GLOBAL_BIN = _os_param.environ.get('NPM_GLOBAL_BIN', str(_TARS_HOME / '.npm-global/bin'))
del _os_param, _Path_param


log = logging.getLogger("auth-proxy")

VAULT_PATH = str(_TARS_HOME / ".secrets-vault/secrets.age")
AGE_KEY_PATH = str(_TARS_HOME / ".config/age/key.txt")
OPENCLAW_CONFIG = "str(_TARS_HOME / .openclaw/openclaw.json)"
WORKSPACE_BASE = "str(_TARS_HOME / .openclaw/workspace)"
AGENT_WORKSPACES = "str(_TARS_HOME / .openclaw)"
BACKUP_DIR = str(_TARS_HOME / ".rescue-bot/backups")
WATCHDOG_CONFIG = str(_TARS_HOME / ".rescue-bot/watchdog.json")


def _get_agent_id(request):
    fn = request.app.get("_get_agent_id")
    return fn(request) if fn else "unknown"


async def _notify_agent_ops(app, message):
    fn = app.get("_notify_agent_ops")
    if fn:
        await fn(app, message)


async def _hitl_gate(request):
    """Apply HITL gate. Returns a 202 response if gated, None if approved or not applicable."""
    agent_id = _get_agent_id(request)
    body_bytes = await request.read()
    # request.path is e.g. "/ops/service-stop" — pass route="ops" and path="/service-stop"
    path = request.path  # e.g. "/ops/service-stop"
    parts = path.strip("/").split("/", 1)
    route = parts[0]  # "ops"
    remainder = "/" + parts[1] if len(parts) > 1 else "/"  # "/service-stop"
    return await check_hitl_gate(request, agent_id, request.method, route, remainder, body_bytes)


# =============================================================================
# SECRET MANAGEMENT
# =============================================================================

async def handle_ops_secret_set(request):
    """Set a secret in the vault. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    name = data.get("name", "").strip()
    value = data.get("value", "")
    scope = data.get("scope", "global").strip()

    if not name:
        return web.json_response({"error": "name is required"}, status=400)
    if not value:
        return web.json_response({"error": "value is required"}, status=400)
    if not re.match(r"^[a-zA-Z0-9_/.-]+$", name):
        return web.json_response({"error": "Invalid secret name (alphanumeric, _, /, ., - only)"}, status=400)

    secrets = request.app["secrets"]
    # Scope-prefix the key if not global
    key = f"{scope}/{name}" if scope != "global" and "/" not in name else name
    is_update = key in secrets
    secrets[key] = value

    try:
        _save_vault_fn = request.app.get("_save_vault")
        if _save_vault_fn:
            _save_vault_fn(secrets)
        else:
            log.error("No _save_vault function registered")
            return web.json_response({"error": "Vault save not available"}, status=500)
    except Exception as e:
        log.error("Failed to save vault: %s", e)
        return web.json_response({"error": f"Failed to save vault: {e}"}, status=500)

    agent_id = _get_agent_id(request)
    action = "updated" if is_update else "added"
    log.info("RED: Secret %s: %s (by %s)", action, key, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Secret {action}:** `{key}` (scope: {scope}) — by `{agent_id}`")

    return web.json_response({"status": action, "name": key, "scope": scope})


async def handle_ops_secret_delete(request):
    """Delete a secret from the vault. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    name = data.get("name", "").strip()
    if not name:
        return web.json_response({"error": "name is required"}, status=400)

    secrets = request.app["secrets"]
    if name not in secrets:
        return web.json_response({"error": f"Secret not found: {name}"}, status=404)

    del secrets[name]

    try:
        _save_vault_fn = request.app.get("_save_vault")
        if _save_vault_fn:
            _save_vault_fn(secrets)
    except Exception as e:
        return web.json_response({"error": f"Failed to save vault: {e}"}, status=500)

    agent_id = _get_agent_id(request)
    log.info("RED: Secret deleted: %s (by %s)", name, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Secret deleted:** `{name}` — by `{agent_id}`")

    return web.json_response({"status": "deleted", "name": name})


async def handle_ops_secret_inject(request):
    """Inject a vault secret into an agent's env config. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip().lower()
    secret_name = data.get("secret_name", "").strip()
    env_var = data.get("env_var", secret_name).strip()

    if not agent:
        return web.json_response({"error": "agent is required"}, status=400)
    if not secret_name:
        return web.json_response({"error": "secret_name is required"}, status=400)

    secrets = request.app["secrets"]
    if secret_name not in secrets:
        return web.json_response({"error": f"Secret not found: {secret_name}"}, status=404)

    # Update openclaw.json to map secret to agent env
    try:
        with open(OPENCLAW_CONFIG) as f:
            config = json.load(f)
    except Exception as e:
        return web.json_response({"error": f"Failed to read config: {e}"}, status=500)

    agents = config.get("agents", [])
    target = None
    for a in agents:
        if a.get("id", "").lower() == agent:
            target = a
            break

    if not target:
        return web.json_response({"error": f"Agent not found: {agent}"}, status=404)

    if "env" not in target:
        target["env"] = {}
    target["env"][env_var] = f"$VAULT:{secret_name}"

    try:
        with open(OPENCLAW_CONFIG, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        return web.json_response({"error": f"Failed to write config: {e}"}, status=500)

    agent_id = _get_agent_id(request)
    log.info("RED: Secret injected: %s -> %s.%s (by %s)", secret_name, agent, env_var, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Secret injected:** `{secret_name}` → `{agent}` as `{env_var}` — by `{agent_id}`. Restart agent to apply.")

    return web.json_response({
        "status": "injected",
        "agent": agent,
        "env_var": env_var,
        "note": "Restart the agent container for changes to take effect",
    })


# =============================================================================
# SERVICE LIFECYCLE
# =============================================================================

async def handle_ops_service_stop(request):
    """Stop a systemd service. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    service = data.get("service", "").strip()
    if not service:
        return web.json_response({"error": "service is required"}, status=400)

    # Safety: never stop auth-proxy (that's us) or rescue-bot
    NEVER_STOP = {"auth-proxy", "rescue-bot"}
    if service in NEVER_STOP:
        return web.json_response({"error": f"Cannot stop critical service: {service}"}, status=403)

    agent_id = _get_agent_id(request)
    log.info("RED: Service stop requested: %s by %s", service, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Service stopping:** `{service}` — requested by `{agent_id}`")

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "stop", service,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            return web.json_response({"error": error_msg}, status=500)

        await _notify_agent_ops(request.app,
            f"🔴 **Service stopped:** `{service}`")
        return web.json_response({"status": "stopped", "service": service})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Stop timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_service_start(request):
    """Start a systemd service. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    service = data.get("service", "").strip()
    if not service:
        return web.json_response({"error": "service is required"}, status=400)

    agent_id = _get_agent_id(request)
    log.info("RED: Service start requested: %s by %s", service, agent_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "start", service,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            return web.json_response({"error": error_msg}, status=500)

        await _notify_agent_ops(request.app,
            f"🔴 **Service started:** `{service}` — by `{agent_id}`")
        return web.json_response({"status": "started", "service": service})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Start timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# =============================================================================
# AUTH PROXY ROUTE MANAGEMENT
# =============================================================================

async def handle_ops_auth_route_add(request):
    """Add a new auth proxy route dynamically. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    path = data.get("path", "").strip().strip("/")
    upstream = data.get("upstream", "").strip()
    auth_type = data.get("auth_type", "bearer").strip()
    secret_name = data.get("secret_name", "").strip()

    if not path:
        return web.json_response({"error": "path is required"}, status=400)
    if not upstream:
        return web.json_response({"error": "upstream is required"}, status=400)
    if not re.match(r"^[a-z0-9-]+$", path):
        return web.json_response({"error": "path must be lowercase alphanumeric with hyphens"}, status=400)

    route_defs = request.app.get("route_defs", {})
    if path in route_defs:
        return web.json_response({"error": f"Route /{path}/ already exists"}, status=409)

    # Validate secret exists if specified
    if secret_name:
        secrets = request.app["secrets"]
        if secret_name not in secrets:
            return web.json_response({"error": f"Secret not found: {secret_name}"}, status=404)

    agent_id = _get_agent_id(request)
    log.info("RED: Auth route add: /%s/ -> %s (by %s)", path, upstream, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Auth route added:** `/{path}/` → `{upstream}` (auth: {auth_type}) — by `{agent_id}`. Requires proxy restart to activate.")

    # Note: Actually adding the route requires modifying auth-proxy.py code or
    # a dynamic route config file. For now, record the intent and notify.
    return web.json_response({
        "status": "recorded",
        "path": f"/{path}/",
        "upstream": upstream,
        "auth_type": auth_type,
        "note": "Route recorded. Requires auth proxy restart or SIGHUP to activate. RB must add the route definition.",
    })


async def handle_ops_auth_route_remove(request):
    """Remove an auth proxy route. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    path = data.get("path", "").strip().strip("/")
    if not path:
        return web.json_response({"error": "path is required"}, status=400)

    # Protect core routes
    PROTECTED_ROUTES = {"openai", "anthropic", "google", "discord-api", "joplin"}
    if path in PROTECTED_ROUTES:
        return web.json_response({"error": f"Cannot remove core route: /{path}/"}, status=403)

    agent_id = _get_agent_id(request)
    log.info("RED: Auth route remove: /%s/ (by %s)", path, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Auth route removal requested:** `/{path}/` — by `{agent_id}`. Requires proxy restart.")

    return web.json_response({
        "status": "recorded",
        "path": f"/{path}/",
        "note": "Route removal recorded. Requires auth proxy restart to take effect. RB must remove the route definition.",
    })


# =============================================================================
# DOCKER MANAGEMENT
# =============================================================================

async def handle_ops_docker_prune(request):
    """Clean up Docker resources. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        data = {}

    what = data.get("what", ["images", "containers"])
    dry_run = data.get("dry_run", True)  # Default to dry run for safety

    results = {}
    agent_id = _get_agent_id(request)

    for resource in what:
        if resource not in ("images", "containers", "volumes", "networks", "builder"):
            results[resource] = {"error": f"Unknown resource type: {resource}"}
            continue

        cmd = ["docker", resource if resource != "builder" else "builder", "prune", "-f"]
        if resource == "images":
            cmd = ["docker", "image", "prune", "-f"]
            if data.get("dangling_only", True):
                pass  # -f already prunes dangling only
            else:
                cmd.append("-a")
        elif resource == "containers":
            cmd = ["docker", "container", "prune", "-f"]
        elif resource == "volumes":
            cmd = ["docker", "volume", "prune", "-f"]
        elif resource == "networks":
            cmd = ["docker", "network", "prune", "-f"]
        elif resource == "builder":
            cmd = ["docker", "builder", "prune", "-f"]

        if dry_run:
            results[resource] = {"status": "dry_run", "command": " ".join(cmd)}
            continue

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            results[resource] = {
                "status": "pruned" if proc.returncode == 0 else "error",
                "output": stdout.decode().strip()[:500],
                "error": stderr.decode().strip()[:200] if proc.returncode != 0 else None,
            }
        except asyncio.TimeoutError:
            results[resource] = {"status": "timeout"}
        except Exception as e:
            results[resource] = {"status": "error", "error": str(e)}

    mode = "dry_run" if dry_run else "executed"
    log.info("RED: Docker prune (%s): %s by %s", mode, what, agent_id)
    if not dry_run:
        await _notify_agent_ops(request.app,
            f"🔴 **Docker prune executed:** {what} — by `{agent_id}`")

    return web.json_response({"mode": mode, "results": results})


async def handle_ops_docker_build(request):
    """Rebuild sandbox Docker image. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        data = {}

    image = data.get("image", "openclaw-sandbox")
    no_cache = data.get("no_cache", False)

    if not re.match(r"^[a-z0-9-]+$", image):
        return web.json_response({"error": "Invalid image name"}, status=400)

    agent_id = _get_agent_id(request)
    log.info("RED: Docker build requested: %s (no_cache=%s) by %s", image, no_cache, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Docker build started:** `{image}` (no_cache={no_cache}) — by `{agent_id}`")

    # Find Dockerfile
    dockerfile_path = f"str(_TARS_HOME / .openclaw)/Dockerfile"
    if not os.path.exists(dockerfile_path):
        return web.json_response({"error": f"Dockerfile not found at {dockerfile_path}"}, status=404)

    cmd = ["docker", "build", "-t", image, "-f", dockerfile_path, "str(_TARS_HOME / .openclaw)"]
    if no_cache:
        cmd.insert(3, "--no-cache")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()[-500:]
            await _notify_agent_ops(request.app,
                f"🔴 **Docker build FAILED:** `{image}` — `{error_msg[:200]}`")
            return web.json_response({"error": error_msg}, status=500)

        await _notify_agent_ops(request.app,
            f"🔴 **Docker build complete:** `{image}`")
        return web.json_response({
            "status": "built",
            "image": image,
            "output": stdout.decode().strip()[-500:],
        })

    except asyncio.TimeoutError:
        return web.json_response({"error": "Build timed out (10 min limit)"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# =============================================================================
# SYSTEM UPDATES
# =============================================================================

async def handle_ops_update_openclaw(request):
    """Update OpenClaw to latest version. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        data = {}

    version = data.get("version", "latest")
    restart_gateway = data.get("restart_gateway", True)

    agent_id = _get_agent_id(request)
    log.info("RED: OpenClaw update requested: %s by %s", version, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **OpenClaw update started:** version={version} — by `{agent_id}`")

    pkg = "openclaw" if version == "latest" else f"openclaw@{version}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "npm", "update", "-g", pkg,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": f"{_NPM_GLOBAL_BIN}:{os.environ.get('PATH', '')}"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            await _notify_agent_ops(request.app,
                f"🔴 **OpenClaw update FAILED:** `{error_msg[:200]}`")
            return web.json_response({"error": error_msg}, status=500)

        result = {"status": "updated", "output": stdout.decode().strip()[:500]}

        if restart_gateway:
            gw_proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", "openclaw-gateway",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(gw_proc.communicate(), timeout=30)
            result["gateway_restarted"] = gw_proc.returncode == 0

        await _notify_agent_ops(request.app,
            f"🔴 **OpenClaw updated** to {version}" +
            (" + gateway restarted" if restart_gateway else ""))
        return web.json_response(result)

    except asyncio.TimeoutError:
        return web.json_response({"error": "Update timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_rebuild_sandbox(request):
    """Rebuild sandbox and restart agent containers. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        data = {}

    agents = data.get("agents", ["all"])
    rolling = data.get("rolling", True)

    agent_id = _get_agent_id(request)
    log.info("RED: Sandbox rebuild requested: agents=%s rolling=%s by %s", agents, rolling, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Sandbox rebuild started:** agents={agents}, rolling={rolling} — by `{agent_id}`")

    # Step 1: Build the image
    dockerfile_path = "str(_TARS_HOME / .openclaw)/Dockerfile"
    if not os.path.exists(dockerfile_path):
        return web.json_response({"error": "Dockerfile not found"}, status=404)

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", "openclaw-sandbox", "-f", dockerfile_path, "str(_TARS_HOME / .openclaw)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        if proc.returncode != 0:
            return web.json_response({"error": f"Build failed: {stderr.decode()[-300:]}"}, status=500)
    except asyncio.TimeoutError:
        return web.json_response({"error": "Build timed out"}, status=504)

    # Step 2: Restart containers
    results = {}
    try:
        list_proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "--format", "{{.Names}}", "--filter", "name=openclaw-sbx-",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await list_proc.communicate()
        containers = [c.strip() for c in stdout.decode().split("\n") if c.strip()]
    except Exception:
        containers = []

    for container in containers:
        agent_name = container.replace("openclaw-sbx-agent-", "").split("-")[0]
        if "all" not in agents and agent_name not in agents:
            continue

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "restart", container,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=60)
            results[container] = "restarted" if proc.returncode == 0 else "failed"
            if rolling:
                await asyncio.sleep(5)  # Stagger restarts
        except Exception as e:
            results[container] = f"error: {e}"

    await _notify_agent_ops(request.app,
        f"🔴 **Sandbox rebuild complete:** {len(results)} containers restarted")
    return web.json_response({"status": "rebuilt", "containers": results})


# =============================================================================
# DISK MANAGEMENT
# =============================================================================

async def handle_ops_disk_cleanup(request):
    """Clean up disk space. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        data = {}

    actions = data.get("actions", ["old_logs"])
    dry_run = data.get("dry_run", True)

    results = {}
    agent_id = _get_agent_id(request)

    for action in actions:
        if action == "docker_prune":
            if dry_run:
                results["docker_prune"] = {"status": "dry_run"}
            else:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "system", "prune", "-f",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                results["docker_prune"] = {"status": "done", "output": stdout.decode().strip()[:300]}

        elif action == "old_logs":
            log_dirs = [
                str(_TARS_HOME / ".rescue-bot/logs"),
                "str(_TARS_HOME / .openclaw)/logs",
            ]
            cleaned = []
            for log_dir in log_dirs:
                if not os.path.exists(log_dir):
                    continue
                for f in os.listdir(log_dir):
                    fpath = os.path.join(log_dir, f)
                    if os.path.isfile(fpath):
                        age_days = (time.time() - os.path.getmtime(fpath)) / 86400
                        if age_days > 7 and f.endswith((".gz", ".old", ".1", ".2", ".3")):
                            if dry_run:
                                cleaned.append(f"{fpath} ({age_days:.0f}d old)")
                            else:
                                os.remove(fpath)
                                cleaned.append(fpath)
            results["old_logs"] = {"status": "dry_run" if dry_run else "cleaned", "files": cleaned}

        elif action == "old_backups":
            if os.path.exists(BACKUP_DIR):
                backups = sorted(os.listdir(BACKUP_DIR))
                to_remove = backups[:-7] if len(backups) > 7 else []  # Keep last 7
                removed = []
                for b in to_remove:
                    bpath = os.path.join(BACKUP_DIR, b)
                    if dry_run:
                        removed.append(b)
                    else:
                        shutil.rmtree(bpath, ignore_errors=True)
                        removed.append(b)
                results["old_backups"] = {"status": "dry_run" if dry_run else "cleaned", "removed": removed}
            else:
                results["old_backups"] = {"status": "no_backup_dir"}

        else:
            results[action] = {"error": f"Unknown action: {action}"}

    mode = "dry_run" if dry_run else "executed"
    log.info("RED: Disk cleanup (%s): %s by %s", mode, actions, agent_id)
    if not dry_run:
        await _notify_agent_ops(request.app,
            f"🔴 **Disk cleanup executed:** {actions} — by `{agent_id}`")

    return web.json_response({"mode": mode, "results": results})


# =============================================================================
# BACKUP OPERATIONS
# =============================================================================

async def handle_ops_agent_backup(request):
    """Backup an agent's workspace and config. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip().lower()
    include = data.get("include", ["workspace", "config"])

    if not agent:
        return web.json_response({"error": "agent is required"}, status=400)

    # Find agent workspace
    workspace = os.path.join(AGENT_WORKSPACES, f"workspace-{agent}" if agent != "main" else "workspace")
    if not os.path.exists(workspace):
        return web.json_response({"error": f"Workspace not found for {agent}"}, status=404)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_name = f"{agent}-{timestamp}"
    backup_path = os.path.join(BACKUP_DIR, backup_name)
    os.makedirs(backup_path, exist_ok=True)

    results = {}
    agent_id = _get_agent_id(request)

    if "workspace" in include:
        dest = os.path.join(backup_path, "workspace")
        try:
            shutil.copytree(workspace, dest, dirs_exist_ok=True)
            file_count = sum(len(files) for _, _, files in os.walk(dest))
            results["workspace"] = {"status": "backed_up", "files": file_count}
        except Exception as e:
            results["workspace"] = {"status": "error", "error": str(e)}

    if "config" in include:
        try:
            with open(OPENCLAW_CONFIG) as f:
                config = json.load(f)
            agent_config = None
            for a in config.get("agents", []):
                if a.get("id", "").lower() == agent:
                    agent_config = a
                    break
            if agent_config:
                with open(os.path.join(backup_path, "config.json"), "w") as f:
                    json.dump(agent_config, f, indent=2)
                results["config"] = {"status": "backed_up"}
            else:
                results["config"] = {"status": "not_found"}
        except Exception as e:
            results["config"] = {"status": "error", "error": str(e)}

    log.info("RED: Agent backup: %s -> %s (by %s)", agent, backup_name, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Agent backup created:** `{agent}` → `{backup_name}` — by `{agent_id}`")

    return web.json_response({"status": "backed_up", "backup_id": backup_name, "results": results})


async def handle_ops_agent_restore(request):
    """Restore an agent from backup. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip().lower()
    backup_id = data.get("backup_id", "").strip()

    if not agent:
        return web.json_response({"error": "agent is required"}, status=400)
    if not backup_id:
        return web.json_response({"error": "backup_id is required"}, status=400)

    backup_path = os.path.join(BACKUP_DIR, backup_id)
    if not os.path.exists(backup_path):
        return web.json_response({"error": f"Backup not found: {backup_id}"}, status=404)

    # Prevent path traversal
    real_backup = os.path.realpath(backup_path)
    if not real_backup.startswith(os.path.realpath(BACKUP_DIR)):
        return web.json_response({"error": "Invalid backup path"}, status=400)

    workspace = os.path.join(AGENT_WORKSPACES, f"workspace-{agent}" if agent != "main" else "workspace")
    results = {}
    agent_id = _get_agent_id(request)

    backup_workspace = os.path.join(backup_path, "workspace")
    if os.path.exists(backup_workspace):
        try:
            shutil.copytree(backup_workspace, workspace, dirs_exist_ok=True)
            results["workspace"] = {"status": "restored"}
        except Exception as e:
            results["workspace"] = {"status": "error", "error": str(e)}

    log.info("RED: Agent restore: %s from %s (by %s)", agent, backup_id, agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Agent restored:** `{agent}` from `{backup_id}` — by `{agent_id}`")

    return web.json_response({"status": "restored", "backup_id": backup_id, "results": results})


async def handle_ops_backups_list(request):
    """List available backups. Not HITL-gated (read-only)."""
    if not os.path.exists(BACKUP_DIR):
        return web.json_response({"backups": [], "count": 0})

    backups = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        bpath = os.path.join(BACKUP_DIR, name)
        if os.path.isdir(bpath):
            size = sum(os.path.getsize(os.path.join(dp, f))
                       for dp, _, fns in os.walk(bpath) for f in fns)
            backups.append({
                "id": name,
                "size_mb": round(size / 1048576, 1),
                "created": datetime.fromtimestamp(os.path.getctime(bpath), timezone.utc).isoformat(),
            })

    return web.json_response({"backups": backups, "count": len(backups)})


# =============================================================================
# WATCHDOG CONFIGURATION
# =============================================================================

async def handle_ops_watchdog_configure(request):
    """Configure watchdog auto-healing rules. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    rules = data.get("rules", [])
    if not rules:
        return web.json_response({"error": "rules array is required"}, status=400)

    # Validate rules
    for rule in rules:
        if not rule.get("service"):
            return web.json_response({"error": "Each rule needs a 'service' field"}, status=400)
        if rule.get("on_failure") not in ("restart", "alert", "ignore"):
            return web.json_response({"error": "on_failure must be 'restart', 'alert', or 'ignore'"}, status=400)

    # Save config
    os.makedirs(os.path.dirname(WATCHDOG_CONFIG), exist_ok=True)
    try:
        with open(WATCHDOG_CONFIG, "w") as f:
            json.dump({"rules": rules, "updated_at": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    except Exception as e:
        return web.json_response({"error": f"Failed to save config: {e}"}, status=500)

    agent_id = _get_agent_id(request)
    log.info("RED: Watchdog config updated: %d rules (by %s)", len(rules), agent_id)
    await _notify_agent_ops(request.app,
        f"🔴 **Watchdog config updated:** {len(rules)} rules — by `{agent_id}`")

    return web.json_response({"status": "configured", "rules_count": len(rules)})


# =============================================================================
# GOOGLE OAUTH
# =============================================================================

async def handle_ops_google_reauth(request):
    """Trigger Google OAuth token refresh. HITL-gated."""
    hitl = await _hitl_gate(request)
    if hitl:
        return hitl

    agent_id = _get_agent_id(request)
    log.info("RED: Google reauth requested by %s", agent_id)

    # Try to force-refresh the token
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleAuthRequest

        token_path = str(_TARS_HOME / ".config/google/token.json")
        if not os.path.exists(token_path):
            return web.json_response({"error": "No Google token file found"}, status=404)

        with open(token_path) as f:
            token_data = json.load(f)

        creds = Credentials.from_authorized_user_info(token_data)
        if creds.expired or not creds.valid:
            creds.refresh(GoogleAuthRequest())
            # Save refreshed token
            with open(token_path, "w") as f:
                json.dump(json.loads(creds.to_json()), f, indent=2)

            await _notify_agent_ops(request.app,
                f"🔴 **Google OAuth refreshed** — by `{agent_id}`")
            return web.json_response({
                "status": "refreshed",
                "valid": creds.valid,
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
            })
        else:
            return web.json_response({
                "status": "already_valid",
                "expiry": creds.expiry.isoformat() if creds.expiry else None,
            })

    except Exception as e:
        log.error("Google reauth failed: %s", e)
        if "refresh" in str(e).lower():
            return web.json_response({
                "status": "reauth_required",
                "message": "Refresh token expired. Manual re-authorization needed.",
                "error": str(e),
            }, status=401)
        return web.json_response({"error": str(e)}, status=500)


# =============================================================================
# ROUTE REGISTRATION
# =============================================================================

def register_red_routes(app, route_defs, get_agent_id_fn, notify_fn, save_vault_fn):
    """Register red tier routes."""
    app["_get_agent_id"] = get_agent_id_fn
    app["_notify_agent_ops"] = notify_fn
    app["route_defs"] = route_defs
    app["_save_vault"] = save_vault_fn

    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Secret management (HITL-gated)
    app.router.add_post("/ops/secret-set", handle_ops_secret_set)
    app.router.add_post("/ops/secret-delete", handle_ops_secret_delete)
    app.router.add_post("/ops/secret-inject", handle_ops_secret_inject)

    # Service lifecycle (HITL-gated)
    app.router.add_post("/ops/service-stop", handle_ops_service_stop)
    app.router.add_post("/ops/service-start", handle_ops_service_start)

    # Auth proxy route management (HITL-gated)
    app.router.add_post("/ops/auth-route-add", handle_ops_auth_route_add)
    app.router.add_post("/ops/auth-route-remove", handle_ops_auth_route_remove)

    # Docker management (HITL-gated)
    app.router.add_post("/ops/docker-prune", handle_ops_docker_prune)
    app.router.add_post("/ops/docker-build", handle_ops_docker_build)

    # System updates (HITL-gated)
    app.router.add_post("/ops/update-openclaw", handle_ops_update_openclaw)
    app.router.add_post("/ops/rebuild-sandbox", handle_ops_rebuild_sandbox)

    # Disk management (HITL-gated)
    app.router.add_post("/ops/disk-cleanup", handle_ops_disk_cleanup)

    # Backup operations (backup/restore HITL-gated, list is read-only)
    app.router.add_post("/ops/agent-backup", handle_ops_agent_backup)
    app.router.add_post("/ops/agent-restore", handle_ops_agent_restore)
    app.router.add_get("/ops/backups", handle_ops_backups_list)

    # Watchdog configuration (HITL-gated)
    app.router.add_post("/ops/watchdog-configure", handle_ops_watchdog_configure)

    # Google OAuth (HITL-gated)
    app.router.add_post("/ops/google-reauth", handle_ops_google_reauth)

    log.info("Red tier ops endpoints registered (16 endpoints, 15 HITL-gated + 1 read-only)")
