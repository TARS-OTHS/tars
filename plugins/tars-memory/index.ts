/**
 * TARS Memory Tools Plugin
 *
 * Native agent tools wrapping the TARS Memory API.
 * Agents get memory_search, memory_store, memory_context, session_state_save,
 * session_state_get as first-class tools — no curl or HTTP construction needed.
 *
 * Optional auto-recall injects relevant memories into agent context at session start.
 * Optional auto-session-state saves state when agent ends.
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";

// ============================================================================
// Config
// ============================================================================

type PluginConfig = {
  memoryApiUrl: string;
  autoRecall: boolean;
  autoSessionState: boolean;
  maxRecallResults: number;
};

function parseConfig(raw: Record<string, unknown> | undefined): PluginConfig {
  return {
    memoryApiUrl: (raw?.memoryApiUrl as string) || process.env.MEMORY_API_URL || "http://172.17.0.1:8897",
    autoRecall: raw?.autoRecall !== false,
    autoSessionState: raw?.autoSessionState !== false,
    maxRecallResults: (raw?.maxRecallResults as number) || 5,
  };
}

// ============================================================================
// HTTP helper
// ============================================================================

async function apiCall(baseUrl: string, path: string, opts?: {
  method?: string;
  body?: unknown;
  params?: Record<string, string>;
}): Promise<unknown> {
  let url = `${baseUrl}${path}`;
  if (opts?.params) {
    const qs = new URLSearchParams(opts.params).toString();
    if (qs) url += `?${qs}`;
  }

  const res = await fetch(url, {
    method: opts?.method || "GET",
    headers: opts?.body ? { "Content-Type": "application/json" } : undefined,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`Memory API ${opts?.method || "GET"} ${path} failed (${res.status}): ${text}`);
  }

  return res.json();
}

// ============================================================================
// Plugin
// ============================================================================

const tarsMemoryPlugin = {
  id: "tars-memory",
  name: "TARS Memory",
  description: "Persistent memory tools backed by the TARS Memory API",
  kind: "memory" as const,

  register(api: OpenClawPluginApi) {
    const cfg = parseConfig(api.pluginConfig as Record<string, unknown>);
    const API = cfg.memoryApiUrl;

    api.logger.info(`tars-memory: registered (api: ${API}, autoRecall: ${cfg.autoRecall})`);

    // ========================================================================
    // Tool: memory_search — full-text search
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "memory_search",
        label: "Memory Search",
        description:
          "Search through persistent memory using full-text search. " +
          "Use to find past decisions, facts, project context, user preferences, or anything previously stored.",
        parameters: Type.Object({
          query: Type.String({ description: "Search query — keywords or natural language" }),
          limit: Type.Optional(Type.Number({ description: "Max results (default: 10)" })),
          type: Type.Optional(Type.String({ description: "Filter by type: semantic, episodic, procedural" })),
          after: Type.Optional(Type.String({ description: "Only memories after this time, e.g. '7d', '2h', '2024-01-01'" })),
        }),
        async execute(_toolCallId, params) {
          const { query, limit, type, after } = params as {
            query: string; limit?: number; type?: string; after?: string;
          };

          const qp: Record<string, string> = {
            q: query,
            agent: ctx.agentId || "main",
            limit: String(limit || 10),
          };
          if (type) qp.type = type;
          if (after) qp.after = after;

          const data = await apiCall(API, "/memory/search", { params: qp }) as {
            results?: Array<{ id: string; content: string; type: string; category: string; confidence: number; tags?: string[] }>;
          };

          const results = data.results || [];
          if (results.length === 0) {
            return {
              content: [{ type: "text" as const, text: "No memories found matching that query." }],
              details: { count: 0 },
            };
          }

          const text = results
            .map((m, i) => `${i + 1}. [${m.category}] ${m.content} (confidence: ${(m.confidence * 100).toFixed(0)}%)`)
            .join("\n");

          return {
            content: [{ type: "text" as const, text: `Found ${results.length} memories:\n\n${text}` }],
            details: { count: results.length, results },
          };
        },
      }),
      { name: "memory_search" },
    );

    // ========================================================================
    // Tool: memory_semantic_search — embedding-based similarity search
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "memory_semantic_search",
        label: "Memory Semantic Search",
        description:
          "Search memories by meaning using vector embeddings. " +
          "Better than text search for finding conceptually related memories even with different wording.",
        parameters: Type.Object({
          query: Type.String({ description: "Natural language query — searches by meaning, not keywords" }),
          limit: Type.Optional(Type.Number({ description: "Max results (default: 5)" })),
        }),
        async execute(_toolCallId, params) {
          const { query, limit } = params as { query: string; limit?: number };

          const data = await apiCall(API, "/memory/search/semantic", {
            method: "POST",
            body: { query, agent: ctx.agentId || "main", limit: limit || 5 },
          }) as {
            results?: Array<{ id: string; content: string; type: string; category: string; confidence: number; similarity: number }>;
          };

          const results = data.results || [];
          if (results.length === 0) {
            return {
              content: [{ type: "text" as const, text: "No semantically similar memories found." }],
              details: { count: 0 },
            };
          }

          const text = results
            .map((m, i) => `${i + 1}. [${m.category}] ${m.content} (similarity: ${(m.similarity * 100).toFixed(0)}%)`)
            .join("\n");

          return {
            content: [{ type: "text" as const, text: `Found ${results.length} similar memories:\n\n${text}` }],
            details: { count: results.length, results },
          };
        },
      }),
      { name: "memory_semantic_search" },
    );

    // ========================================================================
    // Tool: memory_store — save a memory
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "memory_store",
        label: "Memory Store",
        description:
          "Store information in persistent memory. Use for facts, decisions, user preferences, " +
          "project context, procedures — anything worth remembering across sessions.",
        parameters: Type.Object({
          content: Type.String({ description: "What to remember — be specific and self-contained" }),
          type: Type.Optional(Type.String({ description: "Memory type: semantic (facts/knowledge), episodic (events/experiences), procedural (how-to). Default: semantic" })),
          category: Type.Optional(Type.String({ description: "Category: general, system, project, user, business, people, infrastructure, procedural. Default: general" })),
          confidence: Type.Optional(Type.Number({ description: "Confidence 0-1. Default: 0.8" })),
          tags: Type.Optional(Type.Array(Type.String(), { description: "Tags for organization" })),
          pinned: Type.Optional(Type.Boolean({ description: "Pin to prevent decay. Use for critical info only." })),
        }),
        async execute(_toolCallId, params) {
          const { content, type, category, confidence, tags, pinned } = params as {
            content: string; type?: string; category?: string; confidence?: number;
            tags?: string[]; pinned?: boolean;
          };

          const memData: Record<string, unknown> = {
            content,
            type: type || "semantic",
            category: category || "general",
            confidence: confidence ?? 0.8,
          };
          if (tags) memData.tags = tags.join(",");
          if (pinned) memData.pinned = true;

          const data = await apiCall(API, "/memory/write", {
            method: "POST",
            body: {
              table: "memories",
              action: "insert",
              agent: ctx.agentId || "main",
              data: memData,
            },
          }) as { id?: string };

          return {
            content: [{ type: "text" as const, text: `Stored memory: "${content.slice(0, 100)}${content.length > 100 ? "..." : ""}"` }],
            details: { action: "created", id: data.id },
          };
        },
      }),
      { name: "memory_store" },
    );

    // ========================================================================
    // Tool: memory_context — get pinned, recent, conflicts, tasks
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "memory_context",
        label: "Memory Context",
        description:
          "Get current memory context: pinned memories, recent memories, unresolved conflicts, and active tasks. " +
          "Use at session start or when you need a broad overview of what's known.",
        parameters: Type.Object({}),
        async execute() {
          const data = await apiCall(API, "/memory/context", {
            params: { agent: ctx.agentId || "main" },
          }) as {
            pinned?: Array<{ content: string; category: string }>;
            recent?: Array<{ content: string; category: string }>;
            conflicts?: Array<{ description: string }>;
            tasks?: Array<{ title: string; status: string }>;
          };

          const sections: string[] = [];

          const pinned = data.pinned || [];
          if (pinned.length > 0) {
            sections.push("## Pinned Memories\n" + pinned.map(m => `- [${m.category}] ${m.content}`).join("\n"));
          }

          const recent = data.recent || [];
          if (recent.length > 0) {
            sections.push("## Recent Memories\n" + recent.map(m => `- [${m.category}] ${m.content}`).join("\n"));
          }

          const conflicts = data.conflicts || [];
          if (conflicts.length > 0) {
            sections.push(`## Unresolved Conflicts (${conflicts.length})\n` + conflicts.map(c => `- ${c.description}`).join("\n"));
          }

          const tasks = data.tasks || [];
          if (tasks.length > 0) {
            sections.push("## Active Tasks\n" + tasks.map(t => `- [${t.status}] ${t.title}`).join("\n"));
          }

          const text = sections.length > 0 ? sections.join("\n\n") : "No context available — memory is empty.";

          return {
            content: [{ type: "text" as const, text }],
            details: data,
          };
        },
      }),
      { name: "memory_context" },
    );

    // ========================================================================
    // Tool: session_state_save — save current session state
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "session_state_save",
        label: "Save Session State",
        description:
          "Save your current session state so you can resume after a reset. " +
          "Call this on task transitions, before long pauses, and before ending a session. " +
          "Include what you're working on, key context, and next steps.",
        parameters: Type.Object({
          task_summary: Type.String({ description: "What you're currently working on (1-2 sentences)" }),
          status: Type.Optional(Type.String({ description: "One of: active, completed, blocked, idle. Default: active" })),
          context: Type.Optional(Type.String({ description: "Key details needed for resumption — file names, decisions, next steps" })),
        }),
        async execute(_toolCallId, params) {
          const { task_summary, status, context } = params as {
            task_summary: string; status?: string; context?: string;
          };

          await apiCall(API, "/memory/session-state", {
            method: "POST",
            body: {
              agent: ctx.agentId || "main",
              task_summary,
              status: status || "active",
              context: context || "",
            },
          });

          return {
            content: [{ type: "text" as const, text: `Session state saved: "${task_summary}"` }],
            details: { action: "saved" },
          };
        },
      }),
      { name: "session_state_save" },
    );

    // ========================================================================
    // Tool: session_state_get — retrieve last session state
    // ========================================================================

    api.registerTool(
      (ctx) => ({
        name: "session_state_get",
        label: "Get Session State",
        description:
          "Retrieve the last saved session state. Use at session start to resume work. " +
          "Shows what you were working on, status, context, and when it was saved.",
        parameters: Type.Object({}),
        async execute() {
          const data = await apiCall(API, `/memory/session-state/${ctx.agentId || "main"}`) as {
            state?: { task_summary: string; status: string; context: string };
            updated_at?: string;
          };

          if (!data.state) {
            return {
              content: [{ type: "text" as const, text: "No previous session state found." }],
              details: { found: false },
            };
          }

          const s = data.state;
          const text = [
            `**Task:** ${s.task_summary}`,
            `**Status:** ${s.status}`,
            s.context ? `**Context:** ${s.context}` : null,
            `_Last saved: ${data.updated_at}_`,
          ].filter(Boolean).join("\n");

          return {
            content: [{ type: "text" as const, text }],
            details: { found: true, state: s, updated_at: data.updated_at },
          };
        },
      }),
      { name: "session_state_get" },
    );

    // ========================================================================
    // Lifecycle: Auto-recall — inject relevant memories before agent starts
    // ========================================================================

    if (cfg.autoRecall) {
      api.on("before_agent_start", async (event) => {
        if (!event.prompt || event.prompt.length < 10) return;

        try {
          // Search semantically for relevant memories
          const data = await apiCall(API, "/memory/search/semantic", {
            method: "POST",
            body: { query: event.prompt, limit: cfg.maxRecallResults },
          }) as { results?: Array<{ content: string; category: string; similarity: number }> };

          const results = (data.results || []).filter(r => r.similarity > 0.3);
          if (results.length === 0) return;

          const memoryContext = results
            .map(r => `- [${r.category}] ${r.content}`)
            .join("\n");

          api.logger.info(`tars-memory: injecting ${results.length} memories into context`);

          return {
            prependContext:
              `<relevant-memories>\n` +
              `The following memories from your persistent database may be relevant:\n` +
              `${memoryContext}\n` +
              `</relevant-memories>`,
          };
        } catch (err) {
          api.logger.warn(`tars-memory: auto-recall failed: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // Lifecycle: Auto session state — save state when agent ends
    // ========================================================================

    if (cfg.autoSessionState) {
      api.on("agent_end", async (event) => {
        if (!event.success || !event.messages || event.messages.length < 2) return;

        try {
          // Extract last user message as a rough task summary
          const messages = event.messages as Array<Record<string, unknown>>;
          let lastUserMsg = "";
          for (let i = messages.length - 1; i >= 0; i--) {
            if (messages[i]?.role === "user") {
              const content = messages[i]?.content;
              if (typeof content === "string") {
                lastUserMsg = content.slice(0, 200);
              } else if (Array.isArray(content)) {
                for (const block of content) {
                  if (block && typeof block === "object" && (block as Record<string, unknown>).type === "text") {
                    lastUserMsg = ((block as Record<string, unknown>).text as string || "").slice(0, 200);
                    break;
                  }
                }
              }
              if (lastUserMsg) break;
            }
          }

          if (!lastUserMsg || lastUserMsg.length < 10) return;

          // Save a basic session state from the last interaction
          await apiCall(API, "/memory/session-state", {
            method: "POST",
            body: {
              agent: "main",
              task_summary: `Last interaction: ${lastUserMsg}`,
              status: "idle",
              context: `Session ended after ${messages.length} messages`,
            },
          });

          api.logger.info("tars-memory: auto-saved session state on agent end");
        } catch (err) {
          api.logger.warn(`tars-memory: auto-session-state failed: ${String(err)}`);
        }
      });
    }
  },
};

export default tarsMemoryPlugin;
