# TARS Team Specification

## Principles

1. **Everyone knows the team.** All agents have access to the full roster — humans and agents — including roles, responsibilities, and how to reach each other.
2. **Simple access levels.** Owner and admin only to start. Fine-grained permissions later if needed.
3. **One source of truth.** Team registry lives in one file, referenced by all agents and all config.

---

## Team Registry

A single file at `$TARS_HOME/config/team.json` containing both humans and agents:

```json
{
  "humans": [
    {
      "id": "peter",
      "name": "Peter",
      "type": "human",
      "access": "owner",
      "role": "Founder & CEO",
      "responsibilities": ["Strategy", "Operations", "Product decisions", "Finance oversight"],
      "context": "Based in Portugal. Manages all operations. Final decision-maker.",
      "contact": {
        "email": "peter@example.com",
        "phone": "+351...",
        "discord": "341650642709905408",
        "wechat": "peter_wx",
        "telegram": null,
        "signal": null
      },
      "preferences": {
        "timezone": "UTC+7",
        "language": "en",
        "notify_via": "discord"
      }
    },
    {
      "id": "alice",
      "name": "Alice",
      "type": "human",
      "access": "admin",
      "role": "Sourcing Lead",
      "responsibilities": ["Supplier discovery", "Price negotiation", "Sample management", "Quality control"],
      "context": "Based in Shenzhen. 5 years sourcing experience. Speaks Mandarin and English.",
      "contact": {
        "email": "alice@example.com",
        "phone": null,
        "discord": "987654321098765432",
        "wechat": "alice_wx",
        "telegram": null,
        "signal": null
      },
      "preferences": {
        "timezone": "UTC+8",
        "language": "en",
        "notify_via": "wechat"
      }
    }
  ],
  "agents": [
    {
      "id": "tars",
      "name": "T.A.R.S",
      "type": "agent",
      "role": "Coordinator",
      "domain": "Central operations — task routing, delegation, reporting",
      "model": null,
      "channel": "#general",
      "capabilities": ["web search", "memory", "exec", "browser", "cron", "sub-agents"]
    },
    {
      "id": "sourcing",
      "name": "Sourcing Agent",
      "type": "agent",
      "role": "Specialist",
      "domain": "Product research, supplier discovery, pricing analysis",
      "model": null,
      "channel": "#sourcing",
      "capabilities": ["web search", "memory", "browser"]
    }
  ]
}
```

### Human Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique identifier, used in config references |
| `name` | yes | Display name agents use when addressing the person |
| `access` | yes | System access level: `owner` or `admin` |
| `role` | yes | Job title or function (e.g. "Sourcing Lead", "Founder & CEO") |
| `responsibilities` | yes | List of what this person owns — agents use this to know who to involve |
| `context` | no | Free text — location, languages, experience, anything agents should know |
| `contact` | yes | All known communication channels (null if not available) |
| `preferences` | no | Timezone, language, preferred notification channel |

### Agent Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Agent ID matching OpenClaw config |
| `name` | yes | Display name |
| `role` | yes | `Coordinator`, `Specialist`, or `Assistant` |
| `domain` | yes | What this agent is expert in |
| `model` | no | LLM model ID (null = inherit gateway default) |
| `channel` | no | Primary Discord channel (null if no direct human access) |
| `capabilities` | yes | List of tool categories this agent has access to |

---

## Access Levels

### Owner
- Full access to all agents
- Exec auto-approved
- Can add/remove users
- Can add/remove agents
- Can modify system config
- Can view all sessions and memory

### Admin
- Full access to all agents
- Exec with approval (routed to owner via ops-alerts or Discord)
- Cannot modify system config
- Cannot add/remove other users
- Can view their own sessions
- Can write to shared memory

### Future: User (not implemented yet)
- Access to assigned agents only
- No exec
- No config visibility
- Scoped memory access

Starting with owner + admin keeps it simple. Add the user level when there's a real need.

---

## How Agents Know Who's Talking

### Discord → User Resolution

OpenClaw provides the Discord user ID with every message. TARS resolves it against the team registry:

```
Discord message from 341650642709905408
  → lookup team.json
  → inject into agent context:

    <user-context>
    Name: Peter
    Access: owner
    Role: Founder & CEO
    Responsibilities: Strategy, Operations, Product decisions, Finance oversight
    Timezone: UTC+7
    Context: Based in Portugal. Manages all operations. Final decision-maker.
    </user-context>

    <team>
    Humans: Peter (owner, CEO), Alice (admin, Sourcing Lead)
    Agents: T.A.R.S (coordinator), Sourcing Agent (specialist)
    </team>
```

This happens in the `before_prompt_build` hook — before the agent sees the message.

### Implementation

A lightweight OpenClaw plugin (`tars-team`) that:
1. Reads `config/team.json` at startup
2. On `before_prompt_build`, looks up the sender's Discord ID
3. Injects user context + team roster summary into the system prompt
4. Rejects messages from unknown Discord IDs (unless `allowUnknown: true`)

Every agent sees: who is talking to me, what's their role, and who else is on the team (humans and agents). This gives agents natural awareness of the full organisation.

---

## Discord Configuration

Each user's Discord ID goes into the OpenClaw allowlists automatically:

```json
{
  "channels": {
    "discord": {
      "dmPolicy": "allowlist",
      "allowFrom": ["341650642709905408", "987654321098765432"],
      "guilds": {
        "GUILD_ID": {
          "users": ["341650642709905408", "987654321098765432"],
          "requireMention": true
        }
      }
    }
  }
}
```

When a user is added to `team.json`, the config sync updates OpenClaw allowlists and restarts the gateway.

---

## Team Management

### First-Run Onboarding

On T.A.R.S's first conversation, it runs an onboarding flow:

1. **Introduce itself** — who it is, what it can do
2. **Complete the owner's profile** — the setup wizard only captures name and Discord ID. T.A.R.S asks for the rest: email, phone, timezone, role, responsibilities, preferred contact method, any other context
3. **Ask about the team** — "Who else works with you? Tell me about your team members."
4. **Collect details conversationally** — for each person: name, role, responsibilities, Discord ID, other contact methods, timezone, context
5. **Write `team.json`** — save the full roster
6. **Sync allowlists** — add all Discord IDs to OpenClaw config, restart gateway

This doesn't have to happen all at once. The owner can add team members over multiple conversations: "Add my accountant Maria, her Discord is X, email Y, she handles VAT and bookkeeping."

### Adding Team Members (post-install)

Only the owner can add team members, only through T.A.R.S:

```
"Add Alice as an admin. Her Discord ID is 987654321098765432,
email alice@example.com, WeChat alice_wx, timezone UTC+8.
She handles sourcing from Shenzhen."
```

T.A.R.S:
1. Verifies the request comes from an owner
2. Adds the member to `team.json`
3. Adds their Discord ID to `openclaw.json` allowlists
4. Restarts the gateway
5. Confirms: "Alice is now on the team. She can DM me or message in any channel."

**No setup wizard step, no scripts.** Team management is always conversational through T.A.R.S.

### Team Management Skill

T.A.R.S has a `team-management` skill (owner-only) that handles:

| Command | Action |
|---------|--------|
| "Add [name] as [role]" | Create new team member, collect details, sync config |
| "Update Alice's email to..." | Modify existing member |
| "Remove Bob from the team" | Remove member, revoke Discord access, sync config |
| "Show me the team" | Display full roster — humans and agents |
| "What does Alice do?" | Look up a specific member's role and responsibilities |

The skill wraps the exec calls needed to edit `team.json`, update `openclaw.json`, and restart the gateway. It validates all changes before applying them.

---

## Agent Behaviour Per User

Agents adapt based on user context:

| Aspect | Owner | Admin |
|--------|-------|-------|
| Detail level | Concise, assumes full context | More explanatory |
| Exec requests | Auto-approved | Routed for approval |
| Sensitive info | Full access | Filtered by domain |
| Notifications | Discord (or preferred channel) | Preferred channel from profile |
| Follow-ups | "I'll message you on Discord" | "I'll send Alice a WeChat message" |

Agents use the `notify_via` preference and contact details to reach users on the right platform. If T.A.R.S needs to notify Alice about a sourcing issue, it checks her profile and sends via WeChat, not Discord.

---

## Cross-Platform Messaging

Users may not all be on Discord. The contact registry enables agents to reach users on their preferred platform:

- **Discord** — native via OpenClaw channel
- **WeChat** — via WeChat integration (future)
- **Telegram** — via OpenClaw Telegram channel
- **Email** — via Gmail integration
- **Signal** — via OpenClaw Signal channel

T.A.R.S checks `notify_via` first, falls back to whatever contact method is available. The auth proxy handles credentials for each platform.

---

## Sync with OpenClaw

When `team.json` changes, the following must sync:

1. `openclaw.json` → `channels.discord.allowFrom` (all Discord IDs)
2. `openclaw.json` → `channels.discord.guilds.*.users` (all Discord IDs)
3. `exec-approvals.json` → per-user exec policies if needed
4. Gateway restart to pick up changes

T.A.R.S handles all of this when the owner requests a user change.
