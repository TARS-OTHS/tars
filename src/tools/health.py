"""System health audit tool — thin wrapper around scripts/health-audit.sh."""

import asyncio
import os

from src.core.base import ToolContext
from src.core.tools import tool


@tool(
    name="system_audit",
    description="Run a full system health audit against a deployment config file",
    category="ops",
)
async def system_audit(ctx: ToolContext) -> str:
    tars_home = os.environ.get("TARS_HOME", "/opt/tars")
    script = os.path.join(tars_home, "scripts", "health-audit.sh")
    env = {**os.environ, "TERM": "dumb"}
    proc = await asyncio.create_subprocess_exec(
        script, "--report",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return "ERROR: health-audit.sh timed out (60s)"
    if proc.returncode != 0:
        err = stderr.decode().strip() if stderr else "unknown error"
        return f"ERROR: health-audit.sh failed (exit {proc.returncode}): {err}"
    return "Audit posted to #ops-alert"
