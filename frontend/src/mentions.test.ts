/**
 * The composer's autocomplete rules, exercised directly.
 *
 * `activeToken` is a pure function of `(text, caret)` and everything else here
 * is a pure function of a token plus a list, so none of this needs a DOM, a
 * component, or a rendered composer — which is the reason the logic lives in
 * `mentions.ts` in the first place.
 *
 * Two of these tests exist because the bug they describe shipped and was found
 * by hand in a browser. They are named after the symptom rather than the
 * function, so that a future reader who breaks one is told what a person saw,
 * not merely which assertion moved.
 */
import { describe, expect, it } from "vitest";
import type { Capability, Target } from "./types";
import type { TokenMatch } from "./mentions";
import {
  activeToken,
  applySuggestion,
  capabilitySuggestions,
  emptyHint,
  suggestionsFor,
  targetSuggestions,
} from "./mentions";

/** Narrow to a token, failing with a readable message rather than a type error. */
function must(token: TokenMatch | null): TokenMatch {
  if (!token) throw new Error("expected a live token, got null");
  return token;
}

function cap(name: string, description = "", kind: Capability["kind"] = "command"): Capability {
  return { name, kind, description, source: "project" };
}

function target(over: Partial<Target> & { id: number; slug: string }): Target {
  return {
    name: over.slug,
    hostname: `${over.slug}.internal`,
    port: 22,
    username: "root",
    description: null,
    auth_type: "key",
    has_private_key: true,
    has_passphrase: false,
    has_password: false,
    host_key_policy: "accept-new",
    has_known_host_key: true,
    owner_id: 1,
    grants: [],
    my_level: "owner",
    relay_node_id: null,
    created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

// A list where alphabetical order and match quality disagree, so that ranking
// is actually being tested rather than incidentally satisfied.
const CAPS: Capability[] = [
  cap("code-review", "Review the current branch"),
  cap("debug", "Work out why", "skill"),
  cap("deploy", "Ship the built image"),
];

const TARGETS: Target[] = [
  target({ id: 1, slug: "prod-db", name: "Production Database", hostname: "db.internal" }),
  target({ id: 2, slug: "web-01", name: "Web Front End", hostname: "web01.internal", username: "deploy", port: 2222 }),
];

describe("regression: caret parked on the trigger must not open the menu", () => {
  // Typing `/rel` and pressing Home leaves the caret at index 0 — to the *left*
  // of the slash, in no token at all. Matching on "the word starts with a
  // trigger" left the menu open and unfiltered, and accepting from it inserted
  // in front of the word instead of replacing it.
  it("pressing Home after typing /rel closes the menu", () => {
    expect(activeToken("/rel", 0)).toBeNull();
  });

  it("clicking to the left of a mid-sentence trigger closes the menu", () => {
    expect(activeToken("run /rel now", 4)).toBeNull();
  });

  it("still opens the moment the caret moves one character past the trigger", () => {
    const token = must(activeToken("run /rel now", 5));
    expect(token).toEqual({ trigger: "/", query: "", start: 4, end: 8 });
  });

  it("does not offer a replacement that would insert in front of the word", () => {
    // The whole harm of the bug: with a token reported at caret 0, accepting
    // `/release ` produced `/release /rel` instead of `/release `. There is no
    // token, so there is nothing to accept — which is the assertion above.
    expect(activeToken("/rel", 0)).toBeNull();
    const token = must(activeToken("/rel", 4));
    expect(applySuggestion("/rel", token, "/release ")).toEqual({ text: "/release ", caret: 9 });
  });
});

describe("regression: the replacement covers the whole word, not just up to the caret", () => {
  // Clicking back into the middle of `/deploy` and accepting `/deploy-check`
  // used to leave `/deploy-checkloy`: `end` stopped at the caret, so the tail
  // of the old word survived the replacement.
  it("reports the end of the word when the caret is inside it", () => {
    const token = must(activeToken("/deploy", 4));
    expect(token).toEqual({ trigger: "/", query: "dep", start: 0, end: 7 });
  });

  it("accepting mid-word leaves no orphaned tail", () => {
    const text = "/deploy";
    const token = must(activeToken(text, 4));
    expect(applySuggestion(text, token, "/deploy-check ")).toEqual({
      text: "/deploy-check ",
      caret: 14,
    });
  });

  it("extends to the end of the word mid-sentence without eating the rest", () => {
    const text = "run /deploy now";
    const token = must(activeToken(text, 8));
    expect(token).toEqual({ trigger: "/", query: "dep", start: 4, end: 11 });
    // The double space is not a typo: every insertion carries a trailing space
    // so the menu does not immediately reopen on the token just accepted, and
    // here it lands in front of a word that already had one.
    expect(applySuggestion(text, token, "/deploy-check ")).toEqual({
      text: "run /deploy-check  now",
      caret: 18,
    });
  });

  it("leaves end at the caret when the caret is already at the end of the word", () => {
    const token = must(activeToken("/dep", 4));
    expect(token.end).toBe(4);
  });
});

const OPENS: [label: string, text: string, caret: number, trigger: "/" | "@"][] = [
  ["/ at the very start of the input", "/x", 2, "/"],
  ["@ at the very start of the input", "@x", 2, "@"],
  ["/ after a space", "run /x", 6, "/"],
  ["@ after a space", "hello @x", 8, "@"],
  ["/ after a newline", "line one\n/x", 11, "/"],
];

const STAYS_CLOSED: [label: string, text: string, caret: number][] = [
  ["a scheme's double slash", "http://example.com", 8],
  ["the end of a bare host URL", "http://example.com", 18],
  ["an email-shaped user@host", "user@host", 9],
  ["a slash inside a word", "and/or", 6],
  ["a full URL with a path and query", "See https://example.com/path?q=1 for details", 31],
  ["a path in the middle of a sentence", "look in src/mentions.ts please", 22],
];

describe("a trigger only counts at the start of a word", () => {
  it.each(OPENS)("opens for %s", (_label, text, caret, trigger) => {
    const token = must(activeToken(text, caret));
    expect(token.trigger).toBe(trigger);
    expect(token.query).toBe("x");
  });

  it.each(STAYS_CLOSED)("stays closed for %s", (_label, text, caret) => {
    expect(activeToken(text, caret)).toBeNull();
  });

  it("closes as soon as whitespace ends the token", () => {
    // `/deploy now` — the caret is in `now`, whose first character is not a
    // trigger, so the menu is gone rather than still offering skills.
    expect(activeToken("/deploy now", 11)).toBeNull();
  });

  it("returns null for a caret outside the text", () => {
    expect(activeToken("/deploy", -1)).toBeNull();
    expect(activeToken("/deploy", 99)).toBeNull();
  });
});

describe("filtering and ranking", () => {
  it("puts prefix matches above substring matches, then sorts alphabetically", () => {
    // `code-review` contains "de" and sorts first alphabetically, so a plain
    // substring filter would lead with it. It must come last.
    expect(capabilitySuggestions(CAPS, "de").map((s) => s.label)).toEqual([
      "/debug",
      "/deploy",
      "/code-review",
    ]);
  });

  it("drops entries that match nowhere", () => {
    expect(capabilitySuggestions(CAPS, "zzz")).toEqual([]);
  });

  it("matches on the description as well as the name", () => {
    expect(capabilitySuggestions(CAPS, "branch").map((s) => s.label)).toEqual(["/code-review"]);
  });

  it("is case-insensitive", () => {
    expect(capabilitySuggestions(CAPS, "DEP").map((s) => s.label)).toEqual(["/deploy"]);
  });

  it("offers everything, alphabetically, for an empty query", () => {
    expect(capabilitySuggestions(CAPS, "").map((s) => s.label)).toEqual([
      "/code-review",
      "/debug",
      "/deploy",
    ]);
    expect(targetSuggestions(TARGETS, "").map((s) => s.label)).toEqual(["prod-db", "web-01"]);
  });

  it("carries the kind as a badge and the description as detail", () => {
    const [debug] = capabilitySuggestions(CAPS, "debug");
    expect(debug.badge).toBe("skill");
    expect(debug.detail).toBe("Work out why");
    expect(debug.insert).toBe("/debug ");
  });
});

describe("@ offers stored systems by slug", () => {
  it("inserts the slug, not the friendly name", () => {
    // The generated SSH config has one `Host` entry per slug, so the friendly
    // name is a word no run can resolve. This is the entire point of the
    // feature and the assertion most worth keeping.
    const [prod] = targetSuggestions(TARGETS, "prod");
    expect(prod.insert).toBe("prod-db ");
    expect(prod.label).toBe("prod-db");
    expect(prod.insert).not.toContain("Production Database");
  });

  it("shows the friendly name as a badge only when it differs from the slug", () => {
    const [prod] = targetSuggestions(TARGETS, "prod");
    expect(prod.badge).toBe("Production Database");
    const [plain] = targetSuggestions([target({ id: 3, slug: "sandbox" })], "sandbox");
    expect(plain.badge).toBeUndefined();
  });

  it("shows login and host, with the port only when it is not 22", () => {
    expect(targetSuggestions(TARGETS, "prod")[0].detail).toBe("root@db.internal");
    expect(targetSuggestions(TARGETS, "web")[0].detail).toBe("deploy@web01.internal:2222");
  });

  it("matches on the friendly name, the hostname and the login too", () => {
    expect(targetSuggestions(TARGETS, "database").map((s) => s.label)).toEqual(["prod-db"]);
    expect(targetSuggestions(TARGETS, "web01.internal").map((s) => s.label)).toEqual(["web-01"]);
    expect(targetSuggestions(TARGETS, "deploy").map((s) => s.label)).toEqual(["web-01"]);
  });

  it("replaces the whole word when accepted mid-word", () => {
    const text = "@prodx";
    const token = must(activeToken(text, 4));
    expect(token).toEqual({ trigger: "@", query: "pro", start: 0, end: 6 });
    const [prod] = targetSuggestions(TARGETS, token.query);
    expect(applySuggestion(text, token, prod.insert)).toEqual({ text: "prod-db ", caret: 8 });
  });
});

describe("suggestionsFor dispatches on the trigger", () => {
  it("sends / to capabilities and @ to systems", () => {
    const slash = must(activeToken("/", 1));
    const at = must(activeToken("@", 1));
    expect(suggestionsFor(slash, CAPS, TARGETS).map((s) => s.label)).toEqual([
      "/code-review",
      "/debug",
      "/deploy",
    ]);
    expect(suggestionsFor(at, CAPS, TARGETS).map((s) => s.label)).toEqual(["prod-db", "web-01"]);
  });

  it("a bare trigger is a live token with an empty query", () => {
    expect(must(activeToken("/", 1))).toEqual({ trigger: "/", query: "", start: 0, end: 1 });
  });
});

describe("the hint shown when nothing matches", () => {
  it("names the word and the number of skills available", () => {
    const token = must(activeToken("/zzz", 4));
    const hint = emptyHint(token, CAPS, TARGETS);
    expect(hint).toContain("zzz");
    expect(hint).toContain("3 available");
  });

  it("says where skills come from when the session has none at all", () => {
    const token = must(activeToken("/zzz", 4));
    expect(emptyHint(token, [], TARGETS)).toContain(".claude/skills/");
  });

  it("names the word and the number of systems reachable", () => {
    const token = must(activeToken("@zzz", 4));
    const hint = emptyHint(token, CAPS, TARGETS);
    expect(hint).toContain("zzz");
    expect(hint).toContain("2 reachable");
  });

  it("points at the Systems screen when there are no stored systems", () => {
    const token = must(activeToken("@zzz", 4));
    expect(emptyHint(token, CAPS, [])).toContain("no stored systems");
  });
});
