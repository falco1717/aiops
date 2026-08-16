import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { WorkView } from "../work";

/**
 * Where work being done is announced, from anywhere in the app.
 *
 * The transcript already narrated a live turn — but only from inside that
 * session, scrolled to the block of the run that happened to be going. That
 * answers "what is this doing" and never "is anything running", which is the
 * question asked from the other nine screens and from the session list. So this
 * is a button that is always present, says how much is in flight, and opens a
 * short list of it.
 *
 * It is deliberately not a dashboard. Every row is one turn: which conversation
 * it is in, what it is on right now, how long it has been there, the background
 * tasks still open under it, and a way into it. Anything more belongs in the
 * session, which is one tap away.
 *
 * What it shows is scoped by the server to sessions the reader can see, and
 * administrators get nothing extra — see `sessions_visible_to`. There is no
 * "everyone's work" view here and there must not be one.
 */
export default function Working({
  view,
  place,
}: {
  view: WorkView;
  /** Which copy this is; only one is ever displayed. See the stylesheet. */
  place: "topbar" | "sidebar";
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // A panel floating over the app has to close the way every other one does: a
  // press outside it, or Escape. Bound only while it is open, so an app with
  // nothing running installs no document handlers at all.
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", key);
    };
  }, [open]);

  return (
    <div className={`working-bar working-${place}`} ref={rootRef}>
      <button
        type="button"
        className={`work-button${view.busy ? " busy" : ""}${open ? " open" : ""}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        // The count is the label and the sentence is the name, so a screen
        // reader is told what "2" is a count of.
        aria-label={view.description}
        title={view.description}
        onClick={() => setOpen((v) => !v)}
      >
        {view.busy && <span className="live-dot" aria-hidden="true" />}
        <span className="work-label">{view.label}</span>
      </button>

      {open && (
        <div className="work-panel" role="dialog" aria-label="Work in progress">
          <div className="work-head">
            <strong>{view.busy ? view.description : "Nothing is running"}</strong>
            <button
              type="button"
              className="work-close"
              onClick={() => setOpen(false)}
              aria-label="Close"
            >
              ✕
            </button>
          </div>

          {!view.busy && (
            <p className="work-empty">
              This lists every turn in flight in a session you can see. It is empty
              because none of them are working right now.
            </p>
          )}

          {view.runs.map((run) => (
            <Link
              key={run.runId}
              to={`/sessions/${run.sessionId}`}
              className={`work-row${run.running ? " running" : ""}`}
              onClick={() => setOpen(false)}
            >
              <span className="work-row-head">
                {run.running && <span className="live-dot" aria-hidden="true" />}
                <span className="work-title">{run.title}</span>
                {run.age && <span className="work-age">{run.age}</span>}
              </span>

              <span className="work-doing">
                {run.doing}
                {run.detail && <span className="work-detail">{run.detail}</span>}
              </span>

              {/* The background tasks under this turn, which is the other half
                  of "what is it doing": a turn that looks stuck on one tool
                  call often has three of these open underneath it. */}
              {run.tasks.map((task, i) => (
                <span className="work-task" key={`${task.name}:${i}`}>
                  <span className="work-task-name">{task.name}</span>
                  {task.activity && <span className="work-task-doing">{task.activity}</span>}
                </span>
              ))}

              <span className="work-meta">
                {/* Whose turn it is, when it is not the reader's. In a shared
                    session this is the difference between work you forgot
                    about and work somebody else started. */}
                {run.who && <span className="work-who">sent by {run.who}</span>}
                {run.running
                  ? `${run.tools} tool call${run.tools === 1 ? "" : "s"}`
                  : "queued — it runs when the turn above it ends"}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
