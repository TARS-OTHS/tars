# Codex — Business Knowledge Directory

The codex stores **stable business knowledge that has no API**. It gives agents the context they need to understand your business — brand identity, strategy, relationships, compliance, and processes.

## The Golden Rule

Before adding anything, ask: **"Can an agent query this from an API or tool?"**

| Answer | Action |
|--------|--------|
| **Yes** — data exists in an API, database, or tool | Don't put it in the codex. Add an API pointer if the tool isn't obvious. |
| **No, but it changes often** | Add a dated snapshot with "last updated" and a refresh cadence. |
| **No, and it's stable** | Put it in the codex. |

## Structure

```
codex/
├── _index.md              <- Master index — agents read this first
├── README.md              <- This file (how to use the codex)
├── business/              <- Brand voice, company profile, compliance
├── products/              <- Product info, data sheets, guidelines
├── strategy/              <- Playbooks, competitor analysis
└── processes/             <- SOPs, style guides, workflows
```

Organise however fits your business. The structure above is a starting point.

## How It Works

1. **`_index.md` is the entry point.** Agents read this first to understand what knowledge is available and where to find it. Keep it current.

2. **Agent CLAUDE.md references the codex.** Each agent's CLAUDE.md includes pointers to the codex sections relevant to its role. A marketing agent might reference brand voice and competitor briefs; an ops agent might reference contacts and processes.

3. **Codex vs. API — agents decide at query time.** The `_index.md` explicitly lists what should be queried from APIs vs. read from the codex, so agents don't use stale codex data when live data is available.

## Adding Content

### New document

1. Create the file in the appropriate subdirectory
2. Add a "Last updated: YYYY-MM-DD" line near the top
3. Add an entry in `_index.md` under the right section
4. If it contains volatile data, note the refresh cadence

### Updating existing documents

1. Update the content
2. Update the "Last updated" date
3. If the data is now available via an API, replace the content with an API pointer

### Removing documents

When an API becomes available for codex content, replace it with a pointer:
```
~~product-pricing.md~~ -> use `my_pricing_tool` instead
```

## What Does NOT Belong Here

- **Live data** (prices, stock, metrics) -> query via tools
- **Anything in git history** -> use `git log` / `git blame`
- **Conversation context** -> use agent memory system
- **Credentials or secrets** -> use the encrypted vault

## Tips

- Keep files focused. One topic per file, not mega-documents.
- Use markdown tables for structured data (contacts, registries).
- Date everything. A codex entry without a date is a liability.
- Prefer pointers over copies. "See `my_tool` for current data" beats a table that goes stale in a week.
