/**
 * How close an account's sign-in is to lapsing, and how loudly to say so.
 *
 * A Claude OAuth credential lives eight hours and is renewed by the CLI itself;
 * AIOps nudges it beforehand (see backend credentials.py) but cannot promise a
 * renewal it does not perform. So this is written to be honest about three
 * different situations rather than showing one countdown:
 *
 *  - fine: renewal is expected and has been happening;
 *  - soon: inside the window where AIOps is actively trying;
 *  - lapsed / failing: it did not work, and somebody has to sign in again.
 *
 * Timestamps come from Postgres as `...+00:00` and from SQLite with no zone at
 * all, which is why every one of them goes through `parseUtc` — appending "Z"
 * to the first form produces `Invalid Date`, and an Invalid Date compared
 * against anything is false, so a lapsed credential would quietly render as
 * healthy. That exact bug is why time.ts exists.
 */
import type { Account } from "./types";
import { parseUtc } from "./time";

export type CredentialLevel = "unknown" | "ok" | "soon" | "expired" | "error";

export type CredentialStatus = {
  level: CredentialLevel;
  /** Milliseconds until expiry; null when there is no readable expiry. */
  msRemaining: number | null;
  /** Short phrase for the pill, e.g. "expires in 4h". */
  label: string | null;
  /** A sentence for the banner, or null when nothing needs saying. */
  detail: string | null;
};

/** Inside this much of expiry the backend is actively trying to renew. */
export const WARN_MS = 60 * 60 * 1000;

export function formatDuration(ms: number): string {
  const seconds = Math.floor(Math.abs(ms) / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rest = minutes % 60;
    return rest ? `${hours}h ${rest}m` : `${hours}h`;
  }
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

export function credentialStatus(account: Account, now: Date = new Date()): CredentialStatus {
  const nothing: CredentialStatus = {
    level: "unknown",
    msRemaining: null,
    label: null,
    detail: null,
  };

  if (account.credential_refresh_error) {
    return {
      level: "error",
      msRemaining: expiryMs(account, now),
      label: "renewal failing",
      detail: `AIOps could not renew this sign-in: ${account.credential_refresh_error}`,
    };
  }

  if (!account.credential_expires_at) return nothing;
  const expires = parseUtc(account.credential_expires_at);
  if (Number.isNaN(expires.getTime())) return nothing;

  const ms = expires.getTime() - now.getTime();
  if (ms <= 0) {
    return {
      level: "expired",
      msRemaining: ms,
      label: "credential expired",
      detail: account.credential_watch_enabled
        ? "The access token has lapsed. AIOps renews it from the stored refresh token — " +
          "the next check, or the next turn, should clear this. If it stays, sign in again."
        : "The access token has lapsed and automatic renewal is switched off. Sign in again.",
    };
  }
  if (ms <= WARN_MS) {
    return {
      level: "soon",
      msRemaining: ms,
      label: `expires in ${formatDuration(ms)}`,
      detail: account.credential_watch_enabled
        ? `This sign-in expires in ${formatDuration(ms)}. AIOps is renewing it in the ` +
          "background, so no action should be needed."
        : `This sign-in expires in ${formatDuration(ms)} and automatic renewal is switched off.`,
    };
  }
  return {
    level: "ok",
    msRemaining: ms,
    label: `expires in ${formatDuration(ms)}`,
    detail: null,
  };
}

function expiryMs(account: Account, now: Date): number | null {
  if (!account.credential_expires_at) return null;
  const expires = parseUtc(account.credential_expires_at);
  return Number.isNaN(expires.getTime()) ? null : expires.getTime() - now.getTime();
}
