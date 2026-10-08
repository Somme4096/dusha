import type { Plugin } from "@opencode/plugin";
import { CONFIG_KEY, DEFAULT_CONFIG, type DushaConfig } from "./types";

type JsonValue = Parameters<Plugin.Context["storage"]["set"]>[1];

export async function loadConfig(ctx: Plugin.Context): Promise<DushaConfig> {
  const stored = (await ctx.storage.get(CONFIG_KEY)) as Partial<DushaConfig> | undefined;
  return { ...DEFAULT_CONFIG, ...(stored ?? {}) };
}

export async function saveConfig(ctx: Plugin.Context, config: DushaConfig): Promise<void> {
  await ctx.storage.set(CONFIG_KEY, config as unknown as JsonValue);
}
