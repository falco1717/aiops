import { useEffect, useRef, useState } from "react";

/**
 * A copy control that sits *on* the block it copies.
 *
 * The alternative — a full-width labelled button underneath the command — put
 * the action a line away from its object, so a card with a command, a note and
 * a button read as three unrelated things and the button had to name what it
 * copied in order to make sense. In the corner of the block it needs no name at
 * all: the thing it belongs to is directly behind it.
 *
 * Deliberately not hover-to-reveal. That is the usual way this control is
 * built and it is the usual way it becomes unreachable: there is no hover on a
 * touchscreen, and a keyboard tabbing to an invisible button lands nowhere the
 * eye can follow. So it is drawn at rest, dimmed, and brightens on hover and on
 * focus.
 *
 * The confirmation is a live region rather than a swapped `aria-label`. A
 * relabelled button that the user is already focused on is not reliably
 * re-announced, which would leave a screen reader with no signal that anything
 * happened; a `role="status"` beside it is announced, and is the same text a
 * sighted user sees.
 */
export default function CopyButton({
  value,
  what,
}: {
  value: string;
  /** What is being copied, for the accessible name: "Copy the command". */
  what: string;
}) {
  const [said, setSaid] = useState<"" | "Copied" | "Clipboard blocked">("");
  const timer = useRef<number | undefined>(undefined);

  // A confirmation that outlives its block would set state on a gone component.
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setSaid("Copied");
    } catch {
      // Blocked by the browser (an insecure origin, a permissions policy). The
      // block itself is still selectable, so this is a nudge, not a failure.
      setSaid("Clipboard blocked");
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setSaid(""), 2200);
  };

  const done = said === "Copied";

  return (
    <div className="copy-slot">
      <span className={`copy-said${done ? "" : " warn"}`} role="status">
        {said}
      </span>
      <button
        type="button"
        className={`copy-btn${done ? " ok" : ""}`}
        onClick={() => void copy()}
        // Stable across states, so the control keeps one name; what changed is
        // announced by the status beside it.
        aria-label={`Copy ${what}`}
        title={`Copy ${what}`}
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          focusable="false"
        >
          {done ? (
            <path d="M3 8.4 6.4 11.8 13 4.6" />
          ) : (
            <>
              <rect x="5.6" y="5.6" width="8.8" height="8.8" rx="1.6" />
              <path d="M10.4 3.4V3A1.5 1.5 0 0 0 8.9 1.5H3A1.5 1.5 0 0 0 1.5 3v5.9A1.5 1.5 0 0 0 3 10.4h.4" />
            </>
          )}
        </svg>
      </button>
    </div>
  );
}
