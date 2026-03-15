# TARS Roadmap

Last updated: 2026-03-15

---

## Now — Before Daily Use

- [ ] **Sandbox T.A.R.S** — Move main agent from `host: gateway` to sandboxed execution. All infrastructure is ready (sandbox image, mcporter, no_proxy, credential injection).
- [ ] **Security hardening & alerts** — Green/orange/red severity levels for agent actions. `security-audit.sh` script (referenced in docs, doesn't exist). Cloud metadata iptables rule automation.
- [ ] **Skills selection in setup wizard** — Hybrid: some baked in, some chosen during setup.

---

## Soon — Once in Daily Operation

- [ ] **Voice STT↔TTS** — Groq STT + ElevenLabs TTS for Discord voice channel conversations. Architecture proposed, scaffolded. Implement Groq for all STT.
- [ ] **Voice call (Vapi)** — Phone call interface to agents via Vapi. Talk to T.A.R.S by calling a number.
- [ ] **Chat-based connector setup** — Add integrations conversationally via T.A.R.S (tokens entered via terminal, not in chat).
- [ ] **Control Rescue Bot from phone** — Mobile-friendly interface for managing agents on the go.
- [ ] **OC skills: core set** — summarize, blogwatcher, nano-pdf, nano banana pro, gemini integration.
- [ ] **Skills and tools for emerging requirements** — Build and register new skills as business needs surface.

---

## Big Bets — Strategic Capabilities

### OTHS Story Intelligence System
Build an intelligence layer for Off The Hook Solutions — aggregates, analyses, and surfaces business-relevant information.

### Research Skill
Automated research agent covering:
- Twitter, Reddit, web, YouTube, LinkedIn
- Finds new podcasts, books, blogs, sources and recommends them
- Watches for new episodes of things Peter likes, notifies and summarises
- Shopping, organising, and purchasing
- News aggregation and filtering

### Self-Improvement Methodology
Three-tier autonomous improvement system:
1. **Skill check** — Is the skill working? Basic alerts and health checks.
2. **Method check** — Is the improvement method working? No unintended consequences? Review output from skill.
3. **Meta check** — Is the checking system itself operating? Basic checks on the checker.

### Multiple Model Think Tanks
- Task-based model routing — different models for different work
- Multi-model deliberation for complex decisions
- Gemini integration alongside Claude

---

## Later — Nice to Have

- [ ] **WeChat + cross-platform messaging** — Beyond Discord
- [ ] **tars-mcp OC plugin** — Native tool surface for MCP tools (128 Google Workspace tools as first-class agent tools instead of mcporter CLI). Decided not needed yet — mcporter works.
- [ ] **Tavily OC plugin** — Native OpenClaw tool instead of exec→curl→proxy
- [ ] **Browser OC plugin** — Host-side Chrome for agents (sandbox gets bot-blocked)
- [ ] **Health dashboard** — System monitoring and agent health overview
- [ ] **CI/CD testing** — Automated test pipeline for TARS releases
- [ ] **User guide** — End-user documentation
