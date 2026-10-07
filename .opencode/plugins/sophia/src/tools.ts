import type { Plugin } from "@opencode/plugin";
import type { Info, Result } from "@opencode/plugin/promise/tool";
import type { Registration } from "@opencode/plugin/promise/registration";
import { SophiaClient, errorMessage } from "./client";
import { loadConfig, saveConfig } from "./config";
import { FALLBACK_TOOL_PREFIX, type SophiaConfig } from "./types";

type Args = Record<string, unknown>;

function asArgs(input: unknown): Args {
  if (input && typeof input === "object" && !Array.isArray(input)) return input as Args;
  return {};
}

function requireString(args: Args, key: string): string {
  const value = args[key];
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(`Missing required string parameter "${key}".`);
  }
  return value;
}

function requireNumber(args: Args, key: string): number {
  const value = optionalNumber(args, key);
  if (value === undefined) {
    throw new Error(`Missing required number parameter "${key}".`);
  }
  return value;
}

function optionalString(args: Args, key: string): string | undefined {
  const value = args[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

const CLEAR_WORDS = new Set(["clear", "none", "null"]);

function optionalTime(args: Args, key: string): string | null | undefined {
  const value = args[key];
  if (value === null) return null;
  if (typeof value !== "string") return undefined;
  const normalized = value.trim();
  if (CLEAR_WORDS.has(normalized.toLowerCase())) return null;
  return normalized.length > 0 ? normalized : undefined;
}

function optionalNumber(args: Args, key: string): number | undefined {
  const value = args[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function optionalBoolean(args: Args, key: string): boolean | undefined {
  const value = args[key];
  if (typeof value === "boolean") return value;
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

function objectSchema(properties: Record<string, unknown>, required: string[] = []): unknown {
  return { type: "object", properties, required, additionalProperties: false };
}

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

async function runTool<T>(operation: () => Promise<T>): Promise<Result> {
  try {
    const data = await operation();
    return { content: stringify(data) };
  } catch (error) {
    return { content: `Error: ${errorMessage(error)}` };
  }
}

async function clientFrom(ctx: Plugin.Context): Promise<{ config: SophiaConfig; client: SophiaClient }> {
  const config = await loadConfig(ctx);
  return { config, client: SophiaClient.fromConfig(config) };
}

interface ToolIdentity {
  prefix: string;
  label: string;
}

export function toolIdentity(companion: string): ToolIdentity {
  const name = companion.trim();
  const prefix = name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return { prefix: prefix || FALLBACK_TOOL_PREFIX, label: name || "companion" };
}

let current: ToolIdentity = toolIdentity("");

// Fills the companion name from the service when the stored config has none.
export async function adoptCompanion(ctx: Plugin.Context, config: SophiaConfig, force = false): Promise<boolean> {
  if (config.companion && !force) return false;
  try {
    const reported = (await SophiaClient.fromConfig(config).health()).companion;
    if (typeof reported !== "string" || !reported || reported === config.companion) return false;
    config.companion = reported;
    await saveConfig(ctx, config);
    return true;
  } catch {
    return false;
  }
}

export async function refreshTools(ctx: Plugin.Context): Promise<ToolIdentity> {
  const next = toolIdentity((await loadConfig(ctx)).companion);
  const changed = next.prefix !== current.prefix || next.label !== current.label;
  current = next;
  if (changed) await ctx.tool.reload();
  return current;
}

function buildTools(ctx: Plugin.Context, identity: ToolIdentity): Info[] {
  return [
    {
      name: `${identity.prefix}_memory_search`,
      description: `Search the ${identity.label} archive for relevant past exchanges.`,
      input: objectSchema(
        {
          query: { type: "string", description: "Search text." },
          limit: { type: "number", description: "Maximum number of results." },
          context_messages: { type: "number", description: "Neighbor messages to include with each hit." },
        },
        ["query"],
      ) as unknown as Info["input"],
      options: { codemode: false },
      execute: async (input) =>
        runTool(async () => {
          const args = asArgs(input);
          const query = requireString(args, "query");
          const { client } = await clientFrom(ctx);
          return client.searchMemory({
            query,
            limit: optionalNumber(args, "limit"),
            context_messages: optionalNumber(args, "context_messages"),
          });
        }),
    },
    {
      name: `${identity.prefix}_context_build`,
      description: `Build the companion state injection ${identity.label} would add to a prompt.`,
      input: objectSchema(
        {
          query: { type: "string", description: "Current user message." },
          conversation_id: { type: "string", description: "Conversation identifier." },
          include_recent: { type: "boolean", description: "Include recent messages." },
        },
        ["query"],
      ) as unknown as Info["input"],
      options: { codemode: false },
      execute: async (input) =>
        runTool(async () => {
          const args = asArgs(input);
          const query = requireString(args, "query");
          const { config, client } = await clientFrom(ctx);
          return client.buildContext({
            query,
            harness: config.harness,
            conversation_id: optionalString(args, "conversation_id"),
            include_recent: optionalBoolean(args, "include_recent") ?? true,
          });
        }),
    },
    {
      name: `${identity.prefix}_affect_status`,
      description: "Read the current affect state for the companion.",
      input: objectSchema({}) as unknown as Info["input"],
      options: { codemode: false },
      execute: async () =>
        runTool(async () => {
          const { client } = await clientFrom(ctx);
          return client.getAffect();
        }),
    },
    {
      name: `${identity.prefix}_evergreen_remember`,
      description: "Store an evergreen fact the companion should always remember.",
      input: objectSchema(
        {
          key: { type: "string", description: "Stable fact key." },
          text: { type: "string", description: "Fact text." },
          priority: { type: "number", description: "Priority from 0 to 100." },
          reason: { type: "string", description: "Why the fact is being stored." },
          review_after: { type: "string", description: "ISO timestamp to review after." },
          expires_at: { type: "string", description: "ISO timestamp to expire at." },
        },
        ["key", "text"],
      ) as unknown as Info["input"],
      options: { codemode: false },
      execute: async (input) =>
        runTool(async () => {
          const args = asArgs(input);
          const key = requireString(args, "key");
          const text = requireString(args, "text");
          const { client } = await clientFrom(ctx);
          return client.rememberFact({
            key,
            text,
            priority: optionalNumber(args, "priority"),
            reason: optionalString(args, "reason"),
            review_after: optionalTime(args, "review_after"),
            expires_at: optionalTime(args, "expires_at"),
          });
        }),
    },
    {
      name: `${identity.prefix}_evergreen_list`,
      description: "List the evergreen facts the companion currently holds.",
      input: objectSchema({
        include_inactive: { type: "boolean", description: "Include inactive facts." },
        due_only: { type: "boolean", description: "Only facts due for review." },
        limit: { type: "number", description: "Maximum number of facts." },
      }) as unknown as Info["input"],
      options: { codemode: false },
      execute: async (input) =>
        runTool(async () => {
          const args = asArgs(input);
          const { client } = await clientFrom(ctx);
          return client.listFacts({
            include_inactive: optionalBoolean(args, "include_inactive"),
            due_only: optionalBoolean(args, "due_only"),
            limit: optionalNumber(args, "limit"),
          });
        }),
    },
    {
      name: `${identity.prefix}_evergreen_revise`,
      description: "Revise an evergreen fact at the given revision.",
      input: objectSchema(
        {
          fact_id: { type: "string", description: "Fact identifier." },
          expected_revision: { type: "number", description: "Revision the caller expects." },
          text: { type: "string", description: "Complete replacement fact text." },
          priority: { type: "number", description: "Priority from 0 to 100." },
          reason: { type: "string", description: "Why the fact is changing." },
          review_after: { type: "string", description: "ISO timestamp to review after, or clear to remove it." },
          expires_at: { type: "string", description: "ISO timestamp to expire at, or clear to remove it." },
        },
        ["fact_id", "expected_revision", "text"],
      ) as unknown as Info["input"],
      options: { codemode: false },
      execute: async (input) =>
        runTool(async () => {
          const args = asArgs(input);
          const factId = requireString(args, "fact_id");
          const expectedRevision = requireNumber(args, "expected_revision");
          const text = requireString(args, "text");
          const { client } = await clientFrom(ctx);
          return client.reviseFact(factId, {
            expected_revision: expectedRevision,
            text,
            priority: optionalNumber(args, "priority"),
            reason: optionalString(args, "reason"),
            review_after: optionalTime(args, "review_after"),
            expires_at: optionalTime(args, "expires_at"),
          });
        }),
    },
    {
      name: `${identity.prefix}_evergreen_forget`,
      description: "Forget an evergreen fact at the given revision.",
      input: objectSchema(
        {
          fact_id: { type: "string", description: "Fact identifier." },
          expected_revision: { type: "number", description: "Revision the caller expects." },
          reason: { type: "string", description: "Why the fact is being forgotten." },
        },
        ["fact_id", "expected_revision", "reason"],
      ) as unknown as Info["input"],
      options: { codemode: false },
      execute: async (input) =>
        runTool(async () => {
          const args = asArgs(input);
          const factId = requireString(args, "fact_id");
          const expectedRevision = requireNumber(args, "expected_revision");
          const reason = requireString(args, "reason");
          const { client } = await clientFrom(ctx);
          return client.forgetFact(factId, {
            expected_revision: expectedRevision,
            reason,
          });
        }),
    },
  ];
}

export async function registerSophiaTools(ctx: Plugin.Context): Promise<Registration | undefined> {
  const config = await loadConfig(ctx);
  await adoptCompanion(ctx, config);
  current = toolIdentity(config.companion);
  return ctx.tool.transform((editor) => {
    if (typeof (editor as { namespace?: unknown }).namespace === "function") {
      editor.namespace({ name: current.prefix, description: `Dusha tools for ${current.label}.` });
    }
    if (typeof (editor as { add?: unknown }).add !== "function") return;
    for (const tool of buildTools(ctx, current)) editor.add(tool);
  });
}
