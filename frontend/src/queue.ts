/**
 * What the composer is allowed to do, and what it has to admit, while the agent
 * is mid-turn.
 *
 * A session used to refuse a second message with a 409 for the whole length of
 * a turn. It queues them now — but a queue is all it is, and that distinction is
 * the entire reason this file exists rather than the rules living inline in
 * Chat.tsx. Both agent CLIs are driven headless, one process per turn, stdin at
 * /dev/null: there is no channel to hand a running turn a new instruction on.
 * So a message sent mid-turn becomes the *next* turn. It does not interrupt the
 * running one, does not redirect it, and is not seen by it.
 *
 * That is an easy thing for an interface to fudge — an enabled box beside a
 * working agent reads as "it is listening" — so the wording is decided here,
 * next to the state that decides it, and tested.
 */
import type { Run } from "./types";

/** Statuses that mean this turn has not finished with the session yet. */
const OUTSTANDING = new Set(["queued", "running"]);

export type QueueView = {
  /**
   * The turn holding the session. Null when nothing is outstanding.
   *
   * Not always `status === "running"`: a turn that has been picked up but has
   * not spawned its process yet is still 'queued' in the database for a moment,
   * and while it holds the session it is no more withdrawable than a running
   * one. Whichever turn the server would have started is the one named here.
   */
  active: Run | null;
  /** Turns accepted but not started, in the order they will run. */
  waiting: Run[];
  /** True while anything at all is outstanding. */
  busy: boolean;
};

/**
 * Split this session's runs into the one in flight and the ones behind it.
 *
 * Ordered by id, which is what the server orders the queue by — the
 * autoincrement key is the only field that is monotonic under two people
 * sending at the same instant, so `created_at` would be a second, disagreeing
 * definition of "first".
 */
export function queueView(runs: Run[]): QueueView {
  const outstanding = runs
    .filter((r) => OUTSTANDING.has(r.status))
    .sort((a, b) => a.id - b.id);
  // A running turn is the active one even if something older is somehow still
  // sitting at 'queued'; otherwise it is simply the oldest outstanding turn,
  // which is the one the server's dispatch would have taken.
  const active = outstanding.find((r) => r.status === "running") ?? outstanding[0] ?? null;
  return {
    active,
    waiting: outstanding.filter((r) => r.id !== active?.id),
    busy: outstanding.length > 0,
  };
}

/** Whether this turn can still be taken back — only ever one that has not started. */
export function canWithdraw(run: Run, view: QueueView): boolean {
  return view.waiting.some((w) => w.id === run.id);
}

export type ComposerState = {
  placeholder: string;
  /** The submit button's label. "Queue" while busy, because that is what it does. */
  sendLabel: string;
  /**
   * The standing sentence above the composer. Null when the session is idle —
   * there is nothing to disclaim, and a permanent notice is a notice nobody
   * reads by the time it matters.
   */
  notice: string | null;
};

/** How many, in words, without the "1 messages" that gives a bot away. */
function plural(n: number, one: string, many: string): string {
  return n === 1 ? `1 ${one}` : `${n} ${many}`;
}

/**
 * The composer's copy for this moment.
 *
 * The notice never says "the agent will see this" and never implies steering.
 * It says the thing people actually get wrong: that a message sent now lands
 * *after* the work in progress, not inside it.
 */
export function composerState(
  view: QueueView,
  opts: { enterSends: boolean; sending: boolean },
): ComposerState {
  const keys = opts.enterSends
    ? "Enter to send, Shift+Enter for a new line"
    : "Ctrl+Enter to send";
  const hints = `/ for skills, @ for systems, paste or drop files`;

  if (!view.busy) {
    return {
      placeholder: `Describe the task…  (${keys}, ${hints})`,
      sendLabel: opts.sending ? "Sending…" : "Send",
      notice: null,
    };
  }

  const waiting = view.waiting.length;
  const queueSoFar =
    waiting === 0
      ? ""
      : ` ${plural(waiting, "message is", "messages are")} already waiting; ` +
        `yours goes after ${waiting === 1 ? "it" : "them"}.`;

  return {
    placeholder: `Write the next message…  (${keys}) — it is queued until this turn ends`,
    sendLabel: opts.sending ? "Queueing…" : "Queue",
    notice:
      "The agent is working. Anything you send now is queued and runs as its own " +
      "next turn — it does not reach the turn in progress, interrupt it, or change " +
      "what it is doing." +
      queueSoFar,
  };
}
