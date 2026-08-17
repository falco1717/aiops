/**
 * The operator's half of the agent's browser.
 *
 * The agent could always see what it photographed — it reads the file by path.
 * This is the same picture, in the transcript, next to the tool call that took
 * it, for the person the turn is being run for.
 *
 * Three decisions worth stating:
 *
 * **Thumbnails, not the capture.** A page is 1280px wide and a transcript is
 * read on a phone. The strip scrolls inside itself and the bubble does not
 * grow, so a row of screenshots can never push the page sideways.
 *
 * **A lightbox, not a new tab.** The bytes are served with a download
 * disposition, exactly as an uploaded file is, because they are content
 * somebody else chose arriving on the same origin as the session cookie —
 * so "open it bigger" cannot be a link, or it would save the file instead of
 * showing it. The overlay is portalled to `<body>`: it is rendered from inside
 * a message bubble, and `.chat-body` scrolls, which would otherwise clip it.
 *
 * **A missing one is expected, not an error.** Captures are kept with the
 * session and are there when the conversation is reopened — but not always:
 * a turn that ran before AIOps kept them has none, and one too large for the
 * session's storage budget was declined at the time. Neither is a fault, so the
 * tile says so in words instead of showing a broken image. The wording is
 * deliberately about the picture and not about a cause: the client cannot tell
 * those two apart, and guessing wrong in either direction reads as a bug.
 */
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { shotUrl, type Shot } from "../screenshots";

export function ScreenshotStrip({ runId, shots }: { runId: number; shots: Shot[] }) {
  const [open, setOpen] = useState<Shot | null>(null);
  if (shots.length === 0) return null;
  return (
    <>
      <div className="shot-strip">
        {shots.map((shot) => (
          <ShotTile key={shot.name} runId={runId} shot={shot} onOpen={() => setOpen(shot)} />
        ))}
      </div>
      {open && <ShotLightbox runId={runId} shot={open} onClose={() => setOpen(null)} />}
    </>
  );
}

function ShotTile({
  runId,
  shot,
  onOpen,
}: {
  runId: number;
  shot: Shot;
  onOpen: () => void;
}) {
  const [gone, setGone] = useState(false);

  if (gone) {
    return (
      <span className="shot-gone" title={`${shot.url} — this capture was not kept`}>
        {shot.name} · not available
      </span>
    );
  }
  return (
    <button
      type="button"
      className="shot-tile"
      onClick={onOpen}
      title={`${shot.name} — ${shot.url}`}
    >
      <img
        src={shotUrl(runId, shot.name)}
        alt={`Screenshot of ${shot.url}`}
        loading="lazy"
        onError={() => setGone(true)}
      />
    </button>
  );
}

function ShotLightbox({
  runId,
  shot,
  onClose,
}: {
  runId: number;
  shot: Shot;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div
      className="shot-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={`Screenshot of ${shot.url}`}
      onClick={onClose}
    >
      {/* Clicking the picture itself is not "I am done looking at it". */}
      <img
        src={shotUrl(runId, shot.name)}
        alt={`Screenshot of ${shot.url}`}
        onClick={(e) => e.stopPropagation()}
      />
      <div className="shot-caption" onClick={(e) => e.stopPropagation()}>
        <span className="shot-caption-url">{shot.url}</span>
        <span className="shot-caption-note">password fields masked</span>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
    </div>,
    document.body,
  );
}
