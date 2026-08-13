/**
 * Timestamp parsing that works against both backends.
 *
 * Postgres columns are timezone-aware and serialise as `...+00:00`; SQLite (used
 * in development) hands back a naive string with no zone at all. Appending "Z"
 * unconditionally produces `Invalid Date` on Postgres — which silently breaks
 * comparisons, so a rate-limited account renders as available.
 */
export function parseUtc(iso: string): Date {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

export function formatUtc(iso: string | null | undefined, fallback = "—"): string {
  if (!iso) return fallback;
  const date = parseUtc(iso);
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
}

/** True when the timestamp is in the future (false for null or unparseable). */
export function isFuture(iso: string | null | undefined): boolean {
  if (!iso) return false;
  const date = parseUtc(iso);
  return !Number.isNaN(date.getTime()) && date > new Date();
}
