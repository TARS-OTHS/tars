"""System health audit tool for T.A.R.S deployments.

Runs checks against a per-deployment config file defining expected
services, timers, thresholds, and security baselines. Returns a
structured pass/warn/fail report.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)


async def _run(cmd: str, timeout: int = 10) -> tuple[str, int]:
    """Run a shell command and return (stdout, returncode)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode().strip(), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "", -1


@tool(
    name="system_audit",
    description="Run a full system health audit against a deployment config file",
    category="ops",
)
async def system_audit(ctx: ToolContext, config_path: str = "") -> str:
    """Run system health checks defined in a deployment config file.

    Args:
        config_path: Path to health-config.yaml. Defaults to
                     $TARS_OVERLAY/config/health-config.yaml.
    """
    if not config_path:
        overlay = os.environ.get("TARS_OVERLAY", "")
        if overlay:
            config_path = os.path.join(overlay, "config", "health-config.yaml")
        else:
            return "ERROR: No config_path provided and TARS_OVERLAY not set"

    config_file = Path(config_path)
    if not config_file.exists():
        return f"ERROR: Config not found at {config_path}"

    with open(config_file) as f:
        config = yaml.safe_load(f)

    results: list[str] = []
    passed = warned = failed = 0

    def ok(msg: str) -> None:
        nonlocal passed
        passed += 1
        results.append(f"  PASS  {msg}")

    def warn(msg: str) -> None:
        nonlocal warned
        warned += 1
        results.append(f"  WARN  {msg}")

    def fail(msg: str) -> None:
        nonlocal failed
        failed += 1
        results.append(f"  FAIL  {msg}")

    # ── 1. Services ──────────────────────────────────────────────
    results.append("\n## Services")
    for svc in config.get("services", []):
        out, _ = await _run(f"systemctl is-active {svc}")
        if out == "active":
            ok(f"`{svc}` running")
        else:
            fail(f"`{svc}` NOT running (status: {out})")

    # Flapping detection (restart count in 24h)
    for svc in config.get("services", []):
        out, _ = await _run(
            f"systemctl show {svc} --property=NRestarts --value 2>/dev/null"
        )
        try:
            restarts = int(out)
            if restarts > 5:
                warn(f"`{svc}` has restarted {restarts} times (lifetime)")
        except ValueError:
            pass

    # ── 2. Timers ────────────────────────────────────────────────
    results.append("\n## Timers")
    # Auto-discover tars-* timers from systemd
    out, _ = await _run(
        "systemctl list-units --type=timer --no-legend --plain 'tars-*' "
        "2>/dev/null | awk '{print $1}'"
    )
    live_timers = [t.replace(".timer", "") for t in out.splitlines() if t.strip()] if out else []
    config_timers = config.get("timers", [])

    # Check all discovered timers are active
    for timer in live_timers:
        out, _ = await _run(
            f"systemctl show {timer}.timer --property=ActiveState --value"
        )
        if out == "active":
            ok(f"`{timer}.timer` active")
        else:
            fail(f"`{timer}.timer` NOT active (state: {out})")

    # Warn about config timers that no longer exist in systemd
    for timer in config_timers:
        if timer not in live_timers:
            warn(f"`{timer}.timer` in config but not found in systemd — config may be stale")

    if not live_timers:
        warn("No tars-* timers found in systemd")

    # ── 3. Resources ─────────────────────────────────────────────
    results.append("\n## Resources")
    thresholds = config.get("thresholds", {})

    # Disk
    disk_thresh = thresholds.get("disk_percent", 80)
    out, _ = await _run("df / --output=pcent | tail -1 | tr -d ' %'")
    try:
        disk_pct = int(out)
        if disk_pct > disk_thresh:
            fail(f"Disk: {disk_pct}% (threshold: {disk_thresh}%)")
        elif disk_pct > disk_thresh - 10:
            warn(f"Disk: {disk_pct}% (approaching {disk_thresh}% threshold)")
        else:
            ok(f"Disk: {disk_pct}%")
    except ValueError:
        warn("Disk: could not parse usage")

    # RAM
    ram_thresh = thresholds.get("ram_percent", 85)
    out, _ = await _run(
        "free | awk '/Mem:/ {printf \"%.0f\", ($2-$7)/$2*100}'"
    )
    try:
        ram_pct = int(out)
        if ram_pct > ram_thresh:
            fail(f"RAM: {ram_pct}% (threshold: {ram_thresh}%)")
        elif ram_pct > ram_thresh - 10:
            warn(f"RAM: {ram_pct}% (approaching {ram_thresh}% threshold)")
        else:
            ok(f"RAM: {ram_pct}%")
    except ValueError:
        warn("RAM: could not parse usage")

    # Swap
    swap_thresh = thresholds.get("swap_percent", 70)
    out, _ = await _run(
        "free | awk '/Swap:/ {if ($2>0) printf \"%.0f\", $3/$2*100; else print \"0\"}'"
    )
    try:
        swap_pct = int(out)
        if swap_pct > swap_thresh:
            fail(f"Swap: {swap_pct}% (threshold: {swap_thresh}%)")
        else:
            ok(f"Swap: {swap_pct}%")
    except ValueError:
        warn("Swap: could not parse usage")

    # Load
    load_thresh = thresholds.get("load_per_cpu", 1.5)
    cpus_out, _ = await _run("nproc")
    load_out, _ = await _run("awk '{print $1}' /proc/loadavg")
    try:
        cpus = int(cpus_out)
        load = float(load_out)
        max_load = cpus * load_thresh
        if load > max_load:
            fail(f"Load: {load} (threshold: {max_load} for {cpus} CPUs)")
        else:
            ok(f"Load: {load} ({cpus} CPUs)")
    except ValueError:
        warn("Load: could not parse")

    # Zombies
    out, _ = await _run("ps aux | awk '$8 ~ /Z/' | wc -l")
    try:
        zombies = int(out)
        if zombies > 0:
            warn(f"Zombie processes: {zombies}")
        else:
            ok("No zombie processes")
    except ValueError:
        pass

    # Uptime
    out, _ = await _run("uptime -p")
    ok(f"Uptime: {out}")

    # ── 4. Network ───────────────────────────────────────────────
    results.append("\n## Network")

    if config.get("tailscale", False):
        out, _ = await _run("systemctl is-active tailscaled.service")
        if out == "active":
            ok("Tailscale service running")
        else:
            fail("Tailscale NOT running")

    if config.get("cloudflared", False):
        out, _ = await _run("systemctl is-active cloudflared.service")
        if out == "active":
            ok("Cloudflare tunnel running")
        else:
            fail("Cloudflare tunnel NOT running")

    # DNS resolution
    out, rc = await _run("getent hosts github.com >/dev/null 2>&1 && echo ok")
    if "ok" in (out or ""):
        ok("DNS resolution working")
    else:
        fail("DNS resolution failed (github.com)")

    # ── 5. Security ──────────────────────────────────────────────
    results.append("\n## Security")
    paths = config.get("paths", {})
    tars_home = paths.get("tars_home", "/opt/tars-v2")

    # File ownership
    out, _ = await _run(
        f"find {tars_home} -not -user tars -not -path '*/.git/*' 2>/dev/null | head -5"
    )
    if out:
        fail(f"Files not owned by tars:\n{out}")
    else:
        ok(f"All `{tars_home}` files owned by tars:tars")

    # Git hooks
    for hook in config.get("git_hooks", []):
        hook_path = Path(tars_home) / ".git" / "hooks" / hook
        if hook_path.exists() and os.access(hook_path, os.X_OK):
            ok(f"Git hook `{hook}` present + executable")
        elif hook_path.exists():
            warn(f"Git hook `{hook}` present but NOT executable")
        else:
            fail(f"Git hook `{hook}` MISSING")

    # Failed systemd units
    out, _ = await _run(
        "systemctl list-units --state=failed --no-legend --plain | head -5"
    )
    if out:
        fail(f"Failed systemd units:\n{out}")
    else:
        ok("No failed systemd units")

    # ── 6. Memory System ────────────────────────────────────────
    results.append("\n## Memory System")

    db_path = paths.get(
        "memory_db", os.path.join(tars_home, "data", "memory.db")
    )
    if Path(db_path).exists():
        # Basic read
        out, rc = await _run(
            f"sqlite3 '{db_path}' 'SELECT COUNT(*) FROM memories;' 2>/dev/null"
        )
        if rc == 0:
            ok(f"Memory DB readable ({out} memories)")
        else:
            fail("Memory DB exists but query failed")

        # Integrity check
        out, rc = await _run(
            f"sqlite3 '{db_path}' 'PRAGMA integrity_check;' 2>/dev/null"
        )
        if rc == 0 and out.strip() == "ok":
            ok("Memory DB integrity check passed")
        else:
            fail(f"Memory DB integrity check: {out}")

        # WAL size (large WAL = missed checkpoints)
        wal_path = db_path + "-wal"
        if Path(wal_path).exists():
            wal_mb = Path(wal_path).stat().st_size / (1024 * 1024)
            if wal_mb > 50:
                warn(f"Memory DB WAL is {wal_mb:.1f}MB — consider PRAGMA wal_checkpoint")
            else:
                ok(f"Memory DB WAL: {wal_mb:.1f}MB")

        # FTS5 index health
        out, rc = await _run(
            f"sqlite3 '{db_path}' \"SELECT COUNT(*) FROM memories_fts;\" 2>/dev/null"
        )
        if rc == 0:
            ok(f"FTS5 index healthy ({out} entries)")
        else:
            warn("FTS5 index missing or corrupt — keyword search may fail")

        # Memory stats
        out, rc = await _run(
            f"sqlite3 '{db_path}' \""
            "SELECT "
            "  COUNT(*), "
            "  SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END)"
            " FROM memories;\" 2>/dev/null"
        )
        if rc == 0 and out:
            parts = out.split("|")
            if len(parts) == 3:
                ok(f"Memories — total: {parts[0]}, pinned: {parts[1]}, with embeddings: {parts[2]}")

        # Embeddings model
        model_dir = paths.get(
            "embeddings_model",
            os.path.join(
                os.environ.get("TARS_OVERLAY", os.path.join(tars_home, "data")),
                "data", "models", "bge-small-en-v1.5",
            ),
        )
        if Path(model_dir).is_dir():
            ok("Embeddings model present")
        else:
            warn(f"Embeddings model not found at `{model_dir}` — semantic search disabled")
    else:
        fail(f"Memory DB NOT FOUND at `{db_path}`")

    # ── 7. Vault ─────────────────────────────────────────────────
    results.append("\n## Vault")

    vault_path = paths.get("vault", "")
    if vault_path and Path(vault_path).exists():
        vp = Path(vault_path)
        mode = oct(vp.stat().st_mode)[-3:]
        if mode == "600":
            ok(f"Vault present (mode {mode})")
        else:
            warn(f"Vault present but mode {mode} (expected 600)")

        # Salt file
        salt_path = vp.with_suffix(".salt")
        if salt_path.exists():
            ok("Per-instance salt present")
        else:
            warn("Using legacy hardcoded salt — run vault salt migration")

        # Vault unlock test
        key_file = Path.home() / ".config" / "tars-vault-key"
        if key_file.exists():
            out, rc = await _run(
                f"cd {tars_home} && {tars_home}/.venv/bin/python -c \""
                "from src.vault.fernet import FernetVault; "
                f"v = FernetVault('{vault_path}'); "
                f"v.unlock(open('{key_file}').read().strip()); "
                "keys = v.list_keys(); "
                "print('|'.join(sorted(keys)))"
                "\" 2>/dev/null"
            )
            if rc == 0 and out:
                key_list = out.split("|")
                ok(f"Vault unlocks OK ({len(key_list)} keys)")

                # Check expected keys
                expected_keys = config.get("vault_expected_keys", [])
                for ek in expected_keys:
                    if ek in key_list:
                        ok(f"Vault key `{ek}` present")
                    else:
                        fail(f"Vault key `{ek}` MISSING")
            elif rc == 0:
                warn("Vault unlocks but contains no keys")
            else:
                fail("Vault unlock FAILED — wrong passphrase or corrupted")
        else:
            warn("Vault key file not found — cannot test unlock")
    elif vault_path:
        fail(f"Vault NOT FOUND at `{vault_path}`")

    # ── 8. MCP Servers ───────────────────────────────────────────
    results.append("\n## MCP Servers")

    mcp_config_path = paths.get("mcp_config", "")
    if not mcp_config_path:
        overlay = paths.get("overlay", os.environ.get("TARS_OVERLAY", ""))
        if overlay:
            mcp_config_path = os.path.join(overlay, "config", "mcp.yaml")
        else:
            mcp_config_path = os.path.join(tars_home, "config", "mcp.yaml")

    if Path(mcp_config_path).exists():
        try:
            with open(mcp_config_path) as f:
                mcp_config = yaml.safe_load(f) or {}
            servers = mcp_config.get("servers", {})
            active_servers = {k: v for k, v in servers.items()
                             if not str(k).startswith("#")}
            ok(f"MCP config valid ({len(active_servers)} server(s) defined)")

            for name, srv in active_servers.items():
                transport = srv.get("transport", "")
                if transport == "sse" and srv.get("url"):
                    url = srv["url"]
                    out, rc = await _run(
                        f"curl --max-time 5 -o /dev/null -s -w '%{{http_code}}' "
                        f"'{url}' 2>/dev/null",
                        timeout=10000,
                    )
                    try:
                        status = int(out) if out else 0
                    except ValueError:
                        status = 0
                    if status > 0 and status < 500:
                        ok(f"MCP `{name}` reachable ({status})")
                    else:
                        warn(f"MCP `{name}` unreachable at `{url}`")
                elif transport == "stdio" and srv.get("command"):
                    cmd = srv["command"].split()[0]
                    out, rc = await _run(f"which {cmd} 2>/dev/null")
                    if rc == 0:
                        ok(f"MCP `{name}` command `{cmd}` found")
                    else:
                        warn(f"MCP `{name}` command `{cmd}` not found")
        except yaml.YAMLError as e:
            fail(f"MCP config YAML error: {e}")
    else:
        ok("No MCP config found (none configured)")

    # ── 9. Tools & Skills ────────────────────────────────────────
    results.append("\n## Tools & Skills")

    # Scan tool directories across all layers
    tool_dirs = [os.path.join(tars_home, "src", "tools")]

    # OTHS tool directories (Layer 2)
    oths_root = paths.get("oths", "")
    oths_env = os.environ.get("TARS_OTHS", "")
    if oths_root and Path(oths_root).is_dir():
        # Config-based: scan all subdirectories for tools/
        for subdir in sorted(Path(oths_root).iterdir()):
            tools_dir = subdir / "tools"
            if tools_dir.is_dir():
                tool_dirs.append(str(tools_dir))
    elif oths_env:
        # Env-based: each colon-separated path is a module root
        for oths_path in oths_env.split(":"):
            oths_path = oths_path.strip()
            if oths_path and Path(oths_path).is_dir():
                for subdir in Path(oths_path).iterdir():
                    tools_dir = subdir / "tools"
                    if tools_dir.is_dir():
                        tool_dirs.append(str(tools_dir))

    total_tools = 0
    for td in tool_dirs:
        if Path(td).is_dir():
            py_files = [f for f in Path(td).glob("*.py")
                        if not f.name.startswith("_")]
            total_tools += len(py_files)

            # Syntax check each tool file
            for py_file in py_files:
                out, rc = await _run(
                    f"{tars_home}/.venv/bin/python -c "
                    f"\"import ast; ast.parse(open('{py_file}').read())\" 2>&1"
                )
                if rc != 0:
                    fail(f"Tool `{py_file.name}` has syntax error: {out}")

    ok(f"{total_tools} tool files across {len(tool_dirs)} directories (syntax OK)")

    # Scan skill directories
    skill_dirs = [os.path.join(tars_home, "skills")]
    overlay = paths.get("overlay", os.environ.get("TARS_OVERLAY", ""))
    if overlay:
        overlay_skills = os.path.join(overlay, "skills")
        if Path(overlay_skills).is_dir():
            skill_dirs.append(overlay_skills)
    if oths_root and Path(oths_root).is_dir():
        for subdir in sorted(Path(oths_root).iterdir()):
            skills_dir = subdir / "skills"
            if skills_dir.is_dir():
                skill_dirs.append(str(skills_dir))
    elif oths_env:
        for oths_path in oths_env.split(":"):
            oths_path = oths_path.strip()
            if oths_path and Path(oths_path).is_dir():
                for subdir in Path(oths_path).iterdir():
                    skills_dir = subdir / "skills"
                    if skills_dir.is_dir():
                        skill_dirs.append(str(skills_dir))

    total_skills = 0
    for sd in skill_dirs:
        if Path(sd).is_dir():
            yaml_files = list(Path(sd).glob("*.yaml")) + list(Path(sd).glob("*.yml"))
            total_skills += len(yaml_files)

            for yf in yaml_files:
                try:
                    with open(yf) as f:
                        skill_data = yaml.safe_load(f)
                    if not skill_data or not isinstance(skill_data, dict):
                        warn(f"Skill `{yf.name}` is empty or invalid")
                    elif "name" not in skill_data:
                        warn(f"Skill `{yf.name}` missing 'name' field")
                except yaml.YAMLError as e:
                    fail(f"Skill `{yf.name}` YAML error: {e}")

    ok(f"{total_skills} skill files across {len(skill_dirs)} directories")

    # ── 10. Databases ────────────────────────────────────────────
    results.append("\n## Databases")

    # Check all .db files in data dir
    data_dir = paths.get("data_dir", "")
    if not data_dir:
        if overlay:
            data_dir = os.path.join(overlay, "data")
        else:
            data_dir = os.path.join(tars_home, "data")

    if Path(data_dir).is_dir():
        db_files = list(Path(data_dir).glob("*.db"))
        for db_file in db_files:
            if db_file.name == "memory.db":
                continue  # Already checked in Memory System section
            out, rc = await _run(
                f"sqlite3 '{db_file}' 'PRAGMA integrity_check;' 2>/dev/null"
            )
            if rc == 0 and out.strip() == "ok":
                size_mb = db_file.stat().st_size / (1024 * 1024)
                ok(f"`{db_file.name}` integrity OK ({size_mb:.1f}MB)")
            else:
                fail(f"`{db_file.name}` integrity check FAILED: {out}")

    # ── 11. Application Health ───────────────────────────────────
    results.append("\n## Application Health")

    # OAuth token expiry
    if config.get("oauth_check", False):
        token_path = paths.get(
            "oauth_token",
            os.path.join(tars_home, "config", "google-token.json"),
        )
        tp = Path(token_path)
        if tp.exists():
            try:
                import json

                with open(tp) as f:
                    token_data = json.load(f)
                expiry = token_data.get("expiry", "")
                if expiry:
                    exp_dt = datetime.fromisoformat(
                        expiry.replace("Z", "+00:00")
                    )
                    if exp_dt > datetime.now(timezone.utc):
                        ok("OAuth token valid")
                    else:
                        fail(
                            "OAuth token EXPIRED — Google tools will fail silently"
                        )
                else:
                    ok("OAuth token present (no expiry field)")
            except Exception as e:
                warn(f"OAuth token exists but could not parse: {e}")
        else:
            warn(f"OAuth token not found at `{token_path}`")

    # Agent config YAML validity
    if not overlay:
        overlay = paths.get("overlay", os.environ.get("TARS_OVERLAY", ""))
    if overlay:
        for cfg in config.get("config_files", ["config/config.yaml", "config/agents.yaml"]):
            cfg_path = os.path.join(overlay, cfg)
            if Path(cfg_path).exists():
                try:
                    with open(cfg_path) as f:
                        yaml.safe_load(f)
                    ok(f"`{cfg}` valid YAML")
                except yaml.YAMLError as e:
                    fail(f"`{cfg}` YAML parse error: {e}")

    # ── 12. Agent Temp Dirs ────────────────────────────────────
    agent_tmp_dirs = config.get("agent_tmp", [])
    if agent_tmp_dirs and overlay:
        results.append("\n## Agent Temp Dirs")
        for subdir in agent_tmp_dirs:
            d = os.path.join(overlay, subdir)
            if not Path(d).is_dir():
                fail(f"`{subdir}` missing")
            elif not os.access(d, os.W_OK):
                fail(f"`{subdir}` not writable")
            else:
                ok(f"`{subdir}` exists + writable")

    # ── 13. Logs ─────────────────────────────────────────────────
    results.append("\n## Logs")

    out, _ = await _run(
        "journalctl --disk-usage 2>/dev/null | grep -oP '[\\d.]+[MG]' | head -1"
    )
    if out:
        ok(f"Journal size: {out}")

    # Recent errors in services
    for svc in config.get("services", []):
        out, _ = await _run(
            f"journalctl -u {svc} --since '6 hours ago' --no-pager -p err "
            f"2>/dev/null | grep -cv '^--\\|^$\\|^Hint:' || echo 0"
        )
        try:
            errs = int(out)
            if errs > 0:
                warn(f"`{svc}` — {errs} error lines in last 6h")
        except ValueError:
            pass

    # ── 14. Git State ────────────────────────────────────────────
    results.append("\n## Git State")

    for label, path in [
        ("Core", tars_home),
        ("OTHS", oths_root),
        ("Overlay", overlay),
    ]:
        if path and Path(path).is_dir():
            out, _ = await _run(
                f"git -C {path} status --porcelain 2>/dev/null | head -5"
            )
            if out:
                warn(f"{label} (`{path}`) has uncommitted changes")
            else:
                ok(f"{label} clean")

    # ── Summary ──────────────────────────────────────────────────
    total = passed + warned + failed
    if failed > 0:
        status = "UNHEALTHY"
    elif warned > 0:
        status = "DEGRADED"
    else:
        status = "HEALTHY"

    deployment = config.get("deployment", "unknown").upper()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = (
        f"# System Audit: {deployment}\n"
        f"**{status}** — {passed} passed, {warned} warnings, {failed} failures\n"
        f"_Run at {timestamp}_\n"
    )

    return header + "\n".join(results)
