/**
 * Finding the screenshots an agent took, in the tool result that reports them.
 *
 * There is no separate list to fetch. Screenshots are kept with the session now
 * — the same lifetime as a message attachment — so one could be listed, but a
 * list is the wrong shape: it says which captures exist and nothing about
 * *where* in the conversation each one belongs. The tool result already carries
 * both facts, and it carries them at the point in the transcript the agent took
 * the picture at, which is where it wants drawing.
 *
 * The sentence matched below is written by AIOps, not by the model: it is the
 * return value of `Browser.screenshot` in `backend/app/bridge/mcp_browser.py`,
 * and the filename in it is generated there (`screenshot-NNN.png`) rather than
 * chosen by anything on the agent's side. Keep the two in step.
 *
 * It is also written *only when a copy was kept*. A capture the app declined —
 * over the session's storage budget, say — comes back phrased differently on
 * purpose, so the words that draw a thumbnail are never emitted for a picture
 * that is not there to draw.
 *
 * Nothing here is a security boundary. The name is sent back to the API, which
 * matches it against the same generated shape and resolves it inside that one
 * run's directory before opening anything — so the worst a malformed or forged
 * result can do is ask for a picture the same run already took.
 */

/** One photograph, and the page it was of. */
export type Shot = {
  /** `screenshot-001.png` — generated, so it is also the id. */
  name: string;
  /** The page's URL, for the caption and the alt text. */
  url: string;
};

/**
 * Deliberately anchored on the whole sentence rather than on the filename.
 * "Password fields are masked" is the property that makes a screenshot safe to
 * put in front of a person, and it is emitted by the same code that applies the
 * mask — so matching the claim and the file together means a thumbnail is only
 * ever drawn for a capture that went through it.
 *
 * The URL is read lazily up to the closing paren of the sentence, not to the
 * first one: an address with brackets in it is a real thing and must not cut
 * the match short.
 */
const SAVED = /Saved (?:\S+[/\\])?(screenshot-\d{3}\.png) \(([^\n]*?)\)\. Password fields are masked\./g;

/** Where the API serves one of a run's screenshots from. */
export function shotUrl(runId: number, name: string): string {
  return `/api/runs/${runId}/screenshots/${encodeURIComponent(name)}`;
}

/**
 * The screenshots reported by one transcript event, in the order they appear.
 *
 * Only a `tool_result` is looked at. The agent's own prose is not a place to go
 * hunting for filenames — a model that writes the sentence out in its reply
 * should not thereby put pictures in the transcript.
 */
export function shotsIn(kind: string, text: string | null | undefined): Shot[] {
  if (kind !== "tool_result" || !text) return [];
  const found: Shot[] = [];
  const seen = new Set<string>();
  // `matchAll` rather than a loop on a shared lastIndex: the regex is a module
  // constant and /g state on it would leak between calls.
  for (const m of text.matchAll(SAVED)) {
    const name = m[1]!;
    if (seen.has(name)) continue;
    seen.add(name);
    found.push({ name, url: (m[2] ?? "").trim() });
  }
  return found;
}
