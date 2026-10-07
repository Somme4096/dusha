import { Plugin } from "@opencode/plugin";
import type { Registration } from "@opencode/plugin/promise/registration";
import { registerSophiaCommand } from "./commands";
import { registerContextHook, registerPromptHook, registerReplyArchive } from "./hooks";
import { registerSophiaTools } from "./tools";

async function registerSafely(
  registrations: Registration[],
  setup: () => Promise<Registration | undefined>,
): Promise<void> {
  try {
    const registration = await setup();
    if (registration) registrations.push(registration);
  } catch (error) {
    console.warn("[sophia] registration failed", error);
  }
}

export default Plugin.define({
  id: "sophia",
  setup: async (ctx) => {
    const registrations: Registration[] = [];
    await registerSafely(registrations, () => registerSophiaCommand(ctx));
    await registerSafely(registrations, () => registerSophiaTools(ctx));
    await registerSafely(registrations, () => registerContextHook(ctx));
    await registerSafely(registrations, () => registerPromptHook(ctx));
    await registerSafely(registrations, () => registerReplyArchive(ctx));
    return async () => {
      for (const registration of registrations) {
        try {
          await registration.dispose();
        } catch {
          // Disposal failures must not block plugin unload.
        }
      }
    };
  },
});
