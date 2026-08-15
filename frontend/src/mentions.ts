/**
 * Which token the caret is sitting in, and what should be offered for it.
 *
 * Kept out of the component so the rule that decides whether a menu opens is
 * one readable function rather than a condition buried in an onChange handler.
 * The rule is the whole feature: open too eagerly and every URL in a prompt
 * pops a menu over the composer.
 */
import type { Capability, Target } from "./types";

/** `/` offers skills and slash commands; `@` offers stored systems. */
export type Trigger = "/" | "@";

export type TokenMatch = {
  trigger: Trigger;
  /** What has been typed after the trigger, which may be empty. */
  query: string;
  /** Index of the trigger character itself. */
  start: number;
  /**
   * End of the whole word, which may be past the caret.
   *
   * The query is what is typed *before* the caret, but the replacement covers
   * the word it sits in: putting the caret back into the middle of `/deploy`
   * and accepting `/deploy-check` has to leave `/deploy-check`, not
   * `/deploy-checkloy`.
   */
  end: number;
};

const TRIGGERS: Trigger[] = ["/", "@"];

/**
 * The token the caret is in, or null.
 *
 * A trigger only counts at the *start of a word* — position zero, or straight
 * after whitespace. That single condition is what keeps `https://example.com`
 * and `user@host` and `and/or` from opening a menu, and it is why the check is
 * "what is before the trigger" rather than "what is after it".
 *
 * Whitespace ends a token, so `/deploy now` stops offering as soon as the space
 * is typed: the run scanned backwards from the caret stops at the first space,
 * and its first character is then `n`, not `/`.
 *
 * The caret must also be *past* the trigger. Sitting exactly on it — press Home
 * after typing `/rel`, or click to its left — is a caret that is not in the
 * token at all, and treating it as one opened the full unfiltered menu and
 * offered a replacement that would have inserted in front of the word instead
 * of replacing it. Measured in a browser, not reasoned about.
 */
export function activeToken(text: string, caret: number): TokenMatch | null {
  if (caret < 0 || caret > text.length) return null;
  let start = caret;
  while (start > 0 && !/\s/.test(text[start - 1])) start -= 1;
  const char = text[start];
  if (char === undefined) return null;
  if (!TRIGGERS.includes(char as Trigger)) return null;
  // Nothing before it, or whitespace before it. `start` is already the first
  // character of the word, so this is true by construction — asserted anyway
  // because it is the rule the rest of the file depends on.
  if (start > 0 && !/\s/.test(text[start - 1])) return null;
  // The caret is before or on the trigger, so there is no token under it.
  if (caret <= start) return null;
  const query = text.slice(start + 1, caret);
  // Unreachable given the scan above, which cannot cross whitespace. Kept as a
  // guard because everything downstream assumes a token is a single word.
  if (/\s/.test(query)) return null;
  // Forward to the end of the word: the query stops at the caret, but the text
  // being replaced is the whole word the caret is inside.
  let end = caret;
  while (end < text.length && !/\s/.test(text[end])) end += 1;
  return { trigger: char as Trigger, query, start, end };
}

/** One row of the suggestion list. */
export type Suggestion = {
  /** Stable within a list; used for the option element's DOM id. */
  key: string;
  /** The primary label, shown in monospace. */
  label: string;
  /** The badge beside it — a capability kind, or the system's username@host. */
  badge?: string;
  /** Secondary line: description, or hostname and login. */
  detail?: string;
  /** Where it came from, when that disambiguates. */
  source?: string;
  /** The exact text that replaces the token. */
  insert: string;
};

/**
 * Rank: things that *start* with what was typed first, then things that merely
 * contain it, each alphabetically. Typing `de` should offer `deploy` above
 * `code-review`, which a plain substring filter does not do.
 */
function rank(haystack: string, needle: string): number {
  if (!needle) return 1;
  const i = haystack.indexOf(needle);
  if (i < 0) return -1;
  return i === 0 ? 0 : 1;
}

function best(fields: string[], needle: string): number {
  const scores = fields.map((f) => rank(f.toLowerCase(), needle)).filter((s) => s >= 0);
  return scores.length ? Math.min(...scores) : -1;
}

export function capabilitySuggestions(caps: Capability[], query: string): Suggestion[] {
  const needle = query.toLowerCase();
  return caps
    .map((c) => ({ c, score: best([c.name, c.description ?? ""], needle) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => a.score - b.score || a.c.name.localeCompare(b.c.name))
    .map(({ c }) => ({
      key: `cap:${c.kind}:${c.name}`,
      label: `/${c.name}`,
      badge: c.kind,
      detail: c.description || undefined,
      source: c.source,
      // The trailing space is part of the insertion: a slash command is
      // followed by its argument, and stopping the menu re-opening on the very
      // token just accepted is worth a character.
      insert: `/${c.name} `,
    }));
}

/**
 * Stored systems, offered by slug.
 *
 * The slug is inserted rather than the friendly name because the slug is what
 * `ssh <slug>` takes — the agent is given a real SSH config with one Host entry
 * per slug, so anything else is a name the run cannot resolve. The hostname and
 * login ride along as secondary text, which is the part that lets somebody pick
 * the right one out of three boxes all called something-prod.
 */
export function targetSuggestions(targets: Target[], query: string): Suggestion[] {
  const needle = query.toLowerCase();
  return targets
    .map((t) => ({
      t,
      score: best([t.slug, t.name, t.hostname, t.username], needle),
    }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => a.score - b.score || a.t.slug.localeCompare(b.t.slug))
    .map(({ t }) => ({
      key: `target:${t.id}`,
      label: t.slug,
      badge: t.name === t.slug ? undefined : t.name,
      detail: `${t.username}@${t.hostname}${t.port === 22 ? "" : `:${t.port}`}`,
      insert: `${t.slug} `,
    }));
}

/** Everything a suggestion list needs to know about, resolved from a token. */
export function suggestionsFor(
  token: TokenMatch,
  caps: Capability[],
  targets: Target[],
): Suggestion[] {
  return token.trigger === "/"
    ? capabilitySuggestions(caps, token.query)
    : targetSuggestions(targets, token.query);
}

/** What to say when a trigger is live but nothing matches it. */
export function emptyHint(token: TokenMatch, caps: Capability[], targets: Target[]): string {
  if (token.trigger === "/") {
    if (caps.length === 0) {
      return "No skills or slash commands in this session. Skills live in .claude/skills/ in the workspace, or in the container's ~/.claude/.";
    }
    return `No skill or command matches “${token.query}”. ${caps.length} available — clear the word to see them all.`;
  }
  if (targets.length === 0) {
    return "You have no stored systems yet. Add one on the Systems screen, or ask whoever owns it to share it with you.";
  }
  return `No system matches “${token.query}”. ${targets.length} reachable — clear the word to see them all.`;
}
