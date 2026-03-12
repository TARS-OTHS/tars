"""
Content Safety Pipeline — Phase 3 of security hardening.

Three layers:
  Layer 1: Content sanitization (strip invisible chars, HTML, base64, data URIs)
  Layer 2: Injection pattern scanner (flag suspicious patterns, score 0-10)
  Layer 3: Behavioral monitoring (track content consumption → sensitive action patterns)

Integrates into auth proxy handle_proxy as post-response processing.
All layers are signal/alert only — never block.
"""

import json
import logging
import re
import time
import unicodedata
import html as html_module
from datetime import datetime, timezone
from collections import defaultdict

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

# =============================================================================
# CONFIG
# =============================================================================

# Routes whose responses should be sanitized and scanned
SANITIZE_ROUTES = {"google", "tavily"}

# Content consumption paths (reading external content)
CONTENT_CONSUMPTION_PATHS = {
    "google/gmail/v1/users/me/messages/",   # Gmail read (individual messages)
    "tavily/search",                         # Tavily search
    "tavily/extract",                        # Tavily extract
}

# Sensitive action paths (things that could cause harm)
SENSITIVE_ACTION_PATHS = {
    "google/gmail/v1/users/me/messages/send",
    "google/gmail/v1/users/me/drafts/send",
    "google/drive/v3/",
    "notion/v1/pages",
    "cloudflare/",
    "ops/agent-create",
    "ops/agent-delete",
    "ops/sys-cron-add",
    "ops/secret-set",
    "ops/secret-inject",
    "ops/service-stop",
    "ops/auth-route-add",
    "ops/tool-install",
}

CONTENT_WINDOW = 300  # 5 minutes
VOLUME_SPIKE_MULTIPLIER = 3  # 3x rolling average
RAPID_ACTION_THRESHOLD = 3   # 3 different sensitive actions in 5 minutes

# Shadow mode: sanitize and log but return original response
SHADOW_MODE = True

# Security channel for behavioral alerts
SECURITY_CHANNEL_ID = "1478653539004710954"

# =============================================================================
# LAYER 1: Content Sanitization
# =============================================================================

# Zero-width and invisible characters
INVISIBLE_CHARS = re.compile(
    '[\u200b\u200c\u200d\u200e\u200f'   # zero-width chars
    '\u202a-\u202e'                        # bidi overrides
    '\ufeff\ufff9-\ufffb'                  # BOM, interlinear annotations
    ']'
)

# Base64 blocks > 200 chars (likely encoded payloads, not short tokens)
BASE64_BLOCK = re.compile(r'[A-Za-z0-9+/=]{200,}')

# Data URIs
DATA_URI = re.compile(r'data:[^;]+;base64,[A-Za-z0-9+/=]+', re.IGNORECASE)


def sanitize_text(text):
    """Strip potentially dangerous content from external text. Returns (sanitized, changes_made)."""
    if not text or not isinstance(text, str) or len(text) < 10:
        return text, []

    changes = []
    original = text

    # Strip HTML tags (keep text content)
    cleaned = re.sub(r'<[^>]+>', ' ', text)
    if cleaned != text:
        changes.append("html_stripped")
        text = cleaned

    # Decode HTML entities
    cleaned = html_module.unescape(text)
    if cleaned != text:
        changes.append("entities_decoded")
        text = cleaned

    # Remove invisible characters
    cleaned = INVISIBLE_CHARS.sub('', text)
    if cleaned != text:
        changes.append("invisible_chars_removed")
        text = cleaned

    # Remove data URIs
    cleaned = DATA_URI.sub('[DATA_URI_REMOVED]', text)
    if cleaned != text:
        changes.append("data_uris_removed")
        text = cleaned

    # Remove large base64 blocks (preserve small ones like short tokens)
    cleaned = BASE64_BLOCK.sub('[BASE64_BLOCK_REMOVED]', text)
    if cleaned != text:
        changes.append("base64_blocks_removed")
        text = cleaned

    # Normalize Unicode (NFC form)
    cleaned = unicodedata.normalize('NFC', text)
    if cleaned != text:
        changes.append("unicode_normalized")
        text = cleaned

    # Collapse excessive whitespace
    cleaned = re.sub(r'\n{4,}', '\n\n\n', text)
    cleaned = re.sub(r' {4,}', '   ', cleaned)
    if cleaned != text:
        changes.append("whitespace_collapsed")
        text = cleaned

    return text, changes


def sanitize_json_strings(obj, depth=0):
    """Recursively sanitize all string values in a JSON object.
    Returns (sanitized_obj, total_changes)."""
    if depth > 20:  # Prevent infinite recursion
        return obj, []

    all_changes = []

    if isinstance(obj, str):
        sanitized, changes = sanitize_text(obj)
        return sanitized, changes

    elif isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            sanitized, changes = sanitize_json_strings(v, depth + 1)
            result[k] = sanitized
            all_changes.extend(changes)
        return result, all_changes

    elif isinstance(obj, list):
        result = []
        for item in obj:
            sanitized, changes = sanitize_json_strings(item, depth + 1)
            result.append(sanitized)
            all_changes.extend(changes)
        return result, all_changes

    return obj, []


# =============================================================================
# LAYER 2: Injection Pattern Scanner
# =============================================================================

INJECTION_PATTERNS = {
    "instruction": [
        re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
        re.compile(r"you\s+are\s+now\s+a?n?\s*", re.IGNORECASE),
        re.compile(r"^system\s*:", re.IGNORECASE | re.MULTILINE),
        re.compile(r"new\s+instructions?\s*:", re.IGNORECASE),
        re.compile(r"disregard\s+(all\s+)?(above|previous|prior)", re.IGNORECASE),
    ],
    "authority_spoof": [
        re.compile(r"peter\s+says?\s+to", re.IGNORECASE),
        re.compile(r"urgent\s+from\s+(the\s+)?owner", re.IGNORECASE),
        re.compile(r"emergency\s+override", re.IGNORECASE),
        re.compile(r"admin\s+mode", re.IGNORECASE),
    ],
    "exfiltration": [
        re.compile(r"send\s+this\s+to\s+\S+@\S+", re.IGNORECASE),
        re.compile(r"forward\s+(this\s+)?to\s+\S+", re.IGNORECASE),
        re.compile(r"post\s+(this\s+)?in\s+(the\s+)?channel", re.IGNORECASE),
    ],
    "delimiter_attack": [
        re.compile(r"</system>", re.IGNORECASE),
        re.compile(r"<\|im_start\|>"),
        re.compile(r"\[INST\]"),
        re.compile(r"\[/INST\]"),
        re.compile(r"<\|endoftext\|>"),
    ],
}


def score_injection_risk(text):
    """Score text for injection patterns. Returns {score: 0-10, matches: [...], categories: [...]}."""
    if not text or not isinstance(text, str):
        return {"score": 0, "matches": [], "categories": []}

    # Only scan first 50KB to avoid performance issues on large responses
    scan_text = text[:51200]
    matches = []
    categories = set()

    for category, patterns in INJECTION_PATTERNS.items():
        for pattern in patterns:
            if pattern.search(scan_text):
                matches.append({"category": category, "pattern": pattern.pattern[:60]})
                categories.add(category)

    score = min(10, len(matches) * 3)  # 0, 3, 6, 9, 10
    return {"score": score, "matches": matches, "categories": sorted(categories)}


def score_json_content(obj, depth=0):
    """Recursively scan all strings in JSON for injection patterns. Returns highest score."""
    if depth > 20:
        return {"score": 0, "matches": [], "categories": []}

    if isinstance(obj, str):
        return score_injection_risk(obj)

    best = {"score": 0, "matches": [], "categories": []}

    if isinstance(obj, dict):
        for v in obj.values():
            result = score_json_content(v, depth + 1)
            if result["score"] > best["score"]:
                best = result

    elif isinstance(obj, list):
        for item in obj:
            result = score_json_content(item, depth + 1)
            if result["score"] > best["score"]:
                best = result

    return best


# =============================================================================
# LAYER 3: Behavioral Monitoring
# =============================================================================

# Per-agent activity tracking (in-memory, resets on restart)
_agent_activity = defaultdict(lambda: {
    "last_content_consumption": 0,
    "last_injection_score": 0,
    "last_injection_categories": [],
    "route_history": set(),
    "recent_requests": [],       # list of (timestamp, route) tuples
    "recent_sensitive": [],      # list of (timestamp, action) tuples
})


def _is_content_consumption(route, path):
    """Check if this request is consuming external content."""
    full = f"{route}/{path.lstrip('/')}"
    for prefix in CONTENT_CONSUMPTION_PATHS:
        if full.startswith(prefix):
            return True
    return False


def _is_sensitive_action(method, route, path):
    """Check if this request is a sensitive action."""
    if method == "GET":
        return False
    full = f"{route}/{path.lstrip('/')}"
    for prefix in SENSITIVE_ACTION_PATHS:
        if full.startswith(prefix):
            return True
    return False


def track_request(agent_id, method, route, path, injection_score=0, injection_categories=None):
    """Track a request for behavioral analysis. Returns list of anomaly alerts."""
    now = time.time()
    activity = _agent_activity[agent_id]
    full_path = f"{route}/{path.lstrip('/')}"
    alerts = []

    # Track request volume
    activity["recent_requests"].append((now, full_path))
    # Trim to last hour
    activity["recent_requests"] = [
        (t, r) for t, r in activity["recent_requests"] if now - t < 3600
    ]

    # Check if consuming content
    if _is_content_consumption(route, path):
        activity["last_content_consumption"] = now
        activity["last_injection_score"] = injection_score
        activity["last_injection_categories"] = injection_categories or []

    # Check if performing sensitive action
    if _is_sensitive_action(method, route, path):
        activity["recent_sensitive"].append((now, full_path))
        # Trim to last 5 minutes
        activity["recent_sensitive"] = [
            (t, a) for t, a in activity["recent_sensitive"] if now - t < CONTENT_WINDOW
        ]

        # Rule 1: Sensitive action after content consumption
        time_since_content = now - activity["last_content_consumption"]
        if activity["last_content_consumption"] > 0 and time_since_content < CONTENT_WINDOW:
            severity = "HIGH"
            injection_ctx = ""

            # Rule 2: Elevated if high injection score
            if activity["last_injection_score"] >= 6:
                severity = "CRITICAL"
                injection_ctx = f" (injection_score: {activity['last_injection_score']}, categories: {activity['last_injection_categories']})"

            alerts.append({
                "severity": severity,
                "rule": "sensitive_after_content",
                "agent": agent_id,
                "action": f"{method} {full_path}",
                "context": f"{int(time_since_content)}s after external content consumption{injection_ctx}",
            })

        # Rule 5: Rapid sensitive actions (3+ different in 5 min)
        unique_recent = set(a for _, a in activity["recent_sensitive"])
        if len(unique_recent) >= RAPID_ACTION_THRESHOLD:
            alerts.append({
                "severity": "HIGH",
                "rule": "rapid_sensitive_actions",
                "agent": agent_id,
                "action": f"{method} {full_path}",
                "context": f"{len(unique_recent)} different sensitive actions in 5 minutes: {', '.join(sorted(unique_recent)[:5])}",
            })

    # Rule 3: Novel route usage
    if full_path not in activity["route_history"]:
        activity["route_history"].add(full_path)
        # Only alert after baseline period (route_history has >10 known routes)
        if len(activity["route_history"]) > 10:
            alerts.append({
                "severity": "MEDIUM",
                "rule": "novel_route",
                "agent": agent_id,
                "action": f"{method} {full_path}",
                "context": "First time accessing this route",
            })

    # Rule 4: Volume spike
    if len(activity["recent_requests"]) > 10:  # Need some baseline
        # Compare last 10 min to hourly average
        recent_10m = sum(1 for t, _ in activity["recent_requests"] if now - t < 600)
        hourly_total = len(activity["recent_requests"])
        expected_10m = hourly_total / 6  # 10min is 1/6 of an hour
        if expected_10m > 0 and recent_10m > expected_10m * VOLUME_SPIKE_MULTIPLIER:
            alerts.append({
                "severity": "MEDIUM",
                "rule": "volume_spike",
                "agent": agent_id,
                "action": f"{method} {full_path}",
                "context": f"{recent_10m} requests in 10min vs {expected_10m:.0f} expected (3x threshold)",
            })

    return alerts


def format_alert(alert):
    """Format a behavioral alert for Discord."""
    emoji = "🚨" if alert["severity"] == "CRITICAL" else "⚠️"
    return (
        f"{emoji} **BEHAVIORAL ALERT — {alert['agent']}** [{alert['severity']}]\n"
        f"**Rule:** {alert['rule']}\n"
        f"**Action:** {alert['action']}\n"
        f"**Context:** {alert['context']}"
    )


# =============================================================================
# INTEGRATION: Process response from handle_proxy
# =============================================================================

def process_response(agent_id, method, route, path, status, resp_body, content_type=""):
    """Process a proxy response through the content safety pipeline.

    Called from handle_proxy after receiving upstream response.
    Returns dict with:
      - sanitized_body: bytes (sanitized response body, or original if shadow mode)
      - injection_score: int
      - injection_categories: list
      - sanitization_changes: list
      - alerts: list of behavioral alerts
    """
    result = {
        "sanitized_body": resp_body,
        "injection_score": 0,
        "injection_categories": [],
        "sanitization_changes": [],
        "alerts": [],
    }

    # Only process JSON responses on sanitizable routes
    if route not in SANITIZE_ROUTES:
        # Still track for behavioral monitoring
        result["alerts"] = track_request(agent_id, method, route, path)
        return result

    if not content_type or "json" not in content_type.lower():
        result["alerts"] = track_request(agent_id, method, route, path)
        return result

    if status >= 400:
        # Don't process error responses
        result["alerts"] = track_request(agent_id, method, route, path)
        return result

    # Parse JSON response
    try:
        body_json = json.loads(resp_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        result["alerts"] = track_request(agent_id, method, route, path)
        return result

    # Layer 1: Sanitize content
    sanitized_json, changes = sanitize_json_strings(body_json)
    result["sanitization_changes"] = changes

    if changes and not SHADOW_MODE:
        # Re-encode sanitized response
        result["sanitized_body"] = json.dumps(sanitized_json).encode("utf-8")

    if changes:
        log.info("CONTENT_SAFETY [L1]: agent=%s route=%s changes=%d (%s) mode=%s",
                 agent_id, route, len(changes), ",".join(set(changes))[:100],
                 "shadow" if SHADOW_MODE else "active")

    # Layer 2: Injection scoring (scan original content, not sanitized)
    injection = score_json_content(body_json)
    result["injection_score"] = injection["score"]
    result["injection_categories"] = injection["categories"]

    if injection["score"] > 0:
        log.warning("CONTENT_SAFETY [L2]: agent=%s route=%s injection_score=%d categories=%s",
                    agent_id, route, injection["score"], injection["categories"])

    # Layer 3: Behavioral tracking
    result["alerts"] = track_request(
        agent_id, method, route, path,
        injection_score=injection["score"],
        injection_categories=injection["categories"],
    )

    return result


async def post_behavioral_alerts(app, alerts):
    """Post behavioral alerts to #security channel."""
    if not alerts:
        return

    session = app.get("client_session")
    if not session:
        return

    try:
        with open(str(_TARS_HOME / ".secrets/rescue-discord-token")) as f:
            token = f.read().strip()
    except Exception:
        return

    for alert in alerts:
        # Only post HIGH and CRITICAL to Discord (MEDIUM is log-only)
        if alert["severity"] == "MEDIUM":
            log.info("BEHAVIORAL [%s]: %s — %s — %s",
                     alert["severity"], alert["agent"], alert["rule"], alert["context"])
            continue

        message = format_alert(alert)
        try:
            url = f"https://discord.com/api/v10/channels/{SECURITY_CHANNEL_ID}/messages"
            async with session.post(url,
                headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
                json={"content": message},
            ) as resp:
                if resp.status < 300:
                    log.info("BEHAVIORAL alert posted: %s — %s", alert["agent"], alert["rule"])
                else:
                    log.warning("Failed to post behavioral alert: %d", resp.status)
        except Exception as e:
            log.warning("Failed to post behavioral alert: %s", e)
