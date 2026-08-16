/**
 * Credential-expiry rendering.
 *
 * The reason this is a module and not a `useMemo` inside Accounts.tsx is the
 * bug that produced time.ts: a Postgres timestamp arrives as `...+00:00`, an
 * unconditional `new Date(iso + "Z")` returns Invalid Date, and every
 * comparison against Invalid Date is false — so a rate-limited account
 * rendered as available. The same shape of mistake here would render a lapsed
 * sign-in as healthy, which is worse: it is the thing the feature exists to
 * warn about. Both serialisations are therefore asserted directly.
 */
import { describe, expect, it } from "vitest";
import type { Account } from "./types";
import { credentialStatus, formatDuration, WARN_MS } from "./credentials";

const NOW = new Date("2026-08-16T00:00:00Z");

/** An account, with only the fields the expiry rules read. */
function account(over: Partial<Account> = {}): Account {
  return {
    id: 1,
    name: "Default Claude",
    provider: "claude",
    slug: "default-claude",
    description: null,
    is_default: true,
    fallback_account_id: null,
    limited_until: null,
    limit_status: null,
    limit_window: null,
    limit_resets_at: null,
    config_dir: "/home/node/.claude",
    signed_in: true,
    account_detail: null,
    allowed_user_ids: [],
    usable_by_me: true,
    credential_expires_at: null,
    credential_checked_at: null,
    credential_refreshed_at: null,
    credential_refresh_error: null,
    credential_watch_enabled: true,
    ...over,
  };
}

describe("credentialStatus", () => {
  it("says nothing when there is no readable expiry", () => {
    const status = credentialStatus(account(), NOW);
    expect(status.level).toBe("unknown");
    expect(status.label).toBeNull();
    expect(status.detail).toBeNull();
  });

  it("reads a Postgres timestamp with an offset", () => {
    // The exact serialisation that broke the rate-limit pill. Appending "Z"
    // to this produces Invalid Date.
    const status = credentialStatus(
      account({ credential_expires_at: "2026-08-16T08:00:00+00:00" }),
      NOW,
    );
    expect(status.level).toBe("ok");
    expect(status.msRemaining).toBe(8 * 3600 * 1000);
    expect(status.label).toBe("expires in 8h");
  });

  it("reads a SQLite timestamp with no zone as UTC", () => {
    const status = credentialStatus(
      account({ credential_expires_at: "2026-08-16T08:00:00" }),
      NOW,
    );
    expect(status.level).toBe("ok");
    expect(status.msRemaining).toBe(8 * 3600 * 1000);
  });

  it("treats a Z-suffixed timestamp the same way", () => {
    const status = credentialStatus(
      account({ credential_expires_at: "2026-08-16T08:00:00Z" }),
      NOW,
    );
    expect(status.msRemaining).toBe(8 * 3600 * 1000);
  });

  it("ignores an unparseable timestamp rather than inventing a countdown", () => {
    const status = credentialStatus(account({ credential_expires_at: "not a date" }), NOW);
    expect(status.level).toBe("unknown");
    expect(status.msRemaining).toBeNull();
  });

  it("warns inside the last hour", () => {
    const status = credentialStatus(
      account({ credential_expires_at: "2026-08-16T00:45:00Z" }),
      NOW,
    );
    expect(status.level).toBe("soon");
    expect(status.label).toBe("expires in 45m");
    expect(status.detail).toContain("renewing it in the background");
  });

  it("puts the warning boundary exactly at the warn window", () => {
    const edge = new Date(NOW.getTime() + WARN_MS).toISOString();
    expect(credentialStatus(account({ credential_expires_at: edge }), NOW).level).toBe("soon");
    const justOutside = new Date(NOW.getTime() + WARN_MS + 1000).toISOString();
    expect(credentialStatus(account({ credential_expires_at: justOutside }), NOW).level).toBe(
      "ok",
    );
  });

  it("reports a lapsed credential, not a negative countdown", () => {
    const status = credentialStatus(
      account({ credential_expires_at: "2026-08-15T23:00:00Z" }),
      NOW,
    );
    expect(status.level).toBe("expired");
    expect(status.label).toBe("credential expired");
    expect(status.detail).toContain("renews it from the stored refresh token");
  });

  it("says the renewal is off when the watch is disabled", () => {
    const status = credentialStatus(
      account({
        credential_expires_at: "2026-08-15T23:00:00Z",
        credential_watch_enabled: false,
      }),
      NOW,
    );
    expect(status.level).toBe("expired");
    expect(status.detail).toContain("switched off");
  });

  it("surfaces a refresh failure ahead of the countdown", () => {
    const status = credentialStatus(
      account({
        credential_expires_at: "2026-08-16T08:00:00+00:00",
        credential_refresh_error: "probe turn timed out",
      }),
      NOW,
    );
    expect(status.level).toBe("error");
    expect(status.detail).toContain("probe turn timed out");
    // Still knows the timing, so the banner can be specific.
    expect(status.msRemaining).toBe(8 * 3600 * 1000);
  });

  it("survives a refresh failure with no expiry to report", () => {
    const status = credentialStatus(
      account({ credential_refresh_error: "claude is not installed" }),
      NOW,
    );
    expect(status.level).toBe("error");
    expect(status.msRemaining).toBeNull();
  });
});

describe("formatDuration", () => {
  it("uses the largest unit that stays readable", () => {
    expect(formatDuration(45 * 1000)).toBe("45s");
    expect(formatDuration(90 * 1000)).toBe("1m");
    expect(formatDuration(59 * 60 * 1000)).toBe("59m");
    expect(formatDuration(60 * 60 * 1000)).toBe("1h");
    expect(formatDuration(90 * 60 * 1000)).toBe("1h 30m");
    expect(formatDuration(8 * 3600 * 1000)).toBe("8h");
    expect(formatDuration(30 * 3600 * 1000)).toBe("1d 6h");
  });

  it("describes a magnitude, not a direction", () => {
    expect(formatDuration(-45 * 60 * 1000)).toBe("45m");
  });
});
