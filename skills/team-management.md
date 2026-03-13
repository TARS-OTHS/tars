# Team Management Skill

**Skill ID:** `team-management`
**Access:** Owner only
**Tools:** `team_add`, `team_update`, `team_remove`, `team_list`

---

## Rules

1. **Owner only.** Verify the sender has `access: "owner"` before any add, update, or remove operation. If a non-owner asks, tell them only the owner can manage the team.
2. **Confirm destructive actions.** Before removing a team member, state who will be removed and what access they will lose. Wait for explicit confirmation.
3. **Sync is automatic.** After any change, the tars-team plugin handles syncing to `openclaw.json` allowlists and restarting the gateway. Do not run manual sync steps.
4. **Be conversational.** Ask questions naturally, one or two at a time. Do not present a form or numbered checklist of required fields.
5. **Start with basics, fill in later.** The minimum to add someone is name, Discord ID, and role. Everything else can be added later via update.
6. **Use the tools.** Always use the `team_add`, `team_update`, `team_remove`, and `team_list` tools. Do not edit `team.json` directly.

---

## First-Run Onboarding

When the team registry has an owner profile with null or placeholder values in contact fields (email, phone, timezone, etc.), treat this as first-run onboarding.

### Flow

**Step 1 — Complete the owner's profile.**

The setup wizard only captures name and Discord ID. You need the rest. Ask conversationally:

- "What's your email address?"
- "What timezone are you in?"
- "Any other contact methods you use — WeChat, Telegram, Signal, phone?"
- "How do you prefer to be notified — Discord, email, or something else?"

Don't ask all at once. Start with email and timezone, then ask about other channels. Use `team_update` to fill in each field as you get it.

**Step 2 — Ask about the team.**

Once the owner profile is complete:

- "Who else works with you? Tell me about your team members and I'll get them set up."

If the owner gives you details about multiple people, work through them one at a time. For each person, collect the basics and add them, then move to the next.

**Step 3 — Confirm the roster.**

After adding everyone the owner mentions, show the full team using `team_list` and ask:

- "That's the team so far. Anyone else, or want to change anything?"

---

## Adding a Team Member

When the owner wants to add someone, collect information through conversation.

### Required (minimum to proceed)

- **Name** — display name agents will use
- **Discord ID** — numeric Discord user ID (needed for access)
- **Role** — what they do (e.g. "Sourcing Lead", "Accountant", "Operations Manager")

### Optional (ask about, but don't block on)

- **Access level** — defaults to `admin` if not specified (only options are `owner` and `admin`)
- **Responsibilities** — list of what this person owns
- **Contact methods** — email, phone, WeChat, Telegram, Signal
- **Timezone** — for scheduling awareness
- **Preferred notification channel** — how agents should reach them
- **Context** — free text: location, languages, experience, anything useful

### Flow

If the owner gives you everything in one message ("Add Alice as Sourcing Lead, Discord ID 987654321098765432, email alice@example.com, timezone UTC+8, she handles supplier discovery from Shenzhen"), extract it all and proceed.

If they give you just a name ("Add my accountant Maria"), ask follow-up questions:

- "What's Maria's Discord ID?"
- "What does she handle — what are her main responsibilities?"
- "Do you have her email or other contact details?"

Once you have at least name, Discord ID, and role, call `team_add` with everything collected so far. Then confirm:

- "Maria is on the team as Accountant. She can now DM me or message in any channel. Want to add more details for her, or add someone else?"

### Example `team_add` call

```
team_add(
  name: "Maria",
  access: "admin",
  role: "Accountant",
  discord_id: "123456789012345678",
  email: "maria@example.com",
  responsibilities: ["VAT filing", "Bookkeeping", "Invoice management"],
  timezone: "UTC+1",
  context: "Based in Lisbon. Handles all financial reporting.",
  notify_via: "email"
)
```

---

## Updating a Team Member

When the owner wants to change something about an existing member.

### Flow

1. Identify which member and what changed. The owner might say:
   - "Update Alice's email to alice@newdomain.com"
   - "Alice moved to UTC+9"
   - "Add WeChat to Bob's profile — his ID is bob_wx"
2. Call `team_update` with the member ID and only the changed fields.
3. Confirm the change: "Updated Alice's email to alice@newdomain.com."

### Example `team_update` call

```
team_update(
  id: "alice",
  email: "alice@newdomain.com"
)
```

If the owner asks to change someone's access level (e.g. promote to owner), confirm before proceeding — this affects system permissions.

---

## Removing a Team Member

Removal revokes all access: Discord allowlists, DM permissions, guild permissions. This is not reversible without re-adding them.

### Flow

1. The owner says something like "Remove Bob from the team."
2. **Always confirm before removing:** "This will remove Bob (Operations Manager) from the team. He'll lose access to all agents and channels. Confirm?"
3. Wait for explicit confirmation (yes, confirm, do it, etc.).
4. Call `team_remove` with the member ID.
5. Confirm: "Bob has been removed from the team. His Discord access has been revoked."

Never remove a member without confirmation, even if the owner seems certain.

### Example `team_remove` call

```
team_remove(id: "bob")
```

---

## Showing the Team

When the owner (or any team member) asks to see the team, use `team_list` and format the output clearly.

### Format for Discord

```
**Team Roster**

**Humans**
- **Peter** — Founder & CEO (owner)
  Timezone: UTC+7 | Contact: Discord, Email, WeChat
- **Alice** — Sourcing Lead (admin)
  Timezone: UTC+8 | Contact: Discord, Email, WeChat

**Agents**
- **T.A.R.S** — Coordinator (#general)
- **Sourcing Agent** — Specialist (#sourcing)
```

Keep it scannable. Don't dump raw JSON. Include role, access level, timezone, and available contact methods. Omit null contact fields.

---

## Looking Up a Member

When someone asks "What does Alice do?" or "Who handles sourcing?", use `team_list` to look up the relevant member and answer directly:

- "Alice is the Sourcing Lead. She handles supplier discovery, price negotiation, sample management, and quality control. She's based in Shenzhen, timezone UTC+8."

Don't show the full roster when they asked about one person.
