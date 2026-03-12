# Security Model

TARS applies a security baseline automatically on every deployment. This document describes the model, what's enforced, and how to audit it.

## Principles

1. **Credentials never enter agent containers.** The auth proxy is the only component that reads API keys.
2. **Agents are untrusted.** They run in sandboxed containers with minimal privileges.
3. **Defense in depth.** Multiple layers — Docker isolation, network restrictions, capability drops, resource limits.
4. **Secure by default.** The baseline is applied automatically. Users opt *out* of security, not in.

## Baseline Measures

### Container Hardening

| Measure | Implementation |
|---------|---------------|
| **Capability drop** | `cap_drop: [ALL]` — no Linux capabilities |
| **Read-only root** | `read_only: true` — agents can't modify system files |
| **No privilege escalation** | `security_opt: [no-new-privileges:true]` |
| **Resource limits** | Memory and CPU caps per container |
| **No Docker socket** | Agents cannot manage other containers |
| **Tmpfs for temp** | `/tmp` mounted as tmpfs (size-limited) |

### Network Security

| Measure | Implementation |
|---------|---------------|
| **Cloud metadata blocked** | `iptables -I DOCKER-USER -d 169.254.169.254 -j DROP` |
| **No direct internet** | Outbound traffic goes through web proxy only |
| **Services on bridge only** | Auth proxy, memory DB bind to `172.17.0.1`, not `0.0.0.0` |
| **Dashboard access control** | Local-only by default; Tailscale-gated if enabled |

### Credential Security

| Measure | Implementation |
|---------|---------------|
| **Encrypted at rest** | `.secrets/` files encrypted with age |
| **Restricted permissions** | `chmod 600` on all secret files |
| **Proxy injection** | Auth proxy adds credentials to requests at the network layer |
| **No env var exposure** | API keys are not passed as environment variables to agents |
| **Route-based access** | Agents can only reach APIs that have configured routes |

## Security Audit

Run the built-in audit:

```bash
./scripts/security-audit.sh
```

This checks:
- [ ] All containers have `cap_drop: ALL`
- [ ] All containers have resource limits
- [ ] Cloud metadata endpoint is blocked
- [ ] No services exposed on public interfaces (unless intentional)
- [ ] `.secrets/` permissions are correct
- [ ] Dashboard is not publicly accessible
- [ ] Docker socket is not mounted in agent containers
- [ ] No containers running as root
- [ ] Firewall rules are in place

Results are reported to the dashboard security page and stdout.

## Threat Model

### What We Defend Against

| Threat | Mitigation |
|--------|-----------|
| Agent exfiltrating credentials | Credentials never enter containers |
| Agent accessing cloud provider metadata | iptables rule blocks 169.254.169.254 |
| Agent escaping container | Capability drops, no privilege escalation, resource limits |
| Agent accessing other agents' data | Separate workspaces, agent-scoped memory |
| Credential leakage in logs | Auth proxy strips sensitive headers from logs |
| Unauthorized dashboard access | Local-only binding, optional Tailscale gate |

### What's Out of Scope (v1)

- Multi-user authentication (single-user per deployment)
- Encrypted memory at rest (SQLite is plaintext; protect at the disk/VPS level)
- Agent-to-agent network isolation (all agents share the Docker bridge)
- TLS between internal services (trusted internal network)

## Hardening Beyond Baseline

For higher-security deployments:

```bash
# Enable UFW firewall
sudo ufw enable
sudo ufw default deny incoming
sudo ufw allow ssh

# Restrict Docker to Tailscale only
sudo ufw allow in on tailscale0

# Enable automatic security updates
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# Set up fail2ban for SSH
sudo apt install fail2ban
sudo systemctl enable fail2ban
```

See the [healthcheck skill](../skills/healthcheck/) for automated security auditing and hardening recommendations.
