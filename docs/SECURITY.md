# Security Model

TARS applies a security baseline automatically on every deployment. This document describes the model, what's enforced, and how to audit it.

## Principles

1. **Credentials never enter agent containers.** The auth proxy is the only component that reads API keys. Agents access APIs through proxy routes — raw keys are injected at the network layer.
2. **All agents are sandboxed.** Every agent runs in a Docker container with `cap_drop: ALL`, read-only root, non-root user, and forced proxy routing. There is no unsandboxed execution mode.
3. **Defense in depth.** Multiple layers — Docker isolation, network restrictions, capability drops, resource limits, credential proxy, encrypted vault.
4. **Secure by default.** The setup wizard configures sandboxing, encryption, and network isolation automatically. Users opt *out* of security, not in.

## Agent Sandbox

Every agent runs in a `tars-sandbox:base` Docker container managed by OpenClaw. The sandbox configuration is set in `openclaw.json` under `agents.defaults.sandbox` and applies to all agents — including new agents created via `add-agent.sh`.

### Container Hardening

| Measure | Implementation |
|---------|---------------|
| **Sandbox mode** | `mode: "all"` — all tool execution happens inside the sandbox |
| **Capability drop** | `cap_drop: [ALL]` — no Linux capabilities |
| **Read-only root** | Agents cannot modify system files |
| **Non-root user** | Runs as `node` user (not root) |
| **Resource limits** | 2GB RAM, 2 CPUs per agent |
| **No Docker socket** | Agents cannot manage containers |
| **Workspace-only writes** | Only the mounted workspace volume is writable |

### Network Security

| Measure | Implementation |
|---------|---------------|
| **Forced proxy routing** | `http_proxy` and `https_proxy` env vars route all traffic through web proxy |
| **No direct internet** | Agents cannot bypass the proxy |
| **Services on bridge only** | Auth proxy, memory API bind to `172.17.0.1`, not public interfaces |
| **Dashboard access control** | Bound to `127.0.0.1` by default; Tailscale-only if configured |

### Credential Security

| Measure | Implementation |
|---------|---------------|
| **Encrypted at rest** | Secrets encrypted with age in `.secrets-vault/` and `.secrets/` |
| **Restricted permissions** | `chmod 600` on all secret files |
| **Proxy injection** | Auth proxy (reverse) and credential proxy (forward) add credentials at the network layer |
| **No env var exposure** | API keys are not passed as environment variables to agent containers |
| **No filesystem access** | Agent containers cannot read `.secrets/`, the vault, or age keys |
| **Route-based access** | Agents can only reach APIs that have configured proxy routes |
| **MCP credential isolation** | MCP server credentials are mounted into the MCP gateway container from host files — agents access tools via mcporter CLI, never see credentials |

## Threat Model

### What We Defend Against

| Threat | Mitigation |
|--------|-----------|
| **Prompt injection via inbound messages** | Sandbox limits blast radius — even if an agent is tricked, it cannot access credentials, other agents' data, or system files |
| **Agent exfiltrating credentials** | Credentials never enter containers — auth proxy injects them at the network layer |
| **Agent accessing other agents' data** | Separate workspace volumes, agent-scoped memory |
| **Agent escaping container** | `cap_drop: ALL`, no privilege escalation, non-root user, resource limits |
| **Credential leakage in logs** | Auth proxy strips sensitive headers from logs |
| **Unauthorized dashboard access** | Local-only binding, optional Tailscale gate |

### What's Out of Scope (v1)

- Multi-user authentication (single-user per deployment)
- Encrypted memory at rest (SQLite is plaintext; protect at the disk/VPS level)
- Agent-to-agent network isolation (all agents share the Docker bridge)
- TLS between internal services (trusted internal network)
- Cloud metadata blocking (`iptables` rule — recommended but not automated yet)

## Emergent Package Installation

Agents can install packages at runtime (`pip install --user`, `npm install`). This is by design — it allows agents to self-serve tools without requiring image rebuilds. Installed packages persist in the workspace volume at `/workspace/.local/`.

**Security implications:** An agent could install a malicious package. This is mitigated by:
- Sandbox isolation limits what a malicious package can do
- No access to credentials, other workspaces, or system files
- Network traffic is proxied and can be logged
- Workspace volumes can be inspected or wiped

## Hardening Beyond Baseline

For higher-security deployments:

```bash
# Block cloud metadata endpoint (recommended on VPS)
sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP

# Enable UFW firewall
sudo ufw enable
sudo ufw default deny incoming
sudo ufw allow ssh

# Restrict to Tailscale only
sudo ufw allow in on tailscale0

# Enable automatic security updates
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# Set up fail2ban for SSH
sudo apt install fail2ban
sudo systemctl enable fail2ban
```
