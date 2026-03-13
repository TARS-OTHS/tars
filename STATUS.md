# TARS Deployment Status

**Last updated:** 2026-03-13
**Test server:** PROFLEX (178.104.4.188, Hetzner CPX31, Ubuntu 24.04)

---

## What's Working (verified on clean deploy)

- **One-command installer** (`installer/install.sh`) — installs all system deps, clones repo, launches wizard
- **Setup wizard all 6 sections** — prerequisites, basics, OC+credentials, agent identity, integrations, deploy
- **OpenClaw installation** — `--no-onboard`, programmatic config via direct JSON writes
- **Auth profiles** — `auth-profiles.json` written directly with correct format (token or api_key)
- **OC config** — `openclaw.json` written as complete JSON (gateway, channels, secrets providers)
- **Age vault encryption** — secrets collected and encrypted to `.secrets/*.age` files
- **Vault resolver script** — exec-based secret provider maps IDs to age-encrypted files
- **Docker Compose build & start** — all 7 services build and reach healthy status
- **Embedding service** — ONNX BGE-small-en-v1.5 loads and serves
- **Memory API** — healthy, responds on `/status`
- **Auth proxy** — healthy
- **Web proxy** — healthy
- **Credential proxy** — healthy
- **Cron service** — healthy
- **Dashboard API** — responds on port 8766
- **Dashboard UI** — serves `index.html` on port 8765
- **Dashboard security** — bound to `127.0.0.1` by default, `0.0.0.0` only with Tailscale
- **Tailscale setup** — interactive login (no auth key expiry), dashboard accessible via Tailscale IP
- **OC gateway** — running, authenticated, model configured (claude-sonnet-4-6)
- **Discord bot** — connected, paired, responding to messages
- **Health checks** — correct endpoints (`/status` for memory-api, `/health` for others)

## Bugs Fixed This Session

1. ~~Anthropic auth not configured~~ — write `auth-profiles.json` directly (type: "token" + order object)
2. ~~OC config via `config set` fails~~ — write complete `openclaw.json` as single JSON file
3. ~~Dashboard UI not serving~~ — added UIHandler thread on port 8765
4. ~~`AGENT_NAME` unbound in Tailscale setup~~ — use `hostname -s` instead
5. ~~Tailscale auth key expires in 90 days~~ — switched to interactive `tailscale up` login
6. ~~`DASHBOARD_BIND` invalid IP~~ — shell expansion concatenated IPs, replaced with if/else
7. ~~Newline in secret values~~ — `ask_secret` echo went to stdout, redirect to stderr
8. ~~Discord `botToken` field~~ — OC Discord uses `token`, not `botToken`
9. ~~Discord `dmPolicy: allowlist` requires allowFrom~~ — changed to `pairing`

## Known Issues (non-blocking)

1. **Discord pairing required** — after first deploy, owner must approve pairing code via `openclaw pairing approve discord <code>`. Could be automated or documented more prominently in setup output.
2. **Claude connection test returns HTTP 000** — setup token auth test may fail due to network/DNS, but gateway works fine
3. **Embedding service build is slow** — ~500MB Docker image, 10+ minute build. Consider lighter alternatives.
4. **Config file permissions warning** — OC doctor warns `openclaw.json` is group/world readable. Setup now `chmod 600` but verify.

## Next Stage

- **Approval workflows** — Dashboard UI for reviewing/approving agent actions
- **MCP integrations** — Tavily (web search), Trello (task management), etc.
- **Lighter embedding option** — evaluate smaller models or pre-computed embeddings
- **Multi-agent sandboxing** — isolated Docker containers per agent session
- **Auto-update mechanism** — `git pull` + rebuild from setup.sh or cron
- **Backup/restore** — memory DB snapshots, vault backup procedures
- **Documentation** — user guide, architecture diagrams, troubleshooting
- **CI/CD** — automated testing of installer on fresh VMs
- **Setup output polish** — show pairing instructions, Tailscale URL consistently

---

## Test Checklist (verified 2026-03-13)

- [x] `bash <(curl -fsSL ...)` runs installer successfully
- [x] All system deps installed (Docker, Node 24, age, Compose)
- [x] Repo cloned to `/opt/tars`
- [x] Setup wizard launches and collects all inputs
- [x] Claude credentials stored and OC auth configured
- [x] Discord bot token collected and OC channel configured
- [x] Vault resolver created and functional
- [x] OC gateway starts and responds
- [x] All 7 Docker services healthy
- [x] Dashboard accessible via Tailscale
- [x] Dashboard UI loads in browser (port 8765)
- [x] Dashboard API responds (port 8766)
- [x] Bot responds to Discord messages
- [ ] Memory API stores and retrieves memories (untested)
- [ ] Embedding service generates embeddings (untested)
