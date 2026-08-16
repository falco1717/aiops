/**
 * What the working indicator says, and why it says it.
 *
 * `WorkingStrip` already answered "what is this turn doing" — but only inside
 * the transcript of the session you were already looking at, scrolled to the
 * block of the run that happened to be live. That is the wrong shape for the
 * question people actually ask, which is "is anything running", asked from
 * wherever they happen to be. So the same facts are read here for every turn in
 * flight at once.
 *
 * The reading is deliberately not reimplemented. `turnProgress` and `buildRows`
 * in `transcript.ts` decide what a step means, which of them are heartbeats and
 * which background tasks are still open, and every one of those rules was
 * settled against recorded CLI output. Feeding them the tail of a run rather
 * than the whole of it is the only difference — so the indicator can never
 * disagree with the transcript about what an agent is doing, which is the one
 * way a second progress display earns its place instead of becoming a second
 * opinion.
 *
 * Nothing here fetches or renders: it is a pure function of the payload and the
 * clock, so the wording is testable without a browser.
 */
import type { ActiveRun } from "./types";
import { buildRows, elapsed, turnProgress } from "./transcript";
import { parseUtc } from "./time";

/** One background task still open under a turn. */
export type TaskLine = {
  /** What the CLI called it, or the honest fallback when it named nothing. */
  name: string;
  /** The last thing it said it was doing; "" when it has not said anything. */
  activity: string;
};

export type RunSummary = {
  runId: number;
  sessionId: string;
  title: string;
  /** True for the turn holding the session, false for one waiting behind it. */
  running: boolean;
  /** A few words: "Bash", "thinking", "waiting to start". */
  doing: string;
  /** The command or path behind it, or null. */
  detail: string | null;
  /** "2m 14s", or null when it has not started and has no clock yet. */
  age: string | null;
  tools: number;
  tasks: TaskLine[];
  /** Who sent it, or null when the reader sent it or nobody is recorded. */
  who: string | null;
  /** The opening of the message, for recognising the row. */
  prompt: string;
};

/**
 * How a queued turn describes itself.
 *
 * Not "starting up", which is what an empty event list means for a turn that
 * has begun: a queued turn has not been handed to an agent at all, and saying
 * it is starting would put the same words on two genuinely different states.
 */
const WAITING = "waiting to start";

/**
 * Read one in-flight turn.
 *
 * `nowMs` is passed in rather than read from the clock so the whole panel
 * measures every row against one instant, and so the wording can be tested.
 */
export function summarizeRun(run: ActiveRun, nowMs: number, myId: number): RunSummary {
  const running = run.status === "running";
  const progress = turnProgress(run.recent);
  // The clock starts when the agent did. A queued turn has no `started_at`, and
  // counting from `created_at` instead would present time spent waiting as time
  // spent working.
  const since = run.started_at ? parseUtc(run.started_at).getTime() : null;
  const age = since !== null && !Number.isNaN(since) ? elapsed(since, nowMs) : null;

  return {
    runId: run.run_id,
    sessionId: run.session_id,
    title: run.session_title,
    running,
    doing: running ? progress.doing : WAITING,
    detail: running ? progress.detail : null,
    age: running ? age : null,
    tools: run.tools,
    tasks: running ? openTasks(run) : [],
    // "you" is not worth a line. In a shared session somebody else's name very
    // much is: it is the difference between work you forgot about and work
    // somebody else started in a conversation you can read.
    who: run.requested_by_id === null || run.requested_by_id === myId ? null : run.requested_by,
    prompt: run.prompt,
  };
}

/**
 * The background tasks still open under this turn.
 *
 * `buildRows` decides that positionally — a group is open while nothing after
 * it shows the main loop resumed — and it is given the tail of the run, which
 * is exactly the window that question is about. A task that finished a hundred
 * steps ago is not in the tail and is not meant to be.
 *
 * Then folded by the tool call each group belongs to, which the transcript does
 * not do. A subagent's steps arrive *interleaved* with the main loop's, so one
 * task that has taken five steps in the visible tail comes back as five
 * consecutive-run groups. In a transcript that is right — each fragment sits
 * where it happened, in order. Here it is one task, and printing it five times
 * says there are five.
 */
function openTasks(run: ActiveRun): TaskLine[] {
  const byParent = new Map<string, TaskLine>();
  for (const row of buildRows(run.recent, { live: true })) {
    if (row.type !== "subagent" || !row.running) continue;
    const existing = byParent.get(row.parentId);
    byParent.set(row.parentId, {
      // The same fallback the transcript prints. The CLI names a subagent it
      // spawned and does not name the task it narrates around an ordinary tool
      // call, and calling the second one a subagent claims something untrue.
      // A later fragment may be the one that carries the name.
      name: row.name || existing?.name || "background task",
      // The most recent thing it said, which is what "doing" means.
      activity: row.activity || existing?.activity || "",
    });
  }
  return [...byParent.values()];
}

export type WorkView = {
  runs: RunSummary[];
  running: number;
  waiting: number;
  /** True while anything at all is outstanding. */
  busy: boolean;
  /** What the button says. Short: it sits in a 210px sidebar and a phone bar. */
  label: string;
  /** The full sentence, for the button's title and its accessible name. */
  description: string;
};

/** How many, in words, without the "1 turns" that gives a bot away. */
function plural(n: number, one: string, many: string): string {
  return n === 1 ? `1 ${one}` : `${n} ${many}`;
}

/**
 * The whole panel's state, including what to call the button that opens it.
 *
 * The idle case still returns a label rather than nothing. An indicator that
 * only exists while something is happening cannot be found before it is needed,
 * and "where do I see what is running" is the question this is answering — so
 * it is always there, and quiet when there is nothing to say.
 */
export function workView(runs: ActiveRun[], nowMs: number, myId: number): WorkView {
  const summaries = runs.map((r) => summarizeRun(r, nowMs, myId));
  const running = summaries.filter((r) => r.running).length;
  const waiting = summaries.length - running;

  if (summaries.length === 0) {
    return {
      runs: summaries,
      running,
      waiting,
      busy: false,
      label: "Nothing running",
      description: "No agent is working in any session you can see.",
    };
  }

  const parts: string[] = [];
  if (running > 0) parts.push(plural(running, "turn running", "turns running"));
  if (waiting > 0) parts.push(plural(waiting, "waiting", "waiting"));

  return {
    runs: summaries,
    running,
    waiting,
    busy: true,
    // The count is the label. Which sessions they are in is a tap away, and
    // spelling one of them out here would be wrong as soon as there were two.
    label: running > 0 ? plural(running, "turn running", "turns running") : `${waiting} waiting`,
    description: `${parts.join(", ")} — in sessions you can see.`,
  };
}
