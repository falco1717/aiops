/**
 * The full-screen gate's decision, exercised directly.
 *
 * The case that matters most is the order dependency: `needsSetup` must win
 * over everything else, including a `user` object left over from a previous
 * check — an instance can only ever need setup while it has zero users, so a
 * non-null `user` at the same time is not a state the real app can reach, but
 * the function must still resolve it the same safe way rather than trusting
 * the caller never to pass it.
 */
import { describe, expect, it } from "vitest";
import type { Gate, GateInputs } from "./gate";
import { gateFor } from "./gate";

const USER = {
  id: 1,
  username: "walt",
  display_name: null,
  is_admin: true,
  must_change_password: false,
  created_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
};

const base: GateInputs = { ready: true, needsSetup: false, user: null };

function expectGate(input: Partial<GateInputs>, expected: Gate) {
  expect(gateFor({ ...base, ...input })).toBe(expected);
}

describe("gateFor", () => {
  it("shows loading until the checks resolve, regardless of anything else", () => {
    expectGate({ ready: false }, "loading");
    expectGate({ ready: false, needsSetup: true }, "loading");
    expectGate({ ready: false, user: USER }, "loading");
  });

  it("shows setup when this instance has never had a user", () => {
    expectGate({ needsSetup: true }, "setup");
  });

  it("setup outranks a signed-in user — not a real state, but the safe resolution", () => {
    expectGate({ needsSetup: true, user: USER }, "setup");
  });

  it("shows login once ready, with no setup needed and nobody signed in", () => {
    expectGate({}, "login");
  });

  it("shows the forced password-change screen for a signed-in user who must change it", () => {
    expectGate({ user: { ...USER, must_change_password: true } }, "forced-password");
  });

  it("shows the app for an ordinary signed-in user", () => {
    expectGate({ user: USER }, "app");
  });
});
