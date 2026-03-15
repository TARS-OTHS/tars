"""
Human-in-the-Loop (HITL) gate system for the auth proxy.

High-impact actions require Peter's explicit approval via Discord reactions
before the auth proxy executes them.

Flow:
1. Agent requests sensitive action (email send, Drive sharing, etc.)
2. Auth proxy detects gated route, returns 202 with hitl_id
3. Approval request posted to #security with ✅/❌ reaction prompt
4. Agent polls GET /ops/hitl-status/{hitl_id}
5. On ✅ → proxy executes original request, returns result
   On ❌ → returns 403
   On timeout (30 min) → returns 408
"""

import asyncio
import json
import logging
import re
import time
import uuid
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

# --- Config ---
HITL_TIMEOUT = 1800  # 30 minutes
SECURITY_CHANNEL_ID = "1478653539004710954"  # #ops-alerts
PETER_DISCORD_ID = "341650642709905408"
DISCORD_API_BASE = "https://discord.com/api/v10"

# In-memory pending requests (lost on restart — acceptable per spec)
_hitl_pending = {}

# --- Gated route definitions ---
# Each entry: (method_match, route_pattern, description, context_extractor_name)
HITL_GATES = [
    # Email sending
    ("POST", "google/gmail/v1/users/me/messages/send", "Send email", "gmail_send"),
    ("POST", "google/gmail/v1/users/me/drafts/send", "Send draft email", "gmail_send"),
    # Drive sharing (** matches multiple path segments: files/{id}/permissions)
    ("POST", "google/drive/v3/**/permissions", "Share Drive file", "drive_share"),
    ("PATCH", "google/drive/v3/**/permissions", "Modify Drive sharing", "drive_share"),
    # Notion
    ("POST", "notion/v1/pages", "Create Notion page", "notion_page"),
    ("PATCH", "notion/v1/pages", "Modify Notion page", "notion_page"),
    # Cloudflare (any mutation)
    ("POST", "cloudflare/", "Cloudflare API change", "cloudflare"),
    ("PUT", "cloudflare/", "Cloudflare API change", "cloudflare"),
    ("PATCH", "cloudflare/", "Cloudflare API change", "cloudflare"),
    ("DELETE", "cloudflare/", "Cloudflare API deletion", "cloudflare"),
    # Ops: agent lifecycle
    ("POST", "ops/agent-create", "Create agent", "ops_agent"),
    ("POST", "ops/agent-delete", "Delete agent", "ops_agent"),
    # Ops: system cron
    ("POST", "ops/sys-cron-add", "Add system cron job", "ops_cron"),
    # --- Red tier ops ---
    # Secret management
    ("POST", "ops/secret-set", "Set vault secret", "ops_secret"),
    ("POST", "ops/secret-delete", "Delete vault secret", "ops_secret"),
    ("POST", "ops/secret-inject", "Inject secret into agent", "ops_secret_inject"),
    # Service lifecycle
    ("POST", "ops/service-stop", "Stop service", "ops_service"),
    ("POST", "ops/service-start", "Start service", "ops_service"),
    # Auth proxy routes
    ("POST", "ops/auth-route-add", "Add auth proxy route", "ops_auth_route"),
    ("POST", "ops/auth-route-remove", "Remove auth proxy route", "ops_auth_route"),
    # Docker management
    ("POST", "ops/docker-prune", "Docker resource cleanup", "ops_docker"),
    ("POST", "ops/docker-build", "Docker image build", "ops_docker"),
    # System updates
    ("POST", "ops/update-openclaw", "Update OpenClaw", "ops_update"),
    ("POST", "ops/rebuild-sandbox", "Rebuild sandbox image", "ops_update"),
    # Disk management
    ("POST", "ops/disk-cleanup", "Disk space cleanup", "ops_disk"),
    # Backup operations
    ("POST", "ops/agent-backup", "Backup agent", "ops_backup"),
    ("POST", "ops/agent-restore", "Restore agent from backup", "ops_backup"),
    # Watchdog
    ("POST", "ops/watchdog-configure", "Configure watchdog rules", "ops_watchdog"),
]


def _match_gate(method, full_path):
    """Check if a request matches any HITL gate. Returns (desc, extractor) or None."""
    for gate_method, gate_pattern, desc, extractor in HITL_GATES:
        if method != gate_method:
            continue
        # Support ** as multi-segment wildcard, * as single-segment wildcard
        escaped = re.escape(gate_pattern)
        escaped = escaped.replace(r"\*\*", "DOUBLEWILD")
        escaped = escaped.replace(r"\*", "[^/]+")
        escaped = escaped.replace("DOUBLEWILD", ".+")
        regex = "^" + escaped + ".*$"
        if re.match(regex, full_path):
            return desc, extractor
    return None


def _extract_context(extractor_name, body_bytes, full_path):
    """Extract human-readable context from request body for the approval message."""
    context = {}
    try:
        if body_bytes:
            body = json.loads(body_bytes)
        else:
            body = {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}

    if extractor_name == "gmail_send":
        # Gmail send: try to extract recipient and subject from message payload
        msg = body.get("message", body)
        headers = msg.get("payload", {}).get("headers", [])
        for h in headers:
            name = h.get("name", "").lower()
            if name == "to":
                context["to"] = h.get("value", "?")[:100]
            elif name == "subject":
                context["subject"] = h.get("value", "?")[:100]
        # Also check raw format (base64 encoded) — just note it exists
        if msg.get("raw"):
            context["format"] = "raw (base64)"
            # Try to parse headers from raw
            import base64
            try:
                decoded = base64.urlsafe_b64decode(msg["raw"] + "==").decode("utf-8", errors="replace")
                for line in decoded.split("\n")[:20]:
                    if line.lower().startswith("to:"):
                        context["to"] = line[3:].strip()[:100]
                    elif line.lower().startswith("subject:"):
                        context["subject"] = line[8:].strip()[:100]
            except Exception:
                pass

    elif extractor_name == "drive_share":
        context["role"] = body.get("role", "?")
        context["type"] = body.get("type", "?")
        if body.get("emailAddress"):
            context["email"] = body["emailAddress"][:100]
        context["path"] = full_path[:100]

    elif extractor_name == "notion_page":
        props = body.get("properties", {})
        # Try common title property names
        for key in ("title", "Title", "Name", "name"):
            if key in props:
                title_arr = props[key].get("title", [])
                if title_arr:
                    context["title"] = title_arr[0].get("plain_text", "?")[:100]
                    break
        if body.get("parent", {}).get("database_id"):
            context["database"] = body["parent"]["database_id"][:36]

    elif extractor_name == "cloudflare":
        context["path"] = full_path[:150]

    elif extractor_name == "ops_agent":
        context["agent"] = body.get("agent_id", body.get("id", body.get("name", "?")))[:50]
        if body.get("model"):
            context["model"] = body["model"][:30]

    elif extractor_name == "ops_cron":
        context["schedule"] = body.get("schedule", body.get("every", "?"))[:50]
        context["script"] = body.get("script", body.get("command", "?"))[:100]

    # --- Red tier extractors ---
    elif extractor_name == "ops_secret":
        context["secret"] = body.get("name", "?")[:80]
        if body.get("scope"):
            context["scope"] = body["scope"][:30]

    elif extractor_name == "ops_secret_inject":
        context["secret"] = body.get("secret_name", "?")[:80]
        context["agent"] = body.get("agent", "?")[:30]
        context["env_var"] = body.get("env_var", body.get("secret_name", "?"))[:50]

    elif extractor_name == "ops_service":
        context["service"] = body.get("service", "?")[:50]

    elif extractor_name == "ops_auth_route":
        context["path"] = body.get("path", "?")[:50]
        if body.get("upstream"):
            context["upstream"] = body["upstream"][:80]

    elif extractor_name == "ops_docker":
        if body.get("what"):
            context["targets"] = ", ".join(body["what"])[:80]
        if body.get("image"):
            context["image"] = body["image"][:50]
        context["dry_run"] = str(body.get("dry_run", True))

    elif extractor_name == "ops_update":
        if body.get("version"):
            context["version"] = body["version"][:30]
        if body.get("agents"):
            context["agents"] = ", ".join(body["agents"])[:80]

    elif extractor_name == "ops_disk":
        context["actions"] = ", ".join(body.get("actions", []))[:80]
        context["dry_run"] = str(body.get("dry_run", True))

    elif extractor_name == "ops_backup":
        context["agent"] = body.get("agent", "?")[:30]
        if body.get("backup_id"):
            context["backup_id"] = body["backup_id"][:50]

    elif extractor_name == "ops_watchdog":
        rules = body.get("rules", [])
        context["rule_count"] = str(len(rules))
        if rules:
            context["services"] = ", ".join(r.get("service", "?") for r in rules[:5])[:80]

    elif extractor_name == "ops_generic":
        context["path"] = full_path[:100]

    return context


def _format_approval_message(agent_id, desc, context, hitl_id):
    """Format the Discord approval message."""
    lines = [f"🔒 **HITL Approval Required** — `{agent_id}`"]
    lines.append(f"**Action:** {desc}")
    if context:
        for k, v in context.items():
            lines.append(f"**{k.title()}:** {v}")
    lines.append(f"**ID:** `{hitl_id[:8]}`")
    lines.append("")
    lines.append("React ✅ to approve, ❌ to deny. Auto-denies in 30 minutes.")
    return "\n".join(lines)


async def _post_approval_request(app, message):
    """Post approval message to #security and add reaction options. Returns message_id."""
    session = app["client_session"]
    token_path = str(_TARS_HOME / ".secrets/rescue-discord-token")
    try:
        with open(token_path) as f:
            token = f.read().strip()
    except Exception as e:
        log.error("HITL: Failed to read Discord token: %s", e)
        return None

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    # Post message
    url = f"{DISCORD_API_BASE}/channels/{SECURITY_CHANNEL_ID}/messages"
    try:
        async with session.post(url, headers=headers, json={"content": message}) as resp:
            if resp.status >= 300:
                log.error("HITL: Failed to post approval message: %d", resp.status)
                return None
            data = await resp.json()
            message_id = data["id"]
    except Exception as e:
        log.error("HITL: Failed to post approval message: %s", e)
        return None

    # Add reaction options to the message
    for emoji in ["✅", "❌"]:
        emoji_encoded = "%E2%9C%85" if emoji == "✅" else "%E2%9D%8C"
        react_url = f"{DISCORD_API_BASE}/channels/{SECURITY_CHANNEL_ID}/messages/{message_id}/reactions/{emoji_encoded}/@me"
        try:
            async with session.put(react_url, headers={"Authorization": f"Bot {token}"}) as resp:
                if resp.status >= 300:
                    log.warning("HITL: Failed to add %s reaction: %d", emoji, resp.status)
        except Exception as e:
            log.warning("HITL: Failed to add %s reaction: %s", emoji, e)
        # Small delay to avoid Discord rate limits
        await asyncio.sleep(0.3)

    log.info("HITL: Approval request posted (message_id=%s)", message_id)
    return message_id


async def _check_reactions(app, message_id):
    """Check Discord reactions on the approval message. Returns 'approved', 'denied', or 'pending'."""
    session = app["client_session"]
    token_path = str(_TARS_HOME / ".secrets/rescue-discord-token")
    try:
        with open(token_path) as f:
            token = f.read().strip()
    except Exception:
        return "pending"

    headers = {"Authorization": f"Bot {token}"}

    for emoji, status in [("✅", "approved"), ("❌", "denied")]:
        emoji_encoded = "%E2%9C%85" if emoji == "✅" else "%E2%9D%8C"
        url = f"{DISCORD_API_BASE}/channels/{SECURITY_CHANNEL_ID}/messages/{message_id}/reactions/{emoji_encoded}"
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status >= 300:
                    continue
                users = await resp.json()
                # Check if Peter reacted (not just the bot's seed reactions)
                for user in users:
                    if user["id"] == PETER_DISCORD_ID:
                        return status
        except Exception as e:
            log.warning("HITL: Failed to check %s reactions: %s", emoji, e)

    return "pending"


async def check_hitl_gate(request, agent_id, method, route, path, body_bytes):
    """Check if this request requires HITL approval.

    Returns None if not gated, or a web.Response if gated (202 pending).
    Called from handle_proxy and ops handlers before executing the action.
    """
    # Build full path: route + path (ensure single separator)
    full_path = f"{route}/{path.lstrip('/')}" if not route.endswith("/") else f"{route}{path.lstrip('/')}"
    gate = _match_gate(method, full_path)
    if not gate:
        return None

    desc, extractor_name = gate
    context = _extract_context(extractor_name, body_bytes, full_path)
    hitl_id = str(uuid.uuid4())

    # Format and post approval message
    message = _format_approval_message(agent_id, desc, context, hitl_id)
    message_id = await _post_approval_request(request.app, message)

    if not message_id:
        # If we can't post to Discord, fail open with a warning
        # (don't block the agent if Discord is down — but log it loudly)
        log.error("HITL: Could not post approval request — FAILING OPEN for %s by %s", desc, agent_id)
        _write_audit_hitl(request, agent_id, route, path, "fail_open", hitl_id, desc)
        return None

    # Store pending request
    _hitl_pending[hitl_id] = {
        "agent_id": agent_id,
        "method": method,
        "route": route,
        "path": path,
        "body": body_bytes,
        "description": desc,
        "context": context,
        "message_id": message_id,
        "created_at": time.time(),
        "status": "pending",
    }

    log.info("HITL: Gated %s from %s — hitl_id=%s, awaiting approval", desc, agent_id, hitl_id[:8])
    _write_audit_hitl(request, agent_id, route, path, "pending", hitl_id, desc)

    return web.json_response({
        "status": "pending",
        "hitl_id": hitl_id,
        "message": f"Awaiting approval: {desc}. Poll GET /ops/hitl-status/{hitl_id} for updates.",
        "timeout_seconds": HITL_TIMEOUT,
    }, status=202)


def _write_audit_hitl(request, agent_id, route, path, status, hitl_id, desc):
    """Write HITL event to audit log."""
    fn = request.app.get("_write_audit")
    if fn:
        fn({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent_id,
            "ip": request.app.get("_get_container_ip", lambda r: "?")(request) if hasattr(request, 'remote') else "?",
            "method": request.method,
            "route": route,
            "path": path,
            "status": 202 if status == "pending" else 200,
            "hitl": status,
            "hitl_id": hitl_id[:8],
            "hitl_desc": desc,
        })


async def handle_hitl_status(request):
    """GET /ops/hitl-status/{hitl_id} — check approval status."""
    hitl_id = request.match_info["hitl_id"]
    entry = _hitl_pending.get(hitl_id)

    if not entry:
        return web.json_response({"error": "Unknown HITL ID"}, status=404)

    # Check timeout
    elapsed = time.time() - entry["created_at"]
    if elapsed > HITL_TIMEOUT:
        entry["status"] = "timeout"
        _hitl_pending.pop(hitl_id, None)
        log.info("HITL: Timed out — hitl_id=%s (%s)", hitl_id[:8], entry["description"])
        return web.json_response({
            "status": "timeout",
            "message": "No approval received within 30 minutes. You can re-request if Peter asks you to retry.",
            "elapsed_seconds": round(elapsed),
        }, status=408)

    # If already resolved, return cached result
    if entry["status"] == "approved":
        result = entry.get("result")
        _hitl_pending.pop(hitl_id, None)
        if result:
            return web.Response(
                status=result["status"],
                headers=result.get("headers", {}),
                body=result.get("body", b""),
            )
        return web.json_response({"status": "approved", "message": "Approved but no cached result"})

    if entry["status"] == "denied":
        _hitl_pending.pop(hitl_id, None)
        return web.json_response({
            "status": "denied",
            "reason": "Rejected by Peter",
        }, status=403)

    # Check Discord reactions
    reaction_status = await _check_reactions(request.app, entry["message_id"])

    if reaction_status == "approved":
        entry["status"] = "approved"
        log.info("HITL: Approved — hitl_id=%s (%s)", hitl_id[:8], entry["description"])

        # Execute the original request
        result = await _execute_gated_request(request.app, entry)
        if result:
            _hitl_pending.pop(hitl_id, None)
            return web.Response(
                status=result["status"],
                headers=result.get("headers", {}),
                body=result.get("body", b""),
            )
        _hitl_pending.pop(hitl_id, None)
        return web.json_response({"status": "approved", "message": "Approved but execution failed"}, status=502)

    elif reaction_status == "denied":
        entry["status"] = "denied"
        log.info("HITL: Denied — hitl_id=%s (%s)", hitl_id[:8], entry["description"])
        _hitl_pending.pop(hitl_id, None)
        return web.json_response({
            "status": "denied",
            "reason": "Rejected by Peter",
        }, status=403)

    # Still pending
    return web.json_response({
        "status": "pending",
        "hitl_id": hitl_id,
        "elapsed_seconds": round(elapsed),
        "timeout_seconds": HITL_TIMEOUT,
    })


async def handle_hitl_list(request):
    """GET /ops/hitl-list — list pending HITL requests (for debugging/visibility)."""
    now = time.time()
    pending = []
    for hid, entry in list(_hitl_pending.items()):
        elapsed = now - entry["created_at"]
        if elapsed > HITL_TIMEOUT:
            _hitl_pending.pop(hid, None)
            continue
        pending.append({
            "hitl_id": hid,
            "agent": entry["agent_id"],
            "description": entry["description"],
            "context": entry["context"],
            "status": entry["status"],
            "elapsed_seconds": round(elapsed),
            "remaining_seconds": round(HITL_TIMEOUT - elapsed),
        })
    return web.json_response({"pending": pending, "count": len(pending)})


async def _execute_gated_request(app, entry):
    """Execute the original gated request after approval.

    For proxy routes (google/, notion/, cloudflare/): forward to upstream.
    For ops routes: call the handler directly (not implemented here — ops handlers
    will check HITL status themselves).
    """
    from urllib.parse import urlencode, parse_qs, urlparse, urlunparse

    route = entry["route"]
    path = entry["path"]
    method = entry["method"]
    body = entry["body"]

    # Check if this is a proxy route (has an upstream) or an ops route
    route_defs = app.get("route_defs", {})
    defn = route_defs.get(route)

    if defn:
        # Proxy route — forward to upstream with credential injection
        remainder = path
        upstream_url = defn["upstream"].rstrip("/") + remainder

        # Inject auth
        headers = {"Content-Type": "application/json"}
        params = {}
        ctx = {"secrets": app["secrets"]}
        defn["auth"](headers, params, ctx)

        if params:
            parsed = urlparse(upstream_url)
            existing = parse_qs(parsed.query)
            existing.update({k: [v] for k, v in params.items()})
            flat = {k: v[0] if len(v) == 1 else v for k, v in existing.items()}
            new_query = urlencode(flat, doseq=True)
            upstream_url = urlunparse(parsed._replace(query=new_query))

        session = app["client_session"]
        try:
            async with session.request(method, upstream_url, headers=headers, data=body,
                                       allow_redirects=False) as resp:
                resp_headers = {
                    k: v for k, v in resp.headers.items()
                    if k.lower() not in {"transfer-encoding", "connection", "content-encoding"}
                }
                resp_body = await resp.read()
                log.info("HITL: Executed %s %s/%s -> %d (%d bytes)",
                         method, route, path, resp.status, len(resp_body))
                return {"status": resp.status, "headers": resp_headers, "body": resp_body}
        except Exception as e:
            log.error("HITL: Execution failed for %s %s/%s: %s", method, route, path, e)
            return None

    elif route == "ops":
        # Ops routes are handled differently — the ops handler checks hitl_approved flag
        # Return a marker that tells the caller to re-dispatch
        log.info("HITL: Ops route approved — %s", entry["description"])
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "status": "approved",
                "message": f"HITL approved: {entry['description']}. Re-submit your request.",
                "hitl_id": None,  # Signal that approval is consumed
            }).encode(),
        }

    return None


def register_hitl_routes(app, route_defs, get_agent_id_fn, write_audit_fn, get_container_ip_fn):
    """Register HITL routes and store references in app context."""
    app["route_defs"] = route_defs
    app["_write_audit"] = write_audit_fn
    app["_get_container_ip"] = get_container_ip_fn

    app.router.add_get("/ops/hitl-status/{hitl_id}", handle_hitl_status)
    app.router.add_get("/ops/hitl-list", handle_hitl_list)

    log.info("HITL gate system registered (%d gate rules, timeout=%ds)",
             len(HITL_GATES), HITL_TIMEOUT)
