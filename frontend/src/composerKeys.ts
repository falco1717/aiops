/**
 * What a key pressed in the composer means.
 *
 * Enter has four different jobs in that box depending on the modifiers, on
 * whether the autocomplete list is up, and on how wide the window is. That is
 * the sort of rule which is easy to get subtly wrong and awkward to test
 * through a real textarea, so the *decision* lives here as a pure function of a
 * description of the keystroke plus a description of the composer — the same
 * shape `applySuggestion` takes in `mentions.ts`. The component keeps the
 * `preventDefault` and the state changes around the call.
 */

/** The parts of a KeyboardEvent the decision depends on. */
export type Keystroke = {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
};

/** The parts of the composer the decision depends on. */
export type ComposerState = {
  /** The suggestion list is up. */
  menuOpen: boolean;
  /**
   * How many rows it is offering. Zero means the panel is only saying "nothing
   * matches", which must not swallow keys: there is nothing there to choose.
   */
  suggestionCount: number;
  /**
   * Whether Enter on its own sends.
   *
   * True at the desktop layout. False on a phone, where the on-screen keyboard
   * has no comfortable Shift and Enter-to-send makes a multi-line prompt
   * miserable to type — so there a bare Enter stays a newline and Ctrl+Enter
   * remains the only way to send.
   */
  enterSends: boolean;
};

export type ComposerAction =
  /** Submit the prompt. */
  | "send"
  /** Replace the live token with the highlighted suggestion. */
  | "accept"
  /** Close the suggestion list, and do not let the key travel any further. */
  | "dismiss"
  /** Move the highlight down / up the suggestion list. */
  | "next"
  | "prev"
  /** Not ours: let the textarea do whatever it normally does with the key. */
  | "pass";

/**
 * The one place the composer's key rules are written down.
 *
 * Order matters, and the order is the argument:
 *
 * 1. Ctrl/Cmd+Enter sends. Always — list open, list closed, phone or desktop.
 *    It is what this composer shipped with, it is in muscle memory, and it is
 *    named in the placeholder, so it never becomes "accept a suggestion".
 * 2. Shift+Enter is the newline. It has to work everywhere for Enter to be
 *    allowed to send anything.
 * 3. With a list of real suggestions up, a bare Enter accepts the highlighted
 *    one and does not send. Anything else would fire a message at an agent
 *    because somebody was picking a skill out of a menu.
 * 4. Otherwise a bare Enter sends at the desktop layout and makes a newline on
 *    a phone.
 *
 * A list showing only the "nothing matches" hint counts as closed for all of
 * this: `/xyzzy` matching nothing should behave exactly like ordinary typed
 * text, which on desktop means Enter sends it.
 */
export function composerKeyAction(stroke: Keystroke, state: ComposerState): ComposerAction {
  const { menuOpen, suggestionCount, enterSends } = state;
  const choosing = menuOpen && suggestionCount > 0;

  if (stroke.key === "Enter") {
    if (stroke.ctrlKey || stroke.metaKey) return "send";
    if (stroke.shiftKey) return "pass";
    if (choosing) return "accept";
    return enterSends ? "send" : "pass";
  }

  // Escape belongs to the list whenever the list is up, even when it is only
  // showing the hint — and it is taken rather than passed on, so the ⋯ menu's
  // document-level handler does not close on the same press.
  if (stroke.key === "Escape") return menuOpen ? "dismiss" : "pass";

  if (!choosing) return "pass";
  if (stroke.key === "Tab") return "accept";
  if (stroke.key === "ArrowDown") return "next";
  if (stroke.key === "ArrowUp") return "prev";
  return "pass";
}
