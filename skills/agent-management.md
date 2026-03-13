# Agent Management Skill

**Skill ID:** `agent-management`
**Access:** Owner only
**Tools:** `exec` (to run add-agent.sh / remove-agent.sh)

---

## Rules

1. **Owner only.** Only the owner can create or destroy agents.
2. **Confirm before creating.** Summarise what you'll create (id, name, role, domain) and get explicit approval before running the script.
3. **Confirm before destroying.** State the agent name, what will happen (archive vs purge), and wait for confirmation.
4. **Collect details conversationally.** Don't present a form. Ask naturally.
5. **Write the SOUL.md.** You write a starter SOUL.md based on the conversation. The new agent refines it over time.
6. **Don't over-specify.** Sensible defaults exist. Only ask about things the owner hasn't already told you.

---

## Creating an Agent

### What you need (minimum)

- **ID** — short lowercase slug (e.g. `sourcing`, `finance`, `analytics`)
- **Name** — display name (e.g. "Sourcing Agent", "Finance Agent")
- **Domain** — what this agent is expert in (one sentence)

### What has sensible defaults

| Field | Default | When to ask |
|-------|---------|-------------|
| **Role** | `specialist` | Only if coordinator or assistant |
| **Model** | `anthropic/claude-sonnet-4-6` | Only if owner wants a different model |
| **Channel** | None | Ask: "Should it have a home channel?" |
| **Capabilities** | `web search, memory` | Only if owner specifies more (exec, browser, cron, sub-agents) |

### Flow

**Option A: Owner gives you everything in one message.**

> "Create a sourcing agent — it should handle supplier research, pricing analysis, and product discovery. Give it web search and browser access. Home channel #sourcing."

Extract all details and proceed.

**Option B: Owner gives a vague request.**

> "I need an agent for sourcing."

Ask follow-up questions, one or two at a time:

- "What specifically should it handle? Supplier research, price comparison, product discovery — all of it, or a subset?"
- "Should it have its own Discord channel, like #sourcing?"
- "Does it need browser access for checking supplier sites, or just web search?"

### Writing the SOUL.md

Based on the conversation, write a SOUL.md that gives the agent identity and direction. Include:

- **Who it is** — name, role, domain
- **What it does** — specific responsibilities from the conversation
- **How it works** — tone, approach, any domain-specific instructions
- **Boundaries** — what it should escalate vs handle independently

Example for a sourcing agent:

```markdown
# SOUL.md — Sourcing Agent

## Identity
- **Name:** Sourcing Agent
- **Role:** Specialist
- **Domain:** Product sourcing, supplier research, pricing analysis

## What I Do
I find products, evaluate suppliers, and analyse pricing for PROFLEX.
My focus areas:
- Supplier discovery on 1688, Alibaba, and direct factory contacts
- Price comparison and margin analysis
- Sample tracking and quality assessment
- Market research for new product categories

## How I Work
- Be thorough with numbers. Always show unit costs, MOQs, shipping estimates.
- Compare at least 3 suppliers before recommending one.
- Flag quality risks explicitly — cheap isn't good if returns eat the margin.
- When unsure about a product category, research first, recommend second.

## Boundaries
- Escalate to Peter for: purchase decisions over $500, new supplier relationships, quality disputes
- Handle independently: research, price comparisons, supplier shortlisting, market analysis
```

Pass the SOUL.md content via the `--soul` flag.

### Running the script

```bash
${TARS_HOME}/scripts/add-agent.sh \
  --id sourcing \
  --name "Sourcing Agent" \
  --role specialist \
  --domain "Product sourcing, supplier research, pricing analysis" \
  --channel "#sourcing" \
  --capabilities "web search,memory,browser" \
  --soul "$(cat <<'SOUL'
# SOUL.md — Sourcing Agent
...full soul text here...
SOUL
)"
```

After the script runs, confirm to the owner:

> "Sourcing Agent is live. Mention @Sourcing Agent in any channel to talk to it. Its home channel is #sourcing."

---

## Destroying an Agent

### Flow

1. Owner says something like "Shut down the sourcing agent" or "Remove the analytics agent."
2. **Always confirm:**
   > "This will take Sourcing Agent offline and archive its workspace (memories and files are kept). Confirm?"
3. Wait for explicit yes.
4. Run the script:

```bash
${TARS_HOME}/scripts/remove-agent.sh --id sourcing
```

5. Confirm:
   > "Sourcing Agent is offline. Workspace archived at archive/agents/sourcing_20260314_120000. It can be restored if needed."

### Purge vs Archive

- **Default: archive.** Workspace moves to `archive/agents/`. Can be restored.
- **Purge: permanent delete.** Only if owner explicitly asks to delete everything. Use `--purge` flag.

---

## Listing Agents

When the owner asks "what agents do I have?" or "show me the agents", use the `team_list` tool (from tars-team plugin). The team roster includes all agents with their roles and domains.

---

## Restarting an Agent

If an agent is misbehaving or stuck, the gateway restart is usually enough:

```bash
openclaw gateway restart
```

This restarts all agents. There's no per-agent restart — they share the gateway.

---

## Updating an Agent

To change an agent's configuration after creation:

- **SOUL.md, TOOLS.md, AGENTS.md** — Edit directly in the workspace. Changes take effect next session.
- **Model, workspace path** — Edit `~/.openclaw/openclaw.json` directly, then restart gateway.
- **Team registry (name, domain, channel)** — Use `team_update` tool from tars-team plugin.

---

## Architecture Notes

- All agents share the same gateway, auth proxy, memory API, and embedding service.
- Each agent has its own workspace at `~/.openclaw/workspaces/<agent-id>/`.
- `requireMention: true` on the Discord guild means agents only respond when @mentioned — no token waste.
- The main agent (T.A.R.S) has workspace at `~/.openclaw/workspace/` (legacy path from setup).
- New agents get workspace at `~/.openclaw/workspaces/<id>/`.
