import type { Plugin } from "@opencode/plugin";
import type { CommandInvocation } from "@opencode/plugin/promise/command";
import type { Registration } from "@opencode/plugin/promise/registration";
import { DushaClient, errorMessage } from "./client";
import { loadConfig, saveConfig } from "./config";
import { adoptCompanion, refreshTools, toolIdentity } from "./tools";
import type { DushaConfig } from "./types";

type SessionId = Parameters<Plugin.Context["session"]["synthetic"]>[0]["sessionID"];

const HELP_TEXT = [
  "Dusha commands",
  "/dusha status - show configuration and connection status",
  "/dusha test - test the gateway connection",
  "/dusha url <url> - set the gateway base URL and adopt the companion it serves",
  "/dusha companion <name|auto> - set the companion name that prefixes the tools",
  "/dusha token <token> - set the API token, use clear to remove it",
  "/dusha auto-inject <on|off> - toggle companion state injection",
  "/dusha harness <name> - set the harness name conversations are stored under",
  "/dusha timeout <seconds> - set the gateway request timeout",
].join("\n");

interface ParsedInvocation {
  subcommand: string;
  args: string[];
}

function parseInvocation(text: string): ParsedInvocation {
  const withoutCommand = text.trim().replace(/^\/?dusha\b/i, "").trim();
  const parts = withoutCommand.length > 0 ? withoutCommand.split(/\s+/) : [];
  return { subcommand: (parts[0] ?? "").toLowerCase(), args: parts.slice(1) };
}

function normalizeUrl(value: string): string {
  const trimmed = value.trim().replace(/\/+$/, "");
  if (!trimmed) return trimmed;
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return `http://${trimmed}`;
}

async function reply(ctx: Plugin.Context, sessionID: SessionId, text: string): Promise<void> {
  try {
    await ctx.session.synthetic({ sessionID, text, description: "Dusha" });
  } catch {
    // The session can close while the command runs.
  }
}

async function testConnection(config: DushaConfig): Promise<{ connected: boolean; line: string }> {
  const client = DushaClient.fromConfig(config);
  try {
    const health = await client.health();
    return { connected: true, line: `connected (status: ${health.status}, database: ${health.database})` };
  } catch (error) {
    return { connected: false, line: errorMessage(error) };
  }
}

function renderStatus(config: DushaConfig, connectionLine: string): string {
  return [
    "Dusha",
    `URL: ${config.baseUrl}`,
    `Companion: ${config.companion || "not set"} (tools: ${toolIdentity(config.companion).prefix}_*)`,
    `Auth: ${config.apiToken ? "token set" : "no token"}`,
    `Auto-inject: ${config.autoInject ? "on" : "off"}`,
    `Harness: ${config.harness}`,
    `Timeout: ${config.timeoutSeconds}s`,
    `Status: ${connectionLine}`,
    "",
    HELP_TEXT,
  ].join("\n");
}

export async function handleDushaCommand(ctx: Plugin.Context, invocation: CommandInvocation): Promise<void> {
  const { sessionID } = invocation;
  const parsed = parseInvocation(invocation.prompt?.text ?? "");
  const config = await loadConfig(ctx);

  switch (parsed.subcommand) {
    case "":
    case "status": {
      const connection = await testConnection(config);
      if (await adoptCompanion(ctx, config)) await refreshTools(ctx);
      await reply(ctx, sessionID, renderStatus(config, connection.line));
      return;
    }
    case "test": {
      const connection = await testConnection(config);
      if (await adoptCompanion(ctx, config)) await refreshTools(ctx);
      const who = config.companion || "Dusha";
      const text = connection.connected
        ? `${who} is reachable at ${config.baseUrl}.`
        : `${who} is not reachable. ${connection.line}`;
      await reply(ctx, sessionID, text);
      return;
    }
    case "companion": {
      const value = parsed.args[0];
      if (!value) {
        const current = config.companion || "not set";
        await reply(ctx, sessionID, `Companion is currently ${current}. Use /dusha companion <name|auto>.`);
        return;
      }
      if (value.toLowerCase() === "auto") {
        await adoptCompanion(ctx, config, true);
      } else {
        config.companion = value;
        await saveConfig(ctx, config);
      }
      const identity = await refreshTools(ctx);
      await reply(ctx, sessionID, `Companion set to ${identity.label}. Tools are named ${identity.prefix}_*.`);
      return;
    }
    case "url": {
      const value = parsed.args[0];
      if (!value) {
        await reply(ctx, sessionID, "Usage: /dusha url <url>");
        return;
      }
      config.baseUrl = normalizeUrl(value);
      await saveConfig(ctx, config);
      const connection = await testConnection(config);
      await adoptCompanion(ctx, config, true);
      const identity = await refreshTools(ctx);
      const lines = [
        `URL set to ${config.baseUrl}.`,
        `Companion: ${identity.label}. Tools are named ${identity.prefix}_*.`,
        `Status: ${connection.line}`,
      ];
      await reply(ctx, sessionID, lines.join("\n"));
      return;
    }
    case "token": {
      const value = parsed.args[0];
      if (value === undefined) {
        await reply(ctx, sessionID, "Usage: /dusha token <token>, or /dusha token clear to remove it.");
        return;
      }
      config.apiToken = value === "clear" ? "" : value;
      await saveConfig(ctx, config);
      const connection = await testConnection(config);
      const action = config.apiToken ? "updated" : "cleared";
      await reply(ctx, sessionID, `API token ${action}.\nStatus: ${connection.line}`);
      return;
    }
    case "auto-inject": {
      const value = (parsed.args[0] ?? "").toLowerCase();
      if (value !== "on" && value !== "off") {
        const current = config.autoInject ? "on" : "off";
        await reply(ctx, sessionID, `Auto-inject is currently ${current}. Use /dusha auto-inject <on|off>.`);
        return;
      }
      config.autoInject = value === "on";
      await saveConfig(ctx, config);
      await reply(ctx, sessionID, `Auto-inject ${config.autoInject ? "enabled" : "disabled"}.`);
      return;
    }
    case "harness": {
      const value = parsed.args[0];
      if (!value) {
        await reply(ctx, sessionID, `Harness is currently ${config.harness}. Use /dusha harness <name>.`);
        return;
      }
      config.harness = value;
      await saveConfig(ctx, config);
      await reply(ctx, sessionID, `Harness set to ${config.harness}. New messages are stored under this harness.`);
      return;
    }
    case "timeout": {
      const value = Number(parsed.args[0]);
      if (!parsed.args[0] || !Number.isFinite(value) || value <= 0) {
        const current = `${config.timeoutSeconds}s`;
        await reply(ctx, sessionID, `Timeout is currently ${current}. Use /dusha timeout <seconds>.`);
        return;
      }
      config.timeoutSeconds = value;
      await saveConfig(ctx, config);
      await reply(ctx, sessionID, `Request timeout set to ${config.timeoutSeconds}s.`);
      return;
    }
    case "help": {
      await reply(ctx, sessionID, HELP_TEXT);
      return;
    }
    default: {
      await reply(ctx, sessionID, `Unknown option "${parsed.subcommand}".\n\n${HELP_TEXT}`);
    }
  }
}

export async function registerDushaCommand(ctx: Plugin.Context): Promise<Registration | undefined> {
  return ctx.command.transform((editor) => {
    if (typeof (editor as { add?: unknown }).add !== "function") return;
    editor.add({
      name: "dusha",
      description: "Check, test, and configure Dusha.",
      execute: async (invocation) => {
        await handleDushaCommand(ctx, invocation);
      },
    });
  });
}
