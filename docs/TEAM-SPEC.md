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
      "model": "anthropic/claude-sonnet-4-6",
      "channel": "#general",
      "capabilities": ["web search", "memory", "exec", "browser", "cron", "sub-agents"]
    },
    {
      "id": "sourcing",
      "name": "Sourcing Agent",
      "type": "agent",
      "role": "Specialist",
      "domain": "Product research, supplier discovery, pricing analysis",
      "model": "anthropic/claude-sonnet-4-6",
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
| `model` | yes | LLM model ID |
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

## User Management

Users can only be added through two channels:

### 1. Setup wizard (initial install)
The owner adds team members during first-time setup. The wizard prompts for each user's details and writes them to the registry.

### 2. Owner tells T.A.R.S (post-install)
```
"Add Alice as an admin. Her Discord ID is 987654321098765432,
email alice@example.com, WeChat alice_wx, timezone UTC+8.
She handles sourcing from Shenzhen."
```
T.A.R.S updates the registry, syncs OpenClaw config, and restarts the gateway.

**Only the owner can add team members.** T.A.R.S verifies the request comes from an owner-level user before making changes. No standalone scripts — team management is always mediated by the coordinator agent or the setup wizard.

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
