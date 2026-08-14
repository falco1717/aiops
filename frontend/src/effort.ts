import type { ProviderInfo } from "./types";

/**
 * Reasoning effort — how long the model thinks before it acts.
 *
 * The levels are not ours to choose: `claude --effort` and Codex's
 * `model_reasoning_effort` each accept their own list, and Codex's narrows per
 * model, so everything here reads what the server reported rather than
 * hardcoding a ladder that would silently rot on the next CLI release.
 */
export function effortChoices(
  provider: ProviderInfo | undefined,
  model: string | null,
): string[] {
  if (!provider) return [];
  const narrowed = model ? provider.efforts_by_model[model] : undefined;
  return narrowed ?? provider.efforts;
}

/** Said once, next to whichever control is offering the choice. */
export const EFFORT_HINT =
  "How long the model thinks before acting. Higher is better on hard problems " +
  "and slower on easy ones; blank leaves the CLI's own default.";
