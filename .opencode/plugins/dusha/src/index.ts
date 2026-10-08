import { Plugin } from "@opencode/plugin";
import type { Registration } from "@opencode/plugin/promise/registration";
import { registerDushaCommand } from "./commands";
import { registerContextHook, registerPromptHook, registerReplyArchive } from "./hooks";
import { registerDushaTools } from "./tools";

async function registerSafely(
  registrations: Registration[],
  setup: () => Promise<Registration | undefined>,
): Promise<void> {
  try {
    const registration = await setup();
    if (registration) registrations.push(registration);
  } catch (error) {
    console.warn("[dusha] registration failed", error);
  }
}

export default Plugin.define({
  id: "dusha",
  setup: async (ctx) => {
    const registrations: Registration[] = [];
    await registerSafely(registrations, () => registerDushaCommand(ctx));
    await registerSafely(registrations, () => registerDushaTools(ctx));
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
