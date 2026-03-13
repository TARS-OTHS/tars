# TARS Multi-Agent Specification

## Principles

1. **Start small, grow from need.** Begin with T.A.R.S as the sole agent. Add specialists only when a real limitation is hit — not speculatively.
2. **Flexible topology.** No fixed org chart. The agent network reshapes itself based on current needs. Any structure (hub-and-spoke, peer mesh, hierarchical) can emerge and change.
3. **Shared infrastructure, scoped context.** All agents share the auth proxy, embedding service, and memory API. But each agent's working memory is scoped — they don't pollute each other's recall.
4. **Humans stay in control.** Agents suggest new agents and structures. Humans approve and spin them up.

---

## Agent Model

### Identity

Every agent has:
- **ID** — unique identifier (e.g. `tars`, `sourcing`, `analytics`)
- **Role** — one of: `coordinator`, `specialist`, `assistant`
- **Domain** — what it's expert in (e.g. "product sourcing", "financial reporting")
- **SOUL.md** — personality, operating rules, constraints

### Roles

| Role | Description |
|------|-------------|
| **Coordinator** | Routes tasks, delegates to specialists, reports to humans. T.A.R.S is the default coordinator. |
| **Specialist** | Deep expertise in one domain. Called by coordinators or directly by humans. |
| **Assistant** | General-purpose, handles tasks that don't need a specialist. |

Any agent can talk to any other. Roles are conventions, not hard boundaries.

---

## Communication

Agents communicate through three mechanisms:

### 1. Direct Sessions (private, fast)
- `sessions_spawn` — start a new agent with a task
- `sessions_send` — send a message to a running agent
- `sessions_yield` — wait for a sub-agent's result

Best for: task delegation, getting a quick answer, private work.

### 2. Shared Channels (visible, collaborative)
- `message` — post in a Discord channel
- Any agent or human in that channel sees the message

Best for: collaborative problem-solving, status updates, decisions that affect multiple parties.

### 3. Memory (async, persistent)
- Agents write findings to shared memory scopes
- Other agents discover them via semantic search

Best for: knowledge sharing, handoffs between sessions, long-running context.

### Routing Guidelines

| Scenario | Method |
|----------|--------|
| T.A.R.S delegates a task to a specialist | Direct session |
| Two specialists need to collaborate | Shared channel |
| Agent produces knowledge others might need | Memory write |
| Agent needs context from another domain | Memory search |
| Human wants visibility into agent work | Shared channel |
| Quick factual query to another agent | Direct session |

---

## Memory Architecture

### Scoping

```
┌─────────────────────────────────────────────┐
│              Shared Knowledge               │
│  Company facts, product data, policies,     │
│  customer insights, market research         │
│  scope: "shared"                            │
├──────────┬──────────┬──────────┬────────────┤
│ T.A.R.S  │ Sourcing │ Finance  │ Analytics  │
│ Agent    │ Agent    │ Agent    │ Agent      │
│ memory   │ memory   │ memory   │ memory     │
│          │          │          │            │
│ Tasks,   │ Supplier │ Books,   │ Metrics,   │
│ routing  │ contacts │ invoices │ reports    │
│ decisions│ pricing  │ VAT/tax  │ trends     │
└──────────┴──────────┴──────────┴────────────┘
```

- **Agent-scoped memory**: Private to that agent. Session state, working context, domain-specific facts. Retrieved by default during auto-recall.
- **Shared memory**: Visible to all agents. Company knowledge, cross-domain facts. Written with `scope: "shared"`. Agents query it explicitly when they need cross-domain context.

An agent's auto-recall pulls from its own scope first, then supplements with relevant shared memories.

### Memory Types (unchanged)

- **Semantic** — facts, knowledge, preferences
- **Episodic** — events, conversations, outcomes
- **Procedural** — how-to, workflows, processes

---

## Adding a New Agent

### When to add

T.A.R.S (or a human) identifies that:
- A domain needs deeper expertise than T.A.R.S can provide
- A recurring task would benefit from dedicated context and memory
- Workload requires parallel execution

### How to add

1. **T.A.R.S suggests**: "This sourcing research needs a dedicated agent. Want me to set one up?"
2. **Human approves**
3. **Spin up**: `./scripts/add-agent.sh` (or T.A.R.S runs it via exec)
4. **Configure**: agent gets an ID, role, domain, SOUL.md, and optional Discord channel
5. **Available immediately**: other agents can spawn sessions with it

### Agent definition (minimal)

```yaml
id: sourcing
role: specialist
domain: Product research, supplier discovery, pricing analysis
model: anthropic/claude-sonnet-4-6
channel: "#sourcing"        # optional — for direct human access
```

---

## Topology

The network topology is not configured — it emerges from how agents communicate.

### Day 1: Solo
```
Human ↔ T.A.R.S
```

### Month 1: Hub-and-spoke
```
Human ↔ T.A.R.S ↔ Sourcing Agent
                 ↔ Finance Agent
```

### Month 3: Hybrid mesh
```
Human ↔ T.A.R.S ↔ Sourcing Agent ↔ Analytics Agent
  ↕                     ↕
Finance Agent    Platform Agent
```

Humans can talk to T.A.R.S or directly to specialists. Agents talk to each other as needed. The structure adapts.

### Complex goals

For multi-step objectives that span domains (e.g. "launch a new product line"):

1. Human tells T.A.R.S the goal
2. T.A.R.S breaks it into domain tasks
3. T.A.R.S spawns/delegates to specialists in parallel
4. Specialists collaborate via shared channel if needed
5. T.A.R.S aggregates results and reports back
6. Human reviews and adjusts

Agents don't self-organise into new structures autonomously. T.A.R.S proposes a plan, human approves, agents execute. This keeps things predictable while still being flexible.

---

## PROFLEX Business Domains

Potential specialist agents as needs emerge (not pre-built):

| Domain | Potential Agent | Triggers to Create |
|--------|----------------|-------------------|
| Product research & sourcing | `sourcing` | Volume of supplier/product queries exceeds what T.A.R.S handles well |
| Orders & shipping | `logistics` | Tracking, shipping coordination becomes a regular task |
| Platform management | `platform` | Listings, account health, customer comms need dedicated attention |
| VAT, tax, accounting | `finance` | Books, invoices, compliance needs domain expertise |
| Analytics & performance | `analytics` | Regular reporting, trend analysis, data synthesis |
| Advertising | `marketing` | Campaign management across channels |
| Customer research | `research` | Market analysis, competitor monitoring |

These are suggestions, not a plan. Each gets created only when T.A.R.S hits a limit.

---

## Security & Access

### Agent permissions
- All agents access the auth proxy (shared credentials)
- All agents can read/write to their own memory scope
- All agents can read shared memory
- Exec permissions are per-agent (configurable)
- Agents cannot access each other's workspaces directly

### Human access levels (future)

| Level | Access |
|-------|--------|
| **Owner** | All agents, all tools, exec, config changes |
| **Admin** | All agents, all tools, exec with approval |
| **User** | Assigned agents only, no exec, no config |

Start with owner-only. Add admin/user levels when multi-user becomes real.

---

## Setup Integration

The setup wizard offers:

```
Agent topology:
  1) Solo — single agent (recommended to start)
  2) Team — coordinator + specialists (configure after setup)
Choice [1]:
```

Option 1 creates T.A.R.S. Option 2 creates T.A.R.S and prompts for specialist definitions. Both can evolve after setup.

`scripts/add-agent.sh` handles adding agents post-install without re-running the full wizard.
