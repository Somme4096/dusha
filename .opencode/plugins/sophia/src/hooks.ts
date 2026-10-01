import type { Plugin } from "@opencode/plugin";
import type { Registration } from "@opencode/plugin/promise/registration";
import { SophiaClient } from "./client";
import { loadConfig } from "./config";

function contentToText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const part of content) {
    if (part && typeof part === "object" && (part as { type?: unknown }).type === "text") {
      const text = (part as { text?: unknown }).text;
      if (typeof text === "string" && text.length > 0) parts.push(text);
    }
  }
  return parts.join("\n");
}

function latestUserText(messages: readonly unknown[]): string {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (!message || typeof message !== "object") continue;
    if ((message as { role?: unknown }).role !== "user") continue;
    const text = contentToText((message as { content?: unknown }).content);
    if (text.trim().length > 0) return text;
  }
  return "";
}

function externalId(sessionID: string, messageID: string, text: string): string {
  const source = messageID ? `${sessionID}:${messageID}` : `${sessionID}:${text}`;
  let hash = 0x811c9dc5;
  for (let index = 0; index < source.length; index++) {
    hash ^= source.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `opencode:${hash.toString(16)}`;
}

export async function registerContextHook(ctx: Plugin.Context): Promise<Registration> {
  return ctx.session.hook("context", async (event) => {
    try {
      const config = await loadConfig(ctx);
      if (!config.autoInject) return;
      const query = latestUserText(event.messages);
      if (query.trim().length === 0) return;
      const client = new SophiaClient({ baseUrl: config.baseUrl, apiToken: config.apiToken });
      const response = await client.buildContext({
        query,
        harness: config.harness,
        conversation_id: String(event.sessionID),
        include_recent: true,
      });
      const injection = typeof response?.injection === "string" ? response.injection.trim() : "";
      if (injection.length > 0) event.system.push({ type: "text", text: injection });
    } catch {
      // Injection is best effort, a missing gateway must not break a request.
    }
  });
}

export async function registerPromptHook(ctx: Plugin.Context): Promise<Registration> {
  return ctx.session.hook("prompt", async (event) => {
    try {
      const text = typeof event.prompt?.text === "string" ? event.prompt.text : "";
      if (text.trim().length === 0) return;
      const config = await loadConfig(ctx);
      const client = new SophiaClient({ baseUrl: config.baseUrl, apiToken: config.apiToken });
      await client.ingestMessage({
        harness: config.harness,
        conversation_id: String(event.sessionID),
        role: "user",
        content: text,
        route: "opencode",
        external_id: externalId(String(event.sessionID), String(event.messageID ?? ""), text),
      });
    } catch {
      // Archiving is best effort.
    }
  });
}
