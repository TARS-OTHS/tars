# TARS Deployment Status

**Last updated:** 2026-03-13
**Test server:** PROFLEX (178.104.4.188, Hetzner CPX31, Ubuntu 24.04)

---

## What's Working

- **One-command installer** (`installer/install.sh`) — installs all system deps (Docker, Node 24, age, curl, jq, Compose), clones repo, launches setup wizard
- **`bash <(curl ...)` pattern** — preserves stdin for interactive wizard
- **Setup wizard Sections 1-2** — prerequisites check and `.env` generation work cleanly
- **OpenClaw installation** — `--no-onboard` flag skips interactive TUI, programmatic config follows
- **Age vault encryption** — secrets collected and encrypted to `.secrets/*.age` files
- **Vault resolver script** — exec-based secret provider maps IDs to age-encrypted files
- **Docker Compose build & start** — all 7 services build and reach healthy status
- **Embedding service** — ONNX BGE-small-en-v1.5 loads and serves (but slow build, see below)
- **Memory API** — healthy, responds on `/status`
- **Auth proxy** — healthy
- **Web proxy** — healthy
- **Credential proxy** — healthy
- **Cron service** — healthy
- **Dashboard API** — responds on port 8766 with `{"ok": true, "status": "running"}`
- **Dashboard security** — bound to `127.0.0.1` by default (not publicly exposed)
- **SSH tunnel access option** — works for accessing dashboard from local machine
- **Tailscale access option** — code written in setup.sh (untested in clean run)

## What's Broken

### Critical (bot doesn't work)

1. **Anthropic auth not configured in OC**
   - `openclaw models auth paste-token` is an interactive TUI — can't pipe input programmatically
   - **Fix needed:** Write `auth-profiles.json` directly to `~/.openclaw/agents/main/agent/auth-profiles.json` with correct format
   - Status: wrote file directly but `openclaw models status` still didn't recognize it — likely wrong file format or path

2. **OC SecretRef via `config set` fails silently**
   - `openclaw config set channels.discord.token.source '"exec"'` doesn't create proper SecretRef objects
   - Discord channel shows "no token" despite config appearing to be set
   - **Fix needed:** Write `openclaw.json` directly as complete JSON instead of individual `config set` calls

3. **`configure_openclaw()` in setup.sh broken**
   - Individual `config set` commands don't reliably build nested JSON structures
   - Gateway mode not set → gateway crashes
   - **Fix needed:** Generate complete `openclaw.json` and write it in one shot, including `gateway.mode: "local"`

### Important (functionality gaps)

4. **Dashboard UI not serving on port 8765**
   - `api-server.py` only starts HTTP server on port 8766 (JSON API)
   - `index.html` exists but nothing serves it on 8765
   - **Fix needed:** Add a second HTTP server thread in `api-server.py` to serve static files on port 8765

5. **Health checks in setup.sh used wrong endpoints (fixed in code, untested)**
   - Memory API serves `/status` not `/health`
   - Services bind to `${DOCKER_HOST_IP}` not `localhost`
   - Fix pushed but not validated in a clean run

### Minor

6. **Claude connection test returns HTTP 000**
   - Network/DNS issue on PROFLEX when hitting `api.anthropic.com`
   - Non-blocking (just a warning) but needs investigation

7. **Embedding service build is slow**
   - ~500MB Docker image, 10+ minute build, downloads model on first start
   - ONNX Runtime + model export takes significant time
   - Consider pre-built image or lighter embedding approach

## What Needs Fixing (setup.sh changes for next clean run)

1. **Rewrite `collect_claude_credentials()`**
   - Don't use `openclaw models auth paste-token` (interactive TUI)
   - Write `auth-profiles.json` directly with correct format for subscription token or API key
   - Verify correct file path and JSON structure by checking OC docs

2. **Rewrite `configure_openclaw()`**
   - Generate complete `openclaw.json` as single JSON file
   - Include: `gateway.mode: "local"`, channel configs with SecretRefs, vault resolver reference
   - Write to `~/.openclaw/agents/main/agent/openclaw.json`

3. **Fix dashboard to serve UI**
   - Modify `services/dashboard/api-server.py` to serve `index.html` and static files on port 8765

4. **Validate Tailscale setup flow**
   - Code exists but hasn't been tested in a clean run

5. **Add gateway health monitoring**
   - Setup.sh should verify OC gateway is actually responding after configuration
   - Add retry logic with clear error messages

## Next Stage (after core deployment works)

- **Approval workflows** — Dashboard UI for reviewing/approving agent actions
- **MCP integrations** — Tavily (web search), Trello (task management), etc.
- **Lighter embedding option** — evaluate smaller models or pre-computed embeddings
- **Multi-agent sandboxing** — isolated Docker containers per agent session
- **Auto-update mechanism** — `git pull` + rebuild from setup.sh or cron
- **Backup/restore** — memory DB snapshots, vault backup procedures
- **Documentation** — user guide, architecture diagrams, troubleshooting
- **CI/CD** — automated testing of installer on fresh VMs

---

## Test Checklist (for next clean run)

- [ ] `bash <(curl -fsSL ...)` runs installer successfully
- [ ] All system deps installed (Docker, Node 24, age, Compose)
- [ ] Repo cloned to `/opt/tars`
- [ ] Setup wizard launches and collects all inputs
- [ ] Claude credentials stored and OC auth configured
- [ ] Discord bot token collected and OC channel configured
- [ ] Vault resolver created and functional
- [ ] OC gateway starts and responds on `localhost:18789`
- [ ] All 7 Docker services healthy
- [ ] Dashboard accessible via SSH tunnel or Tailscale
- [ ] Dashboard UI loads in browser (port 8765)
- [ ] Dashboard API responds (port 8766)
- [ ] Bot responds to Discord messages
- [ ] Memory API stores and retrieves memories
- [ ] Embedding service generates embeddings
