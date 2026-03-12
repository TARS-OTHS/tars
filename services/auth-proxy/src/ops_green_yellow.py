"""
Green + Yellow tier ops proxy endpoints.
Adds observability (read-only) and controlled mutation endpoints.

Green (no gates): health, services, containers, logs, disk, network,
    watchdog, auth-routes, versions, agent-status, agent-config (read),
    agent-workspace-list, secret-list
Yellow (cooldowns + audit): service-restart, container-restart,
    agent-message, agent-skill-install/remove, agent-workspace-init,
    tunnel-restart, tailscale-status, auth-route-test, agent-config (write)
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
import os as _os_param
from pathlib import Path as _Path_param
_TARS_HOME = _Path_param(_os_param.environ.get('TARS_HOME', str(_Path_param.home())))
_DOCKER_HOST_IP = _os_param.environ.get('DOCKER_HOST_IP', '172.17.0.1')
_MEMORY_API_PORT = _os_param.environ.get('MEMORY_API_PORT', '8897')
_AUTH_PROXY_PORT = _os_param.environ.get('AUTH_PROXY_PORT', '9100')
_NPM_GLOBAL_BIN = _os_param.environ.get('NPM_GLOBAL_BIN', str(_TARS_HOME / '.npm-global/bin'))
del _os_param, _Path_param


log = logging.getLogger("auth-proxy")

OPENCLAW_CONFIG = str(_TARS_HOME / ".openclaw/openclaw.json")
WORKSPACE_BASE = str(_TARS_HOME / ".openclaw/workspace")
AGENT_WORKSPACES = str(_TARS_HOME / ".openclaw")
SKILL_LIBRARY = str(_TARS_HOME / ".openclaw/skills")
OPENCLAW_BIN = os.path.join(_NPM_GLOBAL_BIN, "openclaw")


def _get_agent_id(request):
    """Get agent ID via app-stored helper."""
    fn = request.app.get("_get_agent_id")
    return fn(request) if fn else "unknown"


async def _notify_agent_ops(app, message):
    """Notify via app-stored helper."""
    fn = app.get("_notify_agent_ops")
    if fn:
        await fn(app, message)

# Allowlisted services for restart (Yellow tier)
RESTARTABLE_SERVICES = {
    "agent-services", "auth-proxy", "embedding-service",
    "openclaw-gateway", "openclaw-proxy", "openclaw-dashboard",
    "headless-chrome", "chrome-bridge",
}

# Services that cannot be stopped (critical infra)
PROTECTED_SERVICES = {"rescue-bot", "cloudflared", "port-forwarder"}

# Cooldown tracking for yellow-tier mutations
_restart_cooldowns = {}  # service_name -> last_restart_monotonic
RESTART_COOLDOWN_SECS = 60

# Secret pattern for log sanitization
_SECRET_PATTERNS = re.compile(
    r'(Bearer\s+\S+|token[=:]\s*\S+|key[=:]\s*\S+|password[=:]\s*\S+|'
    r'Authorization:\s*\S+|x-api-key:\s*\S+)',
    re.IGNORECASE
)


def _read_openclaw_config():
    """Read openclaw.json config."""
    try:
        with open(OPENCLAW_CONFIG) as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to read openclaw.json: %s", e)
        return None


def _get_agent_entries(config):
    """Get agent entries list from config (handles both 'entries' and 'list' keys)."""
    agents = config.get("agents", {})
    return agents.get("entries", agents.get("list", []))


def _resolve_workspace(agent, config=None):
    """Resolve workspace path for an agent."""
    if agent == "main":
        return WORKSPACE_BASE

    # Check if agent has explicit workspace in config
    if config:
        for entry in _get_agent_entries(config):
            if entry.get("id") == agent:
                ws = entry.get("workspace")
                if ws and os.path.isdir(ws):
                    return ws

    # Try standard patterns
    for pattern in [
        os.path.join(AGENT_WORKSPACES, f"workspace-{agent}"),
        os.path.join(AGENT_WORKSPACES, agent),
    ]:
        if os.path.isdir(pattern):
            return pattern

    return None


def _sanitize_log_line(line):
    """Redact secrets from log lines."""
    return _SECRET_PATTERNS.sub('[REDACTED]', line)


# ============================================================
# GREEN TIER — Read-only observability
# ============================================================

async def handle_ops_health(request):
    """System health overview — CPU, memory, disk, Docker."""
    try:
        # Uptime
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().split()[0])
        days = int(uptime_secs // 86400)
        hours = int((uptime_secs % 86400) // 3600)
        mins = int((uptime_secs % 3600) // 60)
        uptime_str = f"{days}d {hours}h {mins}m"

        # Load average
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load = [float(parts[0]), float(parts[1]), float(parts[2])]

        # Memory
        meminfo = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]  # kB value
                    meminfo[key] = int(val)
        total_mb = meminfo.get("MemTotal", 0) // 1024
        available_mb = meminfo.get("MemAvailable", 0) // 1024
        used_mb = total_mb - available_mb
        mem_pct = round(used_mb / total_mb * 100, 1) if total_mb else 0

        # Disk
        st = os.statvfs("/")
        disk_total = (st.f_blocks * st.f_frsize) // (1024**3)
        disk_avail = (st.f_bavail * st.f_frsize) // (1024**3)
        disk_used = disk_total - disk_avail
        disk_pct = round(disk_used / disk_total * 100, 1) if disk_total else 0

        # CPU count
        cpu_count = os.cpu_count() or 0

        # Docker containers
        docker_running = 0
        docker_total = 0
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-a", "--format", "{{.State}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            states = stdout.decode().strip().split("\n") if stdout.decode().strip() else []
            docker_total = len(states)
            docker_running = sum(1 for s in states if s == "running")
        except Exception:
            pass

        return web.json_response({
            "hostname": os.uname().nodename,
            "uptime": uptime_str,
            "uptime_seconds": int(uptime_secs),
            "load": load,
            "memory": {
                "total_mb": total_mb, "used_mb": used_mb,
                "available_mb": available_mb, "percent": mem_pct,
            },
            "disk": {
                "total_gb": disk_total, "used_gb": disk_used,
                "available_gb": disk_avail, "percent": disk_pct,
            },
            "cpu_count": cpu_count,
            "docker": {
                "containers_running": docker_running,
                "containers_total": docker_total,
            },
        })
    except Exception as e:
        log.error("Health check error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_services(request):
    """List all managed services and their status."""
    services = [
        "auth-proxy", "agent-services", "embedding-service",
        "openclaw-gateway", "openclaw-proxy", "openclaw-dashboard",
        "rescue-bot", "cloudflared", "port-forwarder",
        "headless-chrome", "chrome-bridge",
    ]
    results = []
    for svc in services:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "show", svc,
                "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            props = {}
            for line in stdout.decode().strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v
            results.append({
                "name": svc, "type": "systemd",
                "status": props.get("ActiveState", "unknown"),
                "sub_state": props.get("SubState", "unknown"),
                "pid": int(props.get("MainPID", "0")) or None,
                "started": props.get("ExecMainStartTimestamp", ""),
            })
        except Exception as e:
            results.append({"name": svc, "type": "systemd", "status": "error", "error": str(e)})

    # Also list Docker containers as services
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--format",
            '{"name":"{{.Names}}","status":"{{.State}}","image":"{{.Image}}","created":"{{.CreatedAt}}"}',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        for line in stdout.decode().strip().split("\n"):
            if line.strip():
                try:
                    c = json.loads(line)
                    c["type"] = "docker"
                    results.append(c)
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass

    return web.json_response({"services": results})


async def handle_ops_health_services(request):
    """Per-service health with memory, CPU, restart info."""
    services = [
        "auth-proxy", "agent-services", "embedding-service",
        "openclaw-gateway", "openclaw-proxy", "openclaw-dashboard",
        "rescue-bot", "headless-chrome", "chrome-bridge",
    ]
    results = []
    for svc in services:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "show", svc,
                "--property=ActiveState,MainPID,NRestarts,ExecMainStartTimestamp,MemoryCurrent",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            props = {}
            for line in stdout.decode().strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k] = v

            pid = int(props.get("MainPID", "0")) or None
            mem_bytes = props.get("MemoryCurrent", "")
            mem_mb = None
            if mem_bytes and mem_bytes != "[not set]":
                try:
                    mem_mb = round(int(mem_bytes) / (1024 * 1024), 1)
                except (ValueError, TypeError):
                    pass

            # CPU usage from /proc/pid/stat if pid available
            cpu_pct = None
            if pid:
                try:
                    with open(f"/proc/{pid}/stat") as f:
                        stat = f.read().split()
                    utime = int(stat[13])
                    stime = int(stat[14])
                    total_ticks = utime + stime
                    # Simple snapshot — not a rate, but gives relative usage
                    cpu_pct = round(total_ticks / os.sysconf("SC_CLK_TCK"), 1)
                except Exception:
                    pass

            results.append({
                "name": svc, "status": props.get("ActiveState", "unknown"),
                "pid": pid, "memory_mb": mem_mb, "cpu_seconds": cpu_pct,
                "restarts": props.get("NRestarts", "0"),
                "last_start": props.get("ExecMainStartTimestamp", ""),
            })
        except Exception as e:
            results.append({"name": svc, "status": "error", "error": str(e)})

    return web.json_response({"services": results})


async def handle_ops_containers(request):
    """List Docker containers with stats."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format",
            '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","mem_pct":"{{.MemPerc}}","net_io":"{{.NetIO}}","pids":"{{.PIDs}}"}',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        containers = []
        for line in stdout.decode().strip().split("\n"):
            if line.strip():
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        # Also get image and created info
        proc2 = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--format",
            '{"name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}","created":"{{.CreatedAt}}","id":"{{.ID}}"}',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
        info_map = {}
        for line in stdout2.decode().strip().split("\n"):
            if line.strip():
                try:
                    d = json.loads(line)
                    info_map[d["name"]] = d
                except json.JSONDecodeError:
                    pass

        # Merge
        for c in containers:
            info = info_map.get(c["name"], {})
            c["image"] = info.get("image", "")
            c["status"] = info.get("status", "")
            c["created"] = info.get("created", "")
            c["id"] = info.get("id", "")

        return web.json_response({"containers": containers})
    except Exception as e:
        log.error("Container stats error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_logs(request):
    """Tail logs for a service. Sanitizes secrets."""
    service = request.query.get("service", "")
    lines = min(int(request.query.get("lines", "50")), 500)
    since = request.query.get("since", "1h")

    # Allowlisted services
    allowed = RESTARTABLE_SERVICES | {"rescue-bot", "cloudflared", "port-forwarder"}
    if service not in allowed:
        return web.json_response({
            "error": f"Unknown service: {service}",
            "available": sorted(allowed),
        }, status=400)

    try:
        cmd = ["journalctl", "--user", "-u", service, f"--since=-{since}",
               "-n", str(lines), "--no-pager", "-o", "short-iso"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        raw_lines = stdout.decode().strip().split("\n")
        sanitized = [_sanitize_log_line(l) for l in raw_lines if l.strip()]
        return web.json_response({
            "service": service, "lines": sanitized,
            "count": len(sanitized), "truncated": len(raw_lines) >= lines,
        })
    except Exception as e:
        log.error("Log read error for %s: %s", service, e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_disk(request):
    """Disk usage breakdown."""
    try:
        st = os.statvfs("/")
        disk_total = (st.f_blocks * st.f_frsize) // (1024**3)
        disk_avail = (st.f_bavail * st.f_frsize) // (1024**3)
        disk_used = disk_total - disk_avail

        breakdown = {}

        # Docker disk usage
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "system", "df", "--format",
                '{"type":"{{.Type}}","size":"{{.Size}}","reclaimable":"{{.Reclaimable}}"}',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            for line in stdout.decode().strip().split("\n"):
                if line.strip():
                    try:
                        d = json.loads(line)
                        breakdown[f"docker_{d['type'].lower()}"] = d["size"]
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception:
            pass

        # Key directories
        dirs_to_check = {
            "memory_db": str(_TARS_HOME / "agent-services/memory.db"),
            "agent_workspaces": "str(_TARS_HOME / .openclaw/workspace)",
            "logs": str(_TARS_HOME / ".rescue-bot/logs"),
            "memory_backups": str(_TARS_HOME / "agent-services/backups"),
        }
        for label, path in dirs_to_check.items():
            try:
                if os.path.isfile(path):
                    size = os.path.getsize(path)
                    breakdown[label] = f"{size / (1024*1024):.1f} MB"
                elif os.path.isdir(path):
                    total = sum(
                        os.path.getsize(os.path.join(dp, f))
                        for dp, _, fnames in os.walk(path)
                        for f in fnames
                    )
                    if total > 1024**3:
                        breakdown[label] = f"{total / (1024**3):.1f} GB"
                    else:
                        breakdown[label] = f"{total / (1024**2):.1f} MB"
            except Exception:
                pass

        return web.json_response({
            "total_gb": disk_total, "used_gb": disk_used,
            "available_gb": disk_avail,
            "percent": round(disk_used / disk_total * 100, 1) if disk_total else 0,
            "breakdown": breakdown,
        })
    except Exception as e:
        log.error("Disk check error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_network(request):
    """Network connectivity status."""
    result = {
        "internet": False, "dns_resolving": False,
        "tailscale": {"connected": False},
        "cloudflare_tunnel": {"connected": False},
        "docker_bridge": {},
    }

    # Internet check
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "--connect-timeout", "3", "https://api.cloudflare.com/cdn-cgi/trace",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        code = stdout.decode().strip()
        result["internet"] = code.startswith("2") or code.startswith("3")
    except Exception:
        pass

    # DNS check
    try:
        proc = await asyncio.create_subprocess_exec(
            "dig", "+short", "google.com", "@1.1.1.1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        result["dns_resolving"] = bool(stdout.decode().strip())
    except Exception:
        pass

    # Tailscale
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ts = json.loads(stdout.decode())
        self_node = ts.get("Self", {})
        result["tailscale"] = {
            "connected": ts.get("BackendState") == "Running",
            "ip": self_node.get("TailscaleIPs", [None])[0],
            "hostname": self_node.get("HostName", ""),
        }
    except Exception:
        pass

    # Cloudflare tunnel
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "is-active", "cloudflared",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        result["cloudflare_tunnel"]["connected"] = stdout.decode().strip() == "active"
    except Exception:
        pass

    # Docker bridge
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "network", "inspect", "bridge", "--format",
            "{{range .IPAM.Config}}{{.Gateway}}{{end}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        gw = stdout.decode().strip()
        proc2 = await asyncio.create_subprocess_exec(
            "docker", "ps", "-q",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=3)
        container_count = len(stdout2.decode().strip().split("\n")) if stdout2.decode().strip() else 0
        result["docker_bridge"] = {"ip": gw, "containers": container_count}
    except Exception:
        pass

    # Active listening ports
    try:
        proc = await asyncio.create_subprocess_exec(
            "ss", "-tlnp",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        ports = []
        for line in stdout.decode().strip().split("\n")[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 4:
                local = parts[3]
                ports.append(local)
        result["listening_ports"] = ports
    except Exception:
        pass

    return web.json_response(result)


async def handle_ops_watchdog(request):
    """Health check of all critical services with failure detection."""
    checks = []
    overall = "healthy"

    # Check systemd services
    critical_services = [
        "auth-proxy", "agent-services", "embedding-service",
        "openclaw-gateway", "openclaw-proxy", "rescue-bot",
        "cloudflared", "openclaw-dashboard",
    ]
    for svc in critical_services:
        check = {"service": svc, "healthy": False}
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "is-active", svc,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            state = stdout.decode().strip()
            check["healthy"] = state == "active"
            if not check["healthy"]:
                check["error"] = f"state: {state}"
                overall = "degraded"
        except Exception as e:
            check["error"] = str(e)
            overall = "degraded"
        checks.append(check)

    # Check Tailscale
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ts = json.loads(stdout.decode())
        ts_healthy = ts.get("BackendState") == "Running"
        checks.append({
            "service": "tailscale", "healthy": ts_healthy,
            "ip": ts.get("Self", {}).get("TailscaleIPs", [None])[0],
        })
        if not ts_healthy:
            overall = "degraded"
    except Exception as e:
        checks.append({"service": "tailscale", "healthy": False, "error": str(e)})
        overall = "degraded"

    return web.json_response({"status": overall, "checks": checks})


async def handle_ops_auth_routes(request):
    """List all auth proxy routes with basic info."""
    route_defs = request.app.get("_route_defs", {})
    routes = []
    for prefix, defn in route_defs.items():
        routes.append({
            "path": f"/{prefix}/",
            "upstream": defn.get("upstream", ""),
        })
    return web.json_response({"routes": routes})


async def handle_ops_versions(request):
    """Check versions of all components."""
    versions = {}

    # OpenClaw
    try:
        proc = await asyncio.create_subprocess_exec(
            "openclaw", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        versions["openclaw"] = stdout.decode().strip()
    except Exception:
        versions["openclaw"] = "unknown"

    # Node
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        versions["node"] = stdout.decode().strip()
    except Exception:
        versions["node"] = "unknown"

    # Python
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        versions["python"] = stdout.decode().strip()
    except Exception:
        versions["python"] = "unknown"

    # Docker
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        versions["docker"] = stdout.decode().strip()
    except Exception:
        versions["docker"] = "unknown"

    # Sandbox image
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", "openclaw-sandbox:upgraded",
            "--format", "{{.Created}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        versions["sandbox_image_built"] = stdout.decode().strip()[:19]
    except Exception:
        versions["sandbox_image_built"] = "unknown"

    # Memory DB size
    try:
        db_path = str(_TARS_HOME / "agent-services/memory.db")
        if os.path.exists(db_path):
            size = os.path.getsize(db_path)
            versions["memory_db_size"] = f"{size / (1024*1024):.1f} MB"
    except Exception:
        pass

    # OS
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    versions["os"] = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass

    return web.json_response(versions)


async def handle_ops_agent_status(request):
    """Get an agent's current state — container, session, memory stats."""
    agent = request.query.get("agent", "")
    if not agent:
        return web.json_response({"error": "agent parameter required"}, status=400)

    result = {"agent": agent}

    # Container status
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "--filter", f"name=openclaw-sbx-agent-{agent}",
            "--format", '{"status":"{{.State}}","created":"{{.CreatedAt}}","image":"{{.Image}}"}',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        line = stdout.decode().strip()
        if line:
            try:
                result["container"] = json.loads(line.split("\n")[0])
            except json.JSONDecodeError:
                result["container"] = {"raw": line}
        else:
            result["container"] = {"status": "not found"}
    except Exception as e:
        result["container"] = {"error": str(e)}

    # Session state from memory API
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", f"http://{_DOCKER_HOST_IP}:{_MEMORY_API_PORT}/memory/session-state/{agent}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        try:
            session_data = json.loads(stdout.decode())
            result["session"] = session_data
        except json.JSONDecodeError:
            result["session"] = {"raw": stdout.decode()[:500]}
    except Exception as e:
        result["session"] = {"error": str(e)}

    # Memory count
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", f"http://{_DOCKER_HOST_IP}:{_MEMORY_API_PORT}/memory/search?q=*&agent={agent}&limit=1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        try:
            mem_data = json.loads(stdout.decode())
            result["memory_total"] = mem_data.get("total", 0)
        except json.JSONDecodeError:
            pass
    except Exception:
        pass

    return web.json_response(result)


async def handle_ops_agent_config_read(request):
    """Read an agent's current config from openclaw.json."""
    agent = request.query.get("agent", "")
    if not agent:
        return web.json_response({"error": "agent parameter required"}, status=400)

    config = _read_openclaw_config()
    if not config:
        return web.json_response({"error": "Failed to read config"}, status=500)

    for entry in _get_agent_entries(config):
        if entry.get("id") == agent:
            # Strip any sensitive fields
            safe = {k: v for k, v in entry.items()
                    if k not in ("discord_token", "token")}
            return web.json_response({"agent": agent, "config": safe})

    return web.json_response({"error": f"Agent '{agent}' not found"}, status=404)


async def handle_ops_agent_workspace_list(request):
    """List files in an agent's workspace (no content)."""
    agent = request.query.get("agent", "main")
    subpath = request.query.get("path", "/")

    # Resolve workspace path
    config = _read_openclaw_config()
    ws_path = _resolve_workspace(agent, config)

    if not ws_path or not os.path.isdir(ws_path):
        return web.json_response({"error": f"Workspace not found for agent '{agent}'"}, status=404)

    target = os.path.normpath(os.path.join(ws_path, subpath.lstrip("/")))
    # Security: ensure we stay within workspace
    if not target.startswith(ws_path):
        return web.json_response({"error": "Path traversal not allowed"}, status=403)

    if not os.path.isdir(target):
        return web.json_response({"error": "Not a directory"}, status=400)

    files = []
    try:
        for entry in sorted(os.listdir(target)):
            full = os.path.join(target, entry)
            try:
                st = os.stat(full)
                files.append({
                    "name": entry,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": st.st_size if os.path.isfile(full) else None,
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            except Exception:
                files.append({"name": entry, "type": "unknown"})
    except PermissionError:
        return web.json_response({"error": "Permission denied"}, status=403)

    return web.json_response({"agent": agent, "path": subpath, "files": files})


async def handle_ops_secret_list(request):
    """List secret names (never values). Available from containers."""
    secrets = request.app.get("secrets", {})
    scope = request.query.get("scope", "")

    items = []
    for key in sorted(secrets.keys()):
        if scope and not key.startswith(scope):
            continue
        items.append({"name": key})

    return web.json_response({"count": len(items), "secrets": items})


# ============================================================
# YELLOW TIER — Controlled mutations (cooldowns + audit)
# ============================================================

async def handle_ops_service_restart(request):
    """Restart an allowlisted service. 60s cooldown."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    service = data.get("service", "").strip()
    if not service:
        return web.json_response({"error": "service is required"}, status=400)

    if service not in RESTARTABLE_SERVICES:
        return web.json_response({
            "error": f"Service '{service}' not in allowlist",
            "allowed": sorted(RESTARTABLE_SERVICES),
        }, status=403)

    # Cooldown check
    now = time.monotonic()
    last = _restart_cooldowns.get(service, 0)
    if last > 0 and (now - last) < RESTART_COOLDOWN_SECS:
        remaining = int(RESTART_COOLDOWN_SECS - (now - last))
        return web.json_response({
            "error": f"Cooldown active. Try again in {remaining}s"
        }, status=429)

    
    agent_id = _get_agent_id(request)
    log.info("Service restart requested: %s by %s", service, agent_id)

    await _notify_agent_ops(request.app,
        f"**Service restarting:** `{service}` — requested by `{agent_id}`")

    _restart_cooldowns[service] = now

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", service,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip() or stdout.decode().strip()
            log.error("Service restart failed: %s — %s", service, error_msg)
            await _notify_agent_ops(request.app,
                f"**Service restart FAILED:** `{service}` — `{error_msg}`")
            return web.json_response({"error": error_msg}, status=500)

        log.info("Service %s restarted successfully", service)
        await _notify_agent_ops(request.app,
            f"**Service restarted:** `{service}` successfully")
        return web.json_response({"status": "restarted", "service": service})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Restart timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_container_restart(request):
    """Restart a Docker container. 60s cooldown per container."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    container = data.get("container", "").strip()
    timeout_secs = data.get("timeout_seconds", 30)
    if not container:
        return web.json_response({"error": "container name required"}, status=400)

    # Security: only allow openclaw containers
    if not container.startswith("openclaw-"):
        return web.json_response({
            "error": "Can only restart openclaw-* containers"
        }, status=403)

    # Cooldown
    key = f"container:{container}"
    now = time.monotonic()
    last = _restart_cooldowns.get(key, 0)
    if last > 0 and (now - last) < RESTART_COOLDOWN_SECS:
        remaining = int(RESTART_COOLDOWN_SECS - (now - last))
        return web.json_response({
            "error": f"Cooldown active. Try again in {remaining}s"
        }, status=429)

    
    agent_id = _get_agent_id(request)
    log.info("Container restart requested: %s by %s", container, agent_id)

    await _notify_agent_ops(request.app,
        f"**Container restarting:** `{container}` — requested by `{agent_id}`")

    _restart_cooldowns[key] = now

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", "-t", str(timeout_secs), container,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs + 10)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            await _notify_agent_ops(request.app,
                f"**Container restart FAILED:** `{container}` — `{error_msg}`")
            return web.json_response({"error": error_msg}, status=500)

        await _notify_agent_ops(request.app,
            f"**Container restarted:** `{container}` successfully")
        return web.json_response({"status": "restarted", "container": container})

    except asyncio.TimeoutError:
        return web.json_response({"error": "Restart timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_agent_message(request):
    """Send a message to another agent via the OpenClaw sessions_send."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip()
    message = data.get("message", "").strip()
    if not agent or not message:
        return web.json_response({"error": "agent and message required"}, status=400)

    
    sender = _get_agent_id(request)
    log.info("Agent message: %s -> %s: %s", sender, agent, message[:100])

    # Use OpenClaw agent command to send message to another agent
    try:
        proc = await asyncio.create_subprocess_exec(
            OPENCLAW_BIN, "agent", "--agent", agent, "-m", message, "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            error = stderr.decode().strip() or stdout.decode().strip()
            return web.json_response({"error": f"Send failed: {error}"}, status=500)

        # Try to parse JSON response
        response_text = stdout.decode().strip()
        try:
            response_data = json.loads(response_text)
        except json.JSONDecodeError:
            response_data = {"raw": response_text[:500]}

        return web.json_response({
            "status": "sent", "from": sender, "to": agent,
            "message_preview": message[:100],
            "response": response_data,
        })
    except asyncio.TimeoutError:
        return web.json_response({"error": "Send timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_agent_skill_install(request):
    """Install a skill into an agent's workspace."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip()
    skill = data.get("skill", "").strip()
    if not agent or not skill:
        return web.json_response({"error": "agent and skill required"}, status=400)

    # Find skill in library
    skill_src = os.path.join(SKILL_LIBRARY, skill)
    if not os.path.isdir(skill_src):
        # List available skills
        available = []
        if os.path.isdir(SKILL_LIBRARY):
            available = [d for d in os.listdir(SKILL_LIBRARY)
                        if os.path.isdir(os.path.join(SKILL_LIBRARY, d))]
        return web.json_response({
            "error": f"Skill '{skill}' not found in library",
            "available": available,
        }, status=404)

    # Resolve workspace
    config = _read_openclaw_config()
    ws = _resolve_workspace(agent, config)
    if not ws or not os.path.isdir(ws):
        return web.json_response({"error": f"Workspace not found for '{agent}'"}, status=404)

    skill_dest = os.path.join(ws, "skills", skill)
    try:
        os.makedirs(os.path.join(ws, "skills"), exist_ok=True)
        if os.path.exists(skill_dest):
            shutil.rmtree(skill_dest)
        shutil.copytree(skill_src, skill_dest)

        
        installer = _get_agent_id(request)
        log.info("Skill '%s' installed for agent '%s' by %s", skill, agent, installer)
        return web.json_response({
            "status": "installed", "skill": skill, "agent": agent,
            "path": skill_dest,
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_agent_skill_remove(request):
    """Remove a skill from an agent's workspace."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip()
    skill = data.get("skill", "").strip()
    if not agent or not skill:
        return web.json_response({"error": "agent and skill required"}, status=400)

    config = _read_openclaw_config()
    ws = _resolve_workspace(agent, config)
    if not ws:
        return web.json_response({"error": f"Workspace not found for '{agent}'"}, status=404)

    skill_path = os.path.join(ws, "skills", skill)
    if not os.path.exists(skill_path):
        return web.json_response({"error": f"Skill '{skill}' not installed for '{agent}'"}, status=404)

    try:
        shutil.rmtree(skill_path)
        log.info("Skill '%s' removed from agent '%s'", skill, agent)
        return web.json_response({"status": "removed", "skill": skill, "agent": agent})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_agent_workspace_init(request):
    """Initialize a new agent's workspace from templates."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip()
    agent_name = data.get("agent_name", agent.capitalize())
    owner_name = data.get("owner_name", "Peter")
    role = data.get("role", "assistant")

    if not agent:
        return web.json_response({"error": "agent is required"}, status=400)

    config = _read_openclaw_config()
    ws = _resolve_workspace(agent, config)
    if not ws:
        ws = os.path.join(AGENT_WORKSPACES, f"workspace-{agent}")
    os.makedirs(ws, exist_ok=True)

    # Create basic workspace files from templates
    templates = {
        "SOUL.md": f"# {agent_name}\n\nYou are {agent_name}, a {role} agent.\n\n## Owner\n{owner_name}\n",
        "AGENTS.md": f"# Agent Configuration\n\n## {agent_name}\n- **Role:** {role}\n- **Owner:** {owner_name}\n",
        "TOOLS.md": "# Available Tools\n\nSee auth proxy routes: `curl -s http://{_DOCKER_HOST_IP}:{_AUTH_PROXY_PORT}/ | jq .`\n",
        "MEMORY.md": f"# {agent_name} Memory\n\n_No memories yet._\n",
    }

    created = []
    skipped = []
    for fname, content in templates.items():
        fpath = os.path.join(ws, fname)
        if os.path.exists(fpath):
            skipped.append(fname)
        else:
            with open(fpath, "w") as f:
                f.write(content)
            created.append(fname)

    # Create standard directories
    for d in ["skills", "scripts", "docs", "kb"]:
        os.makedirs(os.path.join(ws, d), exist_ok=True)

    log.info("Workspace initialized for agent '%s': created=%s, skipped=%s",
             agent, created, skipped)
    return web.json_response({
        "status": "initialized", "agent": agent,
        "workspace": ws, "created": created, "skipped": skipped,
    })


async def handle_ops_tunnel_restart(request):
    """Restart Cloudflare tunnel."""
    now = time.monotonic()
    last = _restart_cooldowns.get("cloudflared", 0)
    if last > 0 and (now - last) < RESTART_COOLDOWN_SECS:
        remaining = int(RESTART_COOLDOWN_SECS - (now - last))
        return web.json_response({"error": f"Cooldown. Try in {remaining}s"}, status=429)

    _restart_cooldowns["cloudflared"] = now

    
    agent_id = _get_agent_id(request)
    log.info("Tunnel restart requested by %s", agent_id)

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "cloudflared",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            return web.json_response({"error": stderr.decode().strip()}, status=500)

        await _notify_agent_ops(request.app,
            f"**Cloudflare tunnel restarted** by `{agent_id}`")
        return web.json_response({"status": "restarted"})
    except asyncio.TimeoutError:
        return web.json_response({"error": "Restart timed out"}, status=504)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_tailscale_status(request):
    """Check Tailscale status, attempt reconnect if down."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        data = json.loads(stdout.decode())
        self_node = data.get("Self", {})
        connected = data.get("BackendState") == "Running"

        result = {
            "connected": connected,
            "ip": self_node.get("TailscaleIPs", [None])[0],
            "hostname": self_node.get("HostName", ""),
            "backend_state": data.get("BackendState", ""),
        }

        # If not connected and this is a POST, try to bring it up
        if request.method == "POST" and not connected:
            try:
                up_proc = await asyncio.create_subprocess_exec(
                    "sudo", "tailscale", "up",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(up_proc.communicate(), timeout=15)
                result["action"] = "reconnect attempted"
            except Exception as e:
                result["action"] = f"reconnect failed: {e}"

        return web.json_response(result)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_ops_auth_route_test(request):
    """Test that an auth proxy route works (lightweight probe)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    path = data.get("path", "").strip().strip("/")
    test_endpoint = data.get("test_endpoint", "/")

    route_defs = request.app.get("_route_defs", {})
    if path not in route_defs:
        return web.json_response({
            "error": f"Route '{path}' not found",
            "available": sorted(route_defs.keys()),
        }, status=404)

    # Make a lightweight HEAD/GET request through the proxy
    defn = route_defs[path]
    upstream = defn["upstream"].rstrip("/") + test_endpoint
    headers = {}
    params = {}
    ctx = {"secrets": request.app["secrets"]}
    defn["auth"](headers, params, ctx)

    try:
        session = request.app["client_session"]
        async with session.head(upstream, headers=headers, allow_redirects=False) as resp:
            return web.json_response({
                "route": f"/{path}/",
                "upstream": defn["upstream"],
                "test_endpoint": test_endpoint,
                "status": resp.status,
                "healthy": resp.status < 500,
            })
    except Exception as e:
        return web.json_response({
            "route": f"/{path}/",
            "upstream": defn["upstream"],
            "test_endpoint": test_endpoint,
            "status": 0,
            "healthy": False,
            "error": str(e),
        })


async def handle_ops_agent_config_write(request):
    """Update an agent's config in openclaw.json. Yellow tier — audit + notify."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    agent = data.get("agent", "").strip()
    config_updates = data.get("config", {})
    if not agent or not config_updates:
        return web.json_response({"error": "agent and config required"}, status=400)

    # Cannot modify main agent's config from containers
    if agent == "main":
        return web.json_response({"error": "Cannot modify main agent config"}, status=403)

    # Blocked fields — security-sensitive
    blocked_fields = {"discord_token", "token", "id"}
    bad_fields = set(config_updates.keys()) & blocked_fields
    if bad_fields:
        return web.json_response({
            "error": f"Cannot modify fields: {', '.join(bad_fields)}"
        }, status=403)

    config = _read_openclaw_config()
    if not config:
        return web.json_response({"error": "Failed to read config"}, status=500)

    entries = _get_agent_entries(config)
    found = False
    for entry in entries:
        if entry.get("id") == agent:
            for k, v in config_updates.items():
                if k not in blocked_fields:
                    entry[k] = v
            found = True
            break

    if not found:
        return web.json_response({"error": f"Agent '{agent}' not found"}, status=404)

    try:
        with open(OPENCLAW_CONFIG, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        return web.json_response({"error": f"Failed to write config: {e}"}, status=500)

    
    requester = _get_agent_id(request)
    log.info("Agent config updated: %s by %s — %s", agent, requester, list(config_updates.keys()))
    await _notify_agent_ops(request.app,
        f"**Agent config updated:** `{agent}` — fields: {', '.join(config_updates.keys())} (by `{requester}`)")

    return web.json_response({
        "status": "updated", "agent": agent,
        "fields_updated": list(config_updates.keys()),
        "note": "Gateway restart may be needed for changes to take effect",
    })


# ============================================================
# Route registration helper
# ============================================================

def register_green_yellow_routes(app, route_defs, get_agent_id_fn, notify_fn):
    """Register all Green + Yellow tier routes on the aiohttp app.

    Args:
        app: aiohttp Application
        route_defs: dict of route prefix -> {upstream, auth} definitions
        get_agent_id_fn: function(request) -> agent_id string
        notify_fn: async function(app, message) -> None
    """
    # Store refs so handlers can access them
    app["_route_defs"] = route_defs
    app["_get_agent_id"] = get_agent_id_fn
    app["_notify_agent_ops"] = notify_fn

    # --- Green tier (GET only, read-only) ---
    app.router.add_get("/ops/health", handle_ops_health)
    app.router.add_get("/ops/services", handle_ops_services)
    app.router.add_get("/ops/health/services", handle_ops_health_services)
    app.router.add_get("/ops/containers", handle_ops_containers)
    app.router.add_get("/ops/logs", handle_ops_logs)
    app.router.add_get("/ops/disk", handle_ops_disk)
    app.router.add_get("/ops/network", handle_ops_network)
    app.router.add_get("/ops/watchdog", handle_ops_watchdog)
    app.router.add_get("/ops/auth-routes", handle_ops_auth_routes)
    app.router.add_get("/ops/versions", handle_ops_versions)
    app.router.add_get("/ops/agent-status", handle_ops_agent_status)
    app.router.add_get("/ops/agent-config", handle_ops_agent_config_read)
    app.router.add_get("/ops/agent-workspace-list", handle_ops_agent_workspace_list)
    app.router.add_get("/ops/secret-list", handle_ops_secret_list)

    # --- Yellow tier (POST, mutations with cooldowns) ---
    app.router.add_post("/ops/service-restart", handle_ops_service_restart)
    app.router.add_post("/ops/container-restart", handle_ops_container_restart)
    app.router.add_post("/ops/agent-message", handle_ops_agent_message)
    app.router.add_post("/ops/agent-skill-install", handle_ops_agent_skill_install)
    app.router.add_post("/ops/agent-skill-remove", handle_ops_agent_skill_remove)
    app.router.add_post("/ops/agent-workspace-init", handle_ops_agent_workspace_init)
    app.router.add_post("/ops/tunnel-restart", handle_ops_tunnel_restart)
    app.router.add_get("/ops/tailscale-status", handle_ops_tailscale_status)
    app.router.add_post("/ops/tailscale-status", handle_ops_tailscale_status)
    app.router.add_post("/ops/auth-route-test", handle_ops_auth_route_test)
    app.router.add_post("/ops/agent-config", handle_ops_agent_config_write)

    log.info("Green + Yellow tier ops endpoints registered (14 green, 11 yellow)")
