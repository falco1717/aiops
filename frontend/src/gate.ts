/**
 * Which full-screen gate covers the app, before any route is ever rendered.
 *
 * Four states, and the order they are checked in is the whole rule:
 *
 * 1. Still finding out — nothing is known yet, so nothing is shown but a
 *    loading message.
 * 2. `needs_setup` — this instance has never had a single user. Nobody could
 *    possibly be signed in, so this outranks everything below it, including a
 *    stale `user` from a previous check.
 * 3. Not signed in — the ordinary login screen.
 * 4. Signed in but `must_change_password` — every screen except the password
 *    form is blocked until this clears, enforced again on the server so this
 *    is convenience, not the real gate.
 *
 * Pulled out of App.tsx for the same reason `composerKeyAction` was pulled out
 * of the composer: four booleans with an order dependency between them is easy
 * to get subtly wrong — showing setup to somebody already signed in, or a
 * flash of the login screen before setup status is known — and awkward to
 * exercise through a mounted component tree.
 */
import type { User } from "./types";

export type GateInputs = {
  /** True once both `needs_setup` and `me()` have resolved (or failed). */
  ready: boolean;
  /** Null before the check resolves, or once it has and this instance has
   *  users — undefined is not a state this takes on: the caller must know
   *  before it is willing to render anything past the loading screen. */
  needsSetup: boolean;
  /** Null when signed out. */
  user: User | null;
};

export type Gate = "loading" | "setup" | "login" | "forced-password" | "app";

export function gateFor({ ready, needsSetup, user }: GateInputs): Gate {
  if (!ready) return "loading";
  if (needsSetup) return "setup";
  if (!user) return "login";
  if (user.must_change_password) return "forced-password";
  return "app";
}
