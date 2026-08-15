/**
 * The composer's key rules, exercised directly.
 *
 * `composerKeyAction` is a pure function of a keystroke plus three booleans, so
 * the entire matrix — every modifier against every list state against both
 * layouts — fits in a table and is enumerated rather than sampled. That is the
 * point of having pulled the decision out of the component: the interesting
 * cases here are the ones where two rules meet (Enter with a list up, Ctrl with
 * a list up, Shift on a phone), and none of them need a DOM to check.
 *
 * The rule that matters most is negative: with suggestions on screen, a bare
 * Enter must never send. Getting that wrong fires a half-typed message at an
 * agent because somebody was picking a skill out of a menu.
 */
import { describe, expect, it } from "vitest";
import type { ComposerAction, ComposerState, Keystroke } from "./composerKeys";
import { composerKeyAction } from "./composerKeys";

/** The three composer states worth distinguishing, at either layout. */
const CLOSED: Omit<ComposerState, "enterSends"> = { menuOpen: false, suggestionCount: 0 };
const OPEN: Omit<ComposerState, "enterSends"> = { menuOpen: true, suggestionCount: 3 };
/** Up, but showing only the "nothing matches" hint. */
const HINT: Omit<ComposerState, "enterSends"> = { menuOpen: true, suggestionCount: 0 };

const desktop = (s: Omit<ComposerState, "enterSends">): ComposerState => ({
  ...s,
  enterSends: true,
});
const mobile = (s: Omit<ComposerState, "enterSends">): ComposerState => ({
  ...s,
  enterSends: false,
});

const ENTER: Keystroke = { key: "Enter" };
const SHIFT_ENTER: Keystroke = { key: "Enter", shiftKey: true };
const CTRL_ENTER: Keystroke = { key: "Enter", ctrlKey: true };
const CMD_ENTER: Keystroke = { key: "Enter", metaKey: true };

describe("the reported request: Enter sends on desktop", () => {
  it("sends on a bare Enter with no list up", () => {
    expect(composerKeyAction(ENTER, desktop(CLOSED))).toBe("send");
  });

  it("makes a newline on Shift+Enter instead of sending", () => {
    expect(composerKeyAction(SHIFT_ENTER, desktop(CLOSED))).toBe("pass");
  });

  it("keeps Ctrl+Enter as send, which is the key that was there first", () => {
    expect(composerKeyAction(CTRL_ENTER, desktop(CLOSED))).toBe("send");
  });

  it("treats Cmd+Enter the same, for a Mac", () => {
    expect(composerKeyAction(CMD_ENTER, desktop(CLOSED))).toBe("send");
  });
});

describe("a phone is left exactly as it was", () => {
  it("a bare Enter still makes a newline — there is no easy Shift on a touch keyboard", () => {
    expect(composerKeyAction(ENTER, mobile(CLOSED))).toBe("pass");
  });

  it("Shift+Enter is a newline there too", () => {
    expect(composerKeyAction(SHIFT_ENTER, mobile(CLOSED))).toBe("pass");
  });

  it("Ctrl+Enter remains the way to send", () => {
    expect(composerKeyAction(CTRL_ENTER, mobile(CLOSED))).toBe("send");
  });

  it("Cmd+Enter too", () => {
    expect(composerKeyAction(CMD_ENTER, mobile(CLOSED))).toBe("send");
  });
});

describe("with the suggestion list up, Enter picks and never sends", () => {
  it("accepts the highlighted suggestion on desktop", () => {
    expect(composerKeyAction(ENTER, desktop(OPEN))).toBe("accept");
  });

  it("accepts it on a phone as well", () => {
    expect(composerKeyAction(ENTER, mobile(OPEN))).toBe("accept");
  });

  it("Tab accepts too, rather than moving focus out of the composer", () => {
    expect(composerKeyAction({ key: "Tab" }, desktop(OPEN))).toBe("accept");
    expect(composerKeyAction({ key: "Tab" }, mobile(OPEN))).toBe("accept");
  });

  it("Shift+Enter is still a newline and does not accept", () => {
    expect(composerKeyAction(SHIFT_ENTER, desktop(OPEN))).toBe("pass");
    expect(composerKeyAction(SHIFT_ENTER, mobile(OPEN))).toBe("pass");
  });

  it("Ctrl+Enter sends straight past an open list, at either layout", () => {
    expect(composerKeyAction(CTRL_ENTER, desktop(OPEN))).toBe("send");
    expect(composerKeyAction(CTRL_ENTER, mobile(OPEN))).toBe("send");
    expect(composerKeyAction(CMD_ENTER, desktop(OPEN))).toBe("send");
    expect(composerKeyAction(CMD_ENTER, mobile(OPEN))).toBe("send");
  });

  it("arrows move the highlight", () => {
    expect(composerKeyAction({ key: "ArrowDown" }, desktop(OPEN))).toBe("next");
    expect(composerKeyAction({ key: "ArrowUp" }, desktop(OPEN))).toBe("prev");
  });

  it("leaves ordinary typing alone", () => {
    for (const key of ["a", " ", "Backspace", "ArrowLeft", "ArrowRight", "Home"]) {
      expect(composerKeyAction({ key }, desktop(OPEN))).toBe("pass");
    }
  });
});

describe("a list showing only the hint counts as closed", () => {
  it("Enter sends on desktop rather than being swallowed by an empty panel", () => {
    expect(composerKeyAction(ENTER, desktop(HINT))).toBe("send");
  });

  it("Enter still makes a newline on a phone", () => {
    expect(composerKeyAction(ENTER, mobile(HINT))).toBe("pass");
  });

  it("Tab and the arrows fall through, because there is nothing to move over", () => {
    for (const key of ["Tab", "ArrowDown", "ArrowUp"]) {
      expect(composerKeyAction({ key }, desktop(HINT))).toBe("pass");
    }
  });
});

describe("Escape", () => {
  it("is taken whenever the list is up, so the ⋯ menu does not close on the same press", () => {
    expect(composerKeyAction({ key: "Escape" }, desktop(OPEN))).toBe("dismiss");
    expect(composerKeyAction({ key: "Escape" }, mobile(OPEN))).toBe("dismiss");
  });

  it("is taken even when the list is only showing the hint", () => {
    expect(composerKeyAction({ key: "Escape" }, desktop(HINT))).toBe("dismiss");
  });

  it("is left alone with no list up, so it can reach the menu", () => {
    expect(composerKeyAction({ key: "Escape" }, desktop(CLOSED))).toBe("pass");
    expect(composerKeyAction({ key: "Escape" }, mobile(CLOSED))).toBe("pass");
  });
});

describe("the whole Enter matrix, enumerated", () => {
  const states = [
    ["closed", CLOSED],
    ["open", OPEN],
    ["hint only", HINT],
  ] as const;
  const strokes = [
    ["Enter", ENTER],
    ["Shift+Enter", SHIFT_ENTER],
    ["Ctrl+Enter", CTRL_ENTER],
    ["Cmd+Enter", CMD_ENTER],
  ] as const;

  // desktop, mobile — for each of the three list states, for each of the four
  // Enter strokes. Written out rather than generated, so a change to any single
  // cell has to be made deliberately.
  const expected: Record<string, ComposerAction> = {
    "desktop/closed/Enter": "send",
    "desktop/closed/Shift+Enter": "pass",
    "desktop/closed/Ctrl+Enter": "send",
    "desktop/closed/Cmd+Enter": "send",
    "desktop/open/Enter": "accept",
    "desktop/open/Shift+Enter": "pass",
    "desktop/open/Ctrl+Enter": "send",
    "desktop/open/Cmd+Enter": "send",
    "desktop/hint only/Enter": "send",
    "desktop/hint only/Shift+Enter": "pass",
    "desktop/hint only/Ctrl+Enter": "send",
    "desktop/hint only/Cmd+Enter": "send",
    "mobile/closed/Enter": "pass",
    "mobile/closed/Shift+Enter": "pass",
    "mobile/closed/Ctrl+Enter": "send",
    "mobile/closed/Cmd+Enter": "send",
    "mobile/open/Enter": "accept",
    "mobile/open/Shift+Enter": "pass",
    "mobile/open/Ctrl+Enter": "send",
    "mobile/open/Cmd+Enter": "send",
    "mobile/hint only/Enter": "pass",
    "mobile/hint only/Shift+Enter": "pass",
    "mobile/hint only/Ctrl+Enter": "send",
    "mobile/hint only/Cmd+Enter": "send",
  };

  for (const [layout, wrap] of [
    ["desktop", desktop],
    ["mobile", mobile],
  ] as const) {
    for (const [stateName, state] of states) {
      for (const [strokeName, stroke] of strokes) {
        const cell = `${layout}/${stateName}/${strokeName}`;
        it(cell, () => {
          expect(composerKeyAction(stroke, wrap(state))).toBe(expected[cell]);
        });
      }
    }
  }

  it("covers every cell of the matrix", () => {
    expect(Object.keys(expected)).toHaveLength(2 * states.length * strokes.length);
  });
});
