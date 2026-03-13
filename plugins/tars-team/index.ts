/**
 * TARS Team Plugin
 *
 * Reads the team registry (team.json) and injects team context into agent
 * prompts. Provides tools for listing, inspecting, and managing team members.
 *
 * On `before_prompt_build`:
 *   - Resolves the sender by Discord user ID
 *   - Injects <user-context> and <team> blocks into the agent's system prompt
 *
 * Tools: team_list, team_get, team_add, team_update, team_remove, team_sync
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { execSync } from "node:child_process";

// ============================================================================
// Types
// ============================================================================

interface HumanContact {
  email?: string | null;
  phone?: string | null;
  discord?: string | null;
  wechat?: string | null;
  telegram?: string | null;
  signal?: string | null;
}

interface HumanPreferences {
  timezone?: string;
  language?: string;
  notify_via?: string;
}

interface HumanMember {
  id: string;
  name: string;
  type: "human";
  access: "owner" | "admin";
  role: string;
  responsibilities: string[];
  context?: string;
  contact: HumanContact;
  preferences?: HumanPreferences;
}

interface AgentMember {
  id: string;
  name: string;
  type: "agent";
  role: string;
  domain: string;
  model: string;
  channel?: string;
  capabilities: string[];
}

interface TeamRegistry {
  humans: HumanMember[];
  agents: AgentMember[];
}

// ============================================================================
// Config
// ============================================================================

type PluginConfig = {
  teamFilePath: string;
};

function parseConfig(raw: Record<string, unknown> | undefined): PluginConfig {
  return {
    teamFilePath: (raw?.teamFilePath as string) || "config/team.json",
  };
}

// ============================================================================
// Helpers
// ============================================================================

const TARS_HOME = process.env.TARS_HOME || process.cwd();

function resolveTeamPath(cfg: PluginConfig): string {
  return resolve(TARS_HOME, cfg.teamFilePath);
}

function readTeam(teamPath: string): TeamRegistry {
  if (!existsSync(teamPath)) {
    return { humans: [], agents: [] };
  }
  const raw = readFileSync(teamPath, "utf-8");
  return JSON.parse(raw) as TeamRegistry;
}

function writeTeam(teamPath: string, team: TeamRegistry): void {
  writeFileSync(teamPath, JSON.stringify(team, null, 2) + "\n", "utf-8");
}

function findHumanByDiscordId(team: TeamRegistry, discordId: string): HumanMember | undefined {
  return team.humans.find((h) => h.contact?.discord === discordId);
}

function findMemberById(team: TeamRegistry, id: string): HumanMember | AgentMember | undefined {
  return team.humans.find((h) => h.id === id) || team.agents.find((a) => a.id === id);
}

function isOwner(team: TeamRegistry, discordId: string): boolean {
  const human = findHumanByDiscordId(team, discordId);
  return human?.access === "owner";
}

function getAllDiscordIds(team: TeamRegistry): string[] {
  return team.humans
    .map((h) => h.contact?.discord)
    .filter((id): id is string => typeof id === "string" && id.length > 0);
}

/**
 * Sync Discord allowlists in openclaw.json with all Discord IDs from team.json,
 * then restart the gateway.
 */
function syncOpenClawConfig(team: TeamRegistry, logger: { info: (msg: string) => void; warn: (msg: string) => void }): string {
  const ocPath = resolve(TARS_HOME, ".openclaw/openclaw.json");
  if (!existsSync(ocPath)) {
    return "Warning: openclaw.json not found at " + ocPath + " — skipping sync.";
  }

  const discordIds = getAllDiscordIds(team);
  const ocRaw = readFileSync(ocPath, "utf-8");
  const oc = JSON.parse(ocRaw) as Record<string, unknown>;

  // Navigate to channels.discord
  const channels = (oc.channels || {}) as Record<string, unknown>;
  const discord = (channels.discord || {}) as Record<string, unknown>;

  // Update allowFrom
  discord.allowFrom = discordIds;

  // Update guilds.*.users
  const guilds = (discord.guilds || {}) as Record<string, Record<string, unknown>>;
  for (const guildId of Object.keys(guilds)) {
    guilds[guildId].users = discordIds;
  }

  discord.guilds = guilds;
  channels.discord = discord;
  oc.channels = channels;

  writeFileSync(ocPath, JSON.stringify(oc, null, 2) + "\n", "utf-8");
  logger.info("tars-team: updated openclaw.json allowlists with " + discordIds.length + " Discord IDs");

  // Restart gateway
  try {
    execSync("openclaw gateway restart", { timeout: 15000, stdio: "pipe" });
    logger.info("tars-team: gateway restarted");
    return "Synced " + discordIds.length + " Discord IDs to openclaw.json and restarted gateway.";
  } catch (err) {
    const msg = "Gateway restart failed: " + String(err);
    logger.warn("tars-team: " + msg);
    return "Synced " + discordIds.length + " Discord IDs to openclaw.json but gateway restart failed. Run `openclaw gateway restart` manually.";
  }
}

function formatHumanSummary(h: HumanMember): string {
  return `${h.name} (${h.access}, ${h.role})`;
}

function formatAgentSummary(a: AgentMember): string {
  return `${a.name} (${a.role.toLowerCase()})`;
}

function buildUserContextBlock(human: HumanMember): string {
  const lines = [
    `Name: ${human.name}`,
    `Access: ${human.access}`,
    `Role: ${human.role}`,
    `Responsibilities: ${human.responsibilities.join(", ")}`,
  ];
  if (human.preferences?.timezone) {
    lines.push(`Timezone: ${human.preferences.timezone}`);
  }
  if (human.context) {
    lines.push(`Context: ${human.context}`);
  }
  return `<user-context>\n${lines.join("\n")}\n</user-context>`;
}

function buildTeamBlock(team: TeamRegistry): string {
  const humansList = team.humans.map(formatHumanSummary).join(", ");
  const agentsList = team.agents.map(formatAgentSummary).join(", ");
  const lines: string[] = [];
  if (humansList) lines.push(`Humans: ${humansList}`);
  if (agentsList) lines.push(`Agents: ${agentsList}`);
  return `<team>\n${lines.join("\n")}\n</team>`;
}

// ============================================================================
// Plugin
// ============================================================================

const tarsTeamPlugin = {
  id: "tars-team",
  name: "TARS Team",
  description: "Team registry — context injection and team management tools",
  kind: "tools" as const,

  register(api: OpenClawPluginApi) {
    const cfg = parseConfig(api.pluginConfig as Record<string, unknown>);
    const teamPath = resolveTeamPath(cfg);

    api.logger.info(`tars-team: registered (teamFile: ${teamPath})`);

    // Load team once at startup to validate
    let team: TeamRegistry;
    try {
      team = readTeam(teamPath);
      api.logger.info(`tars-team: loaded ${team.humans.length} humans, ${team.agents.length} agents`);
    } catch (err) {
      api.logger.warn(`tars-team: failed to read team.json: ${String(err)}`);
      team = { humans: [], agents: [] };
    }

    // ========================================================================
    // Lifecycle: before_prompt_build — inject user context and team roster
    // ========================================================================

    api.on("before_prompt_build", async (event) => {
      // Re-read team.json each time to pick up changes
      let currentTeam: TeamRegistry;
      try {
        currentTeam = readTeam(teamPath);
      } catch {
        return;
      }

      const discordUserId = event.senderDiscordId || event.senderId;
      if (!discordUserId) return;

      const sender = findHumanByDiscordId(currentTeam, discordUserId);
      const blocks: string[] = [];

      if (sender) {
        blocks.push(buildUserContextBlock(sender));
      }

      if (currentTeam.humans.length > 0 || currentTeam.agents.length > 0) {
        blocks.push(buildTeamBlock(currentTeam));
      }

      if (blocks.length === 0) return;

      return {
        prependContext: blocks.join("\n\n"),
      };
    });

    // ========================================================================
    // Tool: team_list — show full team roster
    // ========================================================================

    api.registerTool(
      () => ({
        name: "team_list",
        label: "Team List",
        description:
          "Show the full team roster — all humans and agents, with their roles, " +
          "access levels, and key details.",
        parameters: Type.Object({}),
        async execute() {
          const currentTeam = readTeam(teamPath);

          if (currentTeam.humans.length === 0 && currentTeam.agents.length === 0) {
            return {
              content: [{ type: "text" as const, text: "No team members found. The team registry is empty." }],
              details: { humans: 0, agents: 0 },
            };
          }

          const sections: string[] = [];

          if (currentTeam.humans.length > 0) {
            const humanLines = currentTeam.humans.map((h) => {
              const parts = [`**${h.name}** (${h.id})`, `Role: ${h.role}`, `Access: ${h.access}`];
              if (h.responsibilities.length > 0) parts.push(`Responsibilities: ${h.responsibilities.join(", ")}`);
              if (h.context) parts.push(`Context: ${h.context}`);
              if (h.preferences?.timezone) parts.push(`Timezone: ${h.preferences.timezone}`);
              return parts.join("\n  ");
            });
            sections.push("## Humans\n" + humanLines.join("\n\n"));
          }

          if (currentTeam.agents.length > 0) {
            const agentLines = currentTeam.agents.map((a) => {
              const parts = [`**${a.name}** (${a.id})`, `Role: ${a.role}`, `Domain: ${a.domain}`];
              if (a.channel) parts.push(`Channel: ${a.channel}`);
              parts.push(`Capabilities: ${a.capabilities.join(", ")}`);
              return parts.join("\n  ");
            });
            sections.push("## Agents\n" + agentLines.join("\n\n"));
          }

          return {
            content: [{ type: "text" as const, text: sections.join("\n\n") }],
            details: { humans: currentTeam.humans.length, agents: currentTeam.agents.length },
          };
        },
      }),
      { name: "team_list" },
    );

    // ========================================================================
    // Tool: team_get — get details for a specific member
    // ========================================================================

    api.registerTool(
      () => ({
        name: "team_get",
        label: "Team Get Member",
        description:
          "Get full details for a specific team member by their ID. " +
          "Works for both humans and agents.",
        parameters: Type.Object({
          id: Type.String({ description: "The team member's unique ID (e.g. 'peter', 'alice', 'tars')" }),
        }),
        async execute(_toolCallId, params) {
          const { id } = params as { id: string };
          const currentTeam = readTeam(teamPath);
          const member = findMemberById(currentTeam, id);

          if (!member) {
            return {
              content: [{ type: "text" as const, text: `No team member found with ID "${id}".` }],
              details: { found: false },
            };
          }

          return {
            content: [{ type: "text" as const, text: JSON.stringify(member, null, 2) }],
            details: { found: true, member },
          };
        },
      }),
      { name: "team_get" },
    );

    // ========================================================================
    // Tool: team_add — add a new human team member (owner-only)
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "team_add",
        label: "Team Add Member",
        description:
          "Add a new human team member to the registry. Owner-only. " +
          "After adding, automatically syncs Discord allowlists and restarts the gateway.",
        parameters: Type.Object({
          id: Type.String({ description: "Unique identifier for the member (lowercase, no spaces)" }),
          name: Type.String({ description: "Display name" }),
          access: Type.String({ description: "Access level: 'owner' or 'admin'" }),
          role: Type.String({ description: "Job title or function (e.g. 'Sourcing Lead')" }),
          responsibilities: Type.Array(Type.String(), { description: "List of responsibilities" }),
          context: Type.Optional(Type.String({ description: "Free text — location, languages, experience" })),
          email: Type.Optional(Type.String({ description: "Email address" })),
          phone: Type.Optional(Type.String({ description: "Phone number" })),
          discord: Type.Optional(Type.String({ description: "Discord user ID" })),
          wechat: Type.Optional(Type.String({ description: "WeChat ID" })),
          telegram: Type.Optional(Type.String({ description: "Telegram handle" })),
          signal: Type.Optional(Type.String({ description: "Signal number/handle" })),
          timezone: Type.Optional(Type.String({ description: "Timezone (e.g. 'UTC+8')" })),
          language: Type.Optional(Type.String({ description: "Preferred language (e.g. 'en')" })),
          notify_via: Type.Optional(Type.String({ description: "Preferred notification channel (e.g. 'discord', 'wechat')" })),
        }),
        async execute(_toolCallId, params) {
          const p = params as {
            id: string; name: string; access: string; role: string;
            responsibilities: string[]; context?: string;
            email?: string; phone?: string; discord?: string;
            wechat?: string; telegram?: string; signal?: string;
            timezone?: string; language?: string; notify_via?: string;
          };

          // Owner check
          const currentTeam = readTeam(teamPath);
          const senderDiscordId = ctx.senderDiscordId || ctx.senderId;
          if (!senderDiscordId || !isOwner(currentTeam, senderDiscordId)) {
            return {
              content: [{ type: "text" as const, text: "Permission denied. Only owners can add team members." }],
              details: { error: "not_owner" },
            };
          }

          // Check for duplicate ID
          if (findMemberById(currentTeam, p.id)) {
            return {
              content: [{ type: "text" as const, text: `A team member with ID "${p.id}" already exists. Use team_update to modify.` }],
              details: { error: "duplicate_id" },
            };
          }

          // Validate access level
          if (p.access !== "owner" && p.access !== "admin") {
            return {
              content: [{ type: "text" as const, text: `Invalid access level "${p.access}". Must be "owner" or "admin".` }],
              details: { error: "invalid_access" },
            };
          }

          const newMember: HumanMember = {
            id: p.id,
            name: p.name,
            type: "human",
            access: p.access as "owner" | "admin",
            role: p.role,
            responsibilities: p.responsibilities,
            context: p.context || undefined,
            contact: {
              email: p.email || null,
              phone: p.phone || null,
              discord: p.discord || null,
              wechat: p.wechat || null,
              telegram: p.telegram || null,
              signal: p.signal || null,
            },
            preferences: {
              timezone: p.timezone || undefined,
              language: p.language || "en",
              notify_via: p.notify_via || "discord",
            },
          };

          currentTeam.humans.push(newMember);
          writeTeam(teamPath, currentTeam);

          // Sync allowlists
          const syncResult = syncOpenClawConfig(currentTeam, api.logger);

          return {
            content: [{
              type: "text" as const,
              text: `Added ${p.name} (${p.id}) to the team as ${p.access}.\n${syncResult}`,
            }],
            details: { action: "added", member: newMember, sync: syncResult },
          };
        },
      }),
      { name: "team_add" },
    );

    // ========================================================================
    // Tool: team_update — update an existing member (owner-only)
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "team_update",
        label: "Team Update Member",
        description:
          "Update an existing human team member's details. Owner-only. " +
          "Only provided fields are updated; omitted fields stay unchanged. " +
          "After updating, automatically syncs Discord allowlists and restarts the gateway.",
        parameters: Type.Object({
          id: Type.String({ description: "ID of the member to update" }),
          name: Type.Optional(Type.String({ description: "New display name" })),
          access: Type.Optional(Type.String({ description: "New access level: 'owner' or 'admin'" })),
          role: Type.Optional(Type.String({ description: "New role" })),
          responsibilities: Type.Optional(Type.Array(Type.String(), { description: "New responsibilities list (replaces existing)" })),
          context: Type.Optional(Type.String({ description: "New context text" })),
          email: Type.Optional(Type.String({ description: "New email" })),
          phone: Type.Optional(Type.String({ description: "New phone" })),
          discord: Type.Optional(Type.String({ description: "New Discord user ID" })),
          wechat: Type.Optional(Type.String({ description: "New WeChat ID" })),
          telegram: Type.Optional(Type.String({ description: "New Telegram handle" })),
          signal: Type.Optional(Type.String({ description: "New Signal number/handle" })),
          timezone: Type.Optional(Type.String({ description: "New timezone" })),
          language: Type.Optional(Type.String({ description: "New language" })),
          notify_via: Type.Optional(Type.String({ description: "New notification preference" })),
        }),
        async execute(_toolCallId, params) {
          const p = params as Record<string, unknown>;
          const id = p.id as string;

          const currentTeam = readTeam(teamPath);
          const senderDiscordId = ctx.senderDiscordId || ctx.senderId;
          if (!senderDiscordId || !isOwner(currentTeam, senderDiscordId)) {
            return {
              content: [{ type: "text" as const, text: "Permission denied. Only owners can update team members." }],
              details: { error: "not_owner" },
            };
          }

          const idx = currentTeam.humans.findIndex((h) => h.id === id);
          if (idx === -1) {
            return {
              content: [{ type: "text" as const, text: `No human team member found with ID "${id}".` }],
              details: { error: "not_found" },
            };
          }

          const member = currentTeam.humans[idx];

          // Update top-level fields
          if (p.name !== undefined) member.name = p.name as string;
          if (p.access !== undefined) {
            const access = p.access as string;
            if (access !== "owner" && access !== "admin") {
              return {
                content: [{ type: "text" as const, text: `Invalid access level "${access}". Must be "owner" or "admin".` }],
                details: { error: "invalid_access" },
              };
            }
            member.access = access as "owner" | "admin";
          }
          if (p.role !== undefined) member.role = p.role as string;
          if (p.responsibilities !== undefined) member.responsibilities = p.responsibilities as string[];
          if (p.context !== undefined) member.context = p.context as string;

          // Update contact fields
          if (!member.contact) member.contact = {};
          if (p.email !== undefined) member.contact.email = p.email as string;
          if (p.phone !== undefined) member.contact.phone = p.phone as string;
          if (p.discord !== undefined) member.contact.discord = p.discord as string;
          if (p.wechat !== undefined) member.contact.wechat = p.wechat as string;
          if (p.telegram !== undefined) member.contact.telegram = p.telegram as string;
          if (p.signal !== undefined) member.contact.signal = p.signal as string;

          // Update preferences
          if (!member.preferences) member.preferences = {};
          if (p.timezone !== undefined) member.preferences.timezone = p.timezone as string;
          if (p.language !== undefined) member.preferences.language = p.language as string;
          if (p.notify_via !== undefined) member.preferences.notify_via = p.notify_via as string;

          currentTeam.humans[idx] = member;
          writeTeam(teamPath, currentTeam);

          const syncResult = syncOpenClawConfig(currentTeam, api.logger);

          return {
            content: [{
              type: "text" as const,
              text: `Updated ${member.name} (${id}).\n${syncResult}`,
            }],
            details: { action: "updated", member, sync: syncResult },
          };
        },
      }),
      { name: "team_update" },
    );

    // ========================================================================
    // Tool: team_remove — remove a team member (owner-only)
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "team_remove",
        label: "Team Remove Member",
        description:
          "Remove a human team member from the registry. Owner-only. " +
          "After removing, automatically syncs Discord allowlists and restarts the gateway.",
        parameters: Type.Object({
          id: Type.String({ description: "ID of the member to remove" }),
        }),
        async execute(_toolCallId, params) {
          const { id } = params as { id: string };

          const currentTeam = readTeam(teamPath);
          const senderDiscordId = ctx.senderDiscordId || ctx.senderId;
          if (!senderDiscordId || !isOwner(currentTeam, senderDiscordId)) {
            return {
              content: [{ type: "text" as const, text: "Permission denied. Only owners can remove team members." }],
              details: { error: "not_owner" },
            };
          }

          const idx = currentTeam.humans.findIndex((h) => h.id === id);
          if (idx === -1) {
            return {
              content: [{ type: "text" as const, text: `No human team member found with ID "${id}".` }],
              details: { error: "not_found" },
            };
          }

          const removed = currentTeam.humans.splice(idx, 1)[0];
          writeTeam(teamPath, currentTeam);

          const syncResult = syncOpenClawConfig(currentTeam, api.logger);

          return {
            content: [{
              type: "text" as const,
              text: `Removed ${removed.name} (${id}) from the team.\n${syncResult}`,
            }],
            details: { action: "removed", member: removed, sync: syncResult },
          };
        },
      }),
      { name: "team_remove" },
    );

    // ========================================================================
    // Tool: team_sync — sync Discord allowlists and restart gateway
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "team_sync",
        label: "Team Sync",
        description:
          "Sync Discord allowlists in openclaw.json with all Discord IDs from team.json, " +
          "then restart the gateway. Owner-only. Use after manual edits to team.json or " +
          "if the gateway config is out of sync.",
        parameters: Type.Object({}),
        async execute() {
          const currentTeam = readTeam(teamPath);
          const senderDiscordId = ctx.senderDiscordId || ctx.senderId;
          if (!senderDiscordId || !isOwner(currentTeam, senderDiscordId)) {
            return {
              content: [{ type: "text" as const, text: "Permission denied. Only owners can sync team config." }],
              details: { error: "not_owner" },
            };
          }

          const syncResult = syncOpenClawConfig(currentTeam, api.logger);

          return {
            content: [{ type: "text" as const, text: syncResult }],
            details: { action: "synced" },
          };
        },
      }),
      { name: "team_sync" },
    );
  },
};

export default tarsTeamPlugin;
