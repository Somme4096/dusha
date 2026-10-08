import type { Plugin } from "@opencode/plugin";
import type { Registration } from "@opencode/plugin/promise/registration";
import { DushaClient } from "./client";
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

const ROUTE = "opencode";
const REPLY_EVENT = "session.text.ended";
const RESUBSCRIBE_DELAY_MS = 5_000;
const MAX_TRACKED_SESSIONS = 1_024;

// Latest user message ingest per session, the context hook excludes its id.
const latestIngest = new Map<string, Promise<number | undefined>>();

function trackIngest(sessionID: string, pending: Promise<number | undefined>): void {
  latestIngest.delete(sessionID);
  latestIngest.set(sessionID, pending);
  if (latestIngest.size <= MAX_TRACKED_SESSIONS) return;
  for (const oldest of latestIngest.keys()) {
    latestIngest.delete(oldest);
    break;
  }
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

export async function registerContextHook(ctx: Plugin.Context): Promise<Registration> {
  return ctx.session.hook("context", async (event) => {
    try {
      const config = await loadConfig(ctx);
      if (!config.autoInject) return;
      const query = latestUserText(event.messages);
      if (query.trim().length === 0) return;
      const sessionID = String(event.sessionID);
      const storedId = await latestIngest.get(sessionID);
      const client = DushaClient.fromConfig(config);
      const response = await client.buildContext({
        query,
        harness: config.harness,
        conversation_id: sessionID,
        exclude_message_ids: storedId === undefined ? undefined : [storedId],
        include_recent: true,
      });
      const injection = typeof response?.injection === "string" ? response.injection.trim() : "";
      if (injection.length > 0) event.system.push({ type: "text", text: injection });
    } catch {
      // Injection is best effort, a missing gateway must not break a request.
    }
  });
}

async function ingestUserMessage(
  ctx: Plugin.Context,
  sessionID: string,
  messageID: string,
  text: string,
): Promise<number | undefined> {
  const config = await loadConfig(ctx);
  const stored = await DushaClient.fromConfig(config).ingestMessage({
    harness: config.harness,
    conversation_id: sessionID,
    role: "user",
    content: text,
    route: ROUTE,
    external_id: externalId(sessionID, messageID, text),
  });
  return typeof stored?.id === "number" ? stored.id : undefined;
}

export async function registerPromptHook(ctx: Plugin.Context): Promise<Registration> {
  return ctx.session.hook("prompt", async (event) => {
    const sessionID = String(event.sessionID);
    const text = typeof event.prompt?.text === "string" ? event.prompt.text : "";
    if (text.trim().length === 0) {
      latestIngest.delete(sessionID);
      return;
    }
    // Archiving is best effort, a failed ingest resolves to no id.
    const pending = ingestUserMessage(ctx, sessionID, String(event.messageID ?? ""), text).catch(() => undefined);
    trackIngest(sessionID, pending);
    await pending;
  });
}

async function archiveReply(
  ctx: Plugin.Context,
  data: { sessionID: string; assistantMessageID: string; ordinal: number; text: string },
): Promise<void> {
  try {
    if (typeof data.text !== "string" || data.text.trim().length === 0) return;
    const config = await loadConfig(ctx);
    await DushaClient.fromConfig(config).ingestMessage({
      harness: config.harness,
      conversation_id: String(data.sessionID),
      role: "assistant",
      content: data.text,
      route: ROUTE,
      external_id: `opencode:${data.sessionID}:${data.assistantMessageID}:${data.ordinal}`,
    });
  } catch {
    // Archiving is best effort.
  }
}

export async function registerReplyArchive(ctx: Plugin.Context): Promise<Registration> {
  const controller = new AbortController();
  const { signal } = controller;
  const run = async () => {
    while (!signal.aborted) {
      try {
        for await (const event of ctx.event.subscribe({ signal })) {
          if (event.type === REPLY_EVENT) await archiveReply(ctx, event.data);
        }
      } catch {
        // The event stream can drop, the loop subscribes again.
      }
      await pause(RESUBSCRIBE_DELAY_MS, signal);
    }
  };
  void run();
  return { dispose: async () => controller.abort() };
}
