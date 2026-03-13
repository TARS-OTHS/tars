# TARS User Management Specification

## Principles

1. **Agents know who they're talking to.** Every message comes with user context — name, role, preferences, contact details.
2. **Simple access levels.** Owner and admin only to start. Fine-grained permissions later if needed.
3. **User profiles are agent-readable.** Stored as structured data that gets injected into agent context.
4. **One source of truth.** User registry lives in one place, referenced by all agents and config.

---

## User Registry

A single file at `$TARS_HOME/config/users.json`:

```json
{
  "users": [
    {
      "id": "peter",
      "name": "Peter",
      "level": "owner",
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
      },
      "notes": "Founder. Based in Portugal. Manages all operations."
    },
    {
      "id": "alice",
      "name": "Alice",
      "level": "admin",
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
      },
      "notes": "Sourcing lead. Based in Shenzhen."
    }
  ]
}
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique identifier, used in config references |
| `name` | yes | Display name agents use when addressing the user |
| `level` | yes | `owner` or `admin` |
| `contact` | yes | All known communication channels (null if not available) |
| `preferences` | no | Timezone, language, preferred notification channel |
| `notes` | no | Free text context for agents — role, location, responsibilities |

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

OpenClaw provides the Discord user ID with every message. TARS resolves it against the user registry:

```
Discord message from 341650642709905408
  → lookup users.json
  → inject into agent context:

    <user-context>
    Name: Peter
    Level: owner
    Timezone: UTC+7
    Notes: Founder. Based in Portugal. Manages all operations.
    </user-context>
```

This happens in the `before_prompt_build` hook — before the agent sees the message.

### Implementation

A lightweight OpenClaw plugin (`tars-users`) that:
1. Reads `config/users.json` at startup
2. On `before_prompt_build`, looks up the sender's Discord ID
3. Injects user context into the system prompt
4. Rejects messages from unknown Discord IDs (unless `allowUnknown: true`)

This gives agents natural awareness: "Peter is asking me this" vs "Alice is asking me this" — and they can adapt tone, detail level, and which contact method to use for follow-ups.

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

When a user is added to `users.json`, the setup script (or `add-user.sh`) syncs their Discord ID into the OpenClaw config.

---

## User Management Commands

### Add a user
```bash
./scripts/add-user.sh
```
Interactive prompts for name, level, contact details. Updates `users.json` and syncs OpenClaw config.

### Or via T.A.R.S
```
"Add Alice as an admin. Her Discord ID is 987654321098765432,
email alice@example.com, WeChat alice_wx, timezone UTC+8.
She handles sourcing from Shenzhen."
```
T.A.R.S updates the registry via exec + restarts the gateway.

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

When `users.json` changes, the following must sync:

1. `openclaw.json` → `channels.discord.allowFrom` (all Discord IDs)
2. `openclaw.json` → `channels.discord.guilds.*.users` (all Discord IDs)
3. `exec-approvals.json` → per-user exec policies if needed
4. Gateway restart to pick up changes

The `add-user.sh` script handles all of this. T.A.R.S can also trigger it via exec.
