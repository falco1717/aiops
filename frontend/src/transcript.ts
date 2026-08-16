/**
 * How a run's raw event stream becomes the rows a transcript draws, and what a
 * turn still in flight is doing right now.
 *
 * This is a module rather than three `useMemo`s inside Chat.tsx for the same
 * reason `queue.ts` is: the interesting decisions here are not about rendering.
 * They are about which events say something a person has not already read, and
 * they were all decided by reading what the CLI actually emitted into the
 * production `events` table rather than by guessing.
 *
 * Three of those decisions, and the evidence:
 *
 *  1. The CLI ends a turn with a `result` event whose `result` field is a
 *     verbatim copy of the final assistant message. Across every recorded run
 *     that had both, the two texts were byte-identical — never a summary, never
 *     an addition. So the second copy is dropped and the assistant message is
 *     the one kept: it arrives first, so nothing on screen has to move.
 *
 *  2. When the CLI has no final text to report — an interrupted turn, a turn
 *     that hit a limit — it sends no `result` field at all, and the provider
 *     falls back to the event's `subtype`. That produces a "message" whose
 *     entire body is the token `success` or `error_during_execution`. Those are
 *     not prose and must not be drawn as if they were; but an error one is the
 *     CLI's own account of how the turn ended, so it is kept as a status line
 *     rather than thrown away. `success` says nothing the run's own status does
 *     not, and goes.
 *
 *  3. Nine out of ten `system` events are a single machine word — 2189
 *     `thinking_tokens` and 201 `status` in the recorded set — emitted as a
 *     heartbeat while the model works. Drawn as transcript lines they buried
 *     one recorded turn's actual content under forty consecutive lines reading
 *     "thinking_tokens". They are progress, not content, so they leave the
 *     transcript and feed the progress display instead.
 *
 * Anything not covered by a rule above is still drawn. The failure mode to
 * avoid is not noise — it is a turn that went wrong and says nothing about it.
 */
import type { AgentEvent } from "./types";

export type TranscriptEvent = Pick<
  AgentEvent,
  "run_id" | "seq" | "kind" | "text" | "tool_name" | "parent_tool_use_id" | "agent_name"
> & {
  id?: number;
  /** Live only: websocket frames carry it, the persisted row does not. */
  is_error?: boolean;
};

/**
 * `system` subtypes whose text is the subtype itself and which recur many times
 * a turn. Each one means "still alive", which is worth exactly one indicator on
 * screen and never a line of its own.
 */
const HEARTBEATS = new Set(["status", "thinking_tokens", "background_tasks_changed"]);

/**
 * The CLI's own `result` subtypes, which is what the text is when the CLI sent
 * no result text. Deliberately an exact list rather than "one lowercase word":
 * an agent whose entire final answer is "done" must not be mistaken for one.
 */
const CLI_SUBTYPE = /^(success|error_[a-z_]+)$/;

/** A group of events produced under one tool call — a subagent, or a task. */
export type SubagentRow = {
  type: "subagent";
  parentId: string;
  /** The subagent's name when the CLI named one; "" when it did not. */
  name: string;
  /** The most recent thing this one said it was doing. */
  activity: string;
  /** Its steps, with the CLI's duplicate narration folded away. */
  steps: TranscriptEvent[];
  tools: number;
  /** True while nothing in the main loop has yet shown this one finished. */
  running: boolean;
};

export type Row =
  | { type: "event"; event: TranscriptEvent }
  | { type: "outcome"; key: string; text: string; error: boolean }
  | SubagentRow;

const key = (e: TranscriptEvent) => `${e.run_id}:${e.seq}`;

/** The first line of a value, shortened — enough to recognise, not to read. */
export function firstLine(text: string, limit = 120): string {
  const line = text.split("\n", 1)[0]!.trim();
  return line.length > limit ? `${line.slice(0, limit - 1)}…` : line;
}

/** What one event says it is doing, for a header rather than for a body. */
function describe(e: TranscriptEvent): string {
  const text = (e.text ?? "").trim();
  if (e.kind === "tool_use") {
    const name = e.tool_name ?? "tool";
    return text ? `${name} · ${firstLine(text)}` : name;
  }
  return text ? firstLine(text) : "";
}

/**
 * Whether this `result` event has anything left to say.
 *
 * `previous` is the last main-loop assistant text since the previous result —
 * the message the CLI copies into `result`.
 */
export function resultRole(
  text: string,
  previous: string | null,
): "echo" | "outcome" | "message" | "empty" {
  const trimmed = text.trim();
  if (!trimmed) return "empty";
  if (previous !== null && trimmed === previous) return "echo";
  // Checked after the echo test on purpose: a one-word reply that the CLI then
  // repeated is an echo, and must be recognised as one before it is measured
  // against the subtype list.
  if (CLI_SUBTYPE.test(trimmed)) return trimmed === "success" ? "empty" : "outcome";
  return "message";
}

function step(row: SubagentRow, e: TranscriptEvent): void {
  if (!row.name && e.agent_name) row.name = e.agent_name;
  const previous = row.steps[row.steps.length - 1];
  // The CLI narrates one tool call twice — `task_started` then
  // `task_notification`, same words — so the second is not a step.
  const repeat =
    previous !== undefined &&
    previous.kind === "system" &&
    e.kind === "system" &&
    (previous.text ?? "").trim() === (e.text ?? "").trim();
  if (!repeat) row.steps.push(e);
  if (e.kind === "tool_use") row.tools += 1;
  const said = describe(e);
  if (said) row.activity = said;
}

/**
 * Mark the groups that have not been shown to have finished.
 *
 * There is no "subagent done" event to read, so this is positional: a group is
 * still going while nothing after it proves the main loop resumed. Another tool
 * call does not prove it — the CLI issues several at once and their narration
 * interleaves, which is exactly the case where two must both read as live — but
 * a result, a reply or a thought does.
 */
function markRunning(rows: Row[]): void {
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i]!;
    if (row.type === "subagent") {
      row.running = true;
      continue;
    }
    if (row.type === "event" && row.event.kind === "tool_use") continue;
    return;
  }
}

/**
 * The rows to draw for one run's events.
 *
 * `live` is whether that run is still going; it is the only thing that can make
 * a group render as in-flight, so a finished transcript never claims work is
 * happening.
 */
export function buildRows(
  events: TranscriptEvent[],
  opts: { live?: boolean } = {},
): Row[] {
  const rows: Row[] = [];
  let previousAssistant: string | null = null;

  for (const e of events) {
    if (e.parent_tool_use_id) {
      const last = rows[rows.length - 1];
      if (last?.type === "subagent" && last.parentId === e.parent_tool_use_id) {
        step(last, e);
      } else {
        const row: SubagentRow = {
          type: "subagent",
          parentId: e.parent_tool_use_id,
          name: e.agent_name ?? "",
          activity: "",
          steps: [],
          tools: 0,
          running: false,
        };
        step(row, e);
        rows.push(row);
      }
      continue;
    }

    const text = (e.text ?? "").trim();

    if (e.kind === "system" && HEARTBEATS.has(text)) continue;

    if (e.kind === "result") {
      const role = resultRole(text, previousAssistant);
      previousAssistant = null;
      if (role === "outcome") {
        rows.push({
          type: "outcome",
          key: key(e),
          text: text.replace(/_/g, " "),
          error: text !== "success",
        });
      } else if (role === "message") {
        rows.push({ type: "event", event: e });
      }
      continue;
    }

    if (e.kind === "assistant") previousAssistant = text || null;
    rows.push({ type: "event", event: e });
  }

  if (opts.live === true) markRunning(rows);
  return rows;
}

/** What a turn in flight is doing, for the strip that replaces the spinner. */
export type Progress = {
  /** A few words: "thinking", "Bash", "writing the reply". */
  doing: string;
  /** The command, path or description behind it — or null when there is none. */
  detail: string | null;
  /** Tool calls issued so far this turn, the main loop's and its tasks'. */
  tools: number;
  /** Everything the agent has done this turn, heartbeats excluded. */
  steps: number;
};

/**
 * Read a run's progress off the events it has produced so far.
 *
 * `streaming` is whether partial reply text is arriving, which is the one state
 * the event list cannot show: the deltas are not persisted as events.
 */
export function turnProgress(
  events: TranscriptEvent[],
  opts: { streaming?: boolean } = {},
): Progress {
  let tools = 0;
  let steps = 0;
  let last: TranscriptEvent | null = null;

  for (const e of events) {
    const text = (e.text ?? "").trim();
    if (!e.parent_tool_use_id && e.kind === "system" && HEARTBEATS.has(text)) continue;
    steps += 1;
    if (e.kind === "tool_use") tools += 1;
    last = e;
  }

  if (opts.streaming === true) {
    return { doing: "writing the reply", detail: null, tools, steps };
  }
  if (last === null) {
    return { doing: "starting up", detail: null, tools, steps };
  }
  if (last.kind === "tool_use") {
    const text = (last.text ?? "").trim();
    return {
      doing: last.tool_name ?? "a tool call",
      detail: text ? firstLine(text) : null,
      tools,
      steps,
    };
  }
  if (last.parent_tool_use_id) {
    const said = describe(last);
    return { doing: said || "a background task", detail: null, tools, steps };
  }
  if (last.kind === "assistant") return { doing: "writing the reply", detail: null, tools, steps };
  // Everything else — a tool result just in, a thought, a heartbeat — is the
  // model working out what to do next, which is one state, not three.
  return { doing: "thinking", detail: null, tools, steps };
}

/**
 * How long a turn has been going, as a person would say it.
 *
 * Seconds up to a minute because that is the range where the number is the
 * reassurance; minutes after that, because a turn that has run for eleven
 * minutes is not made clearer by knowing it is also nineteen seconds.
 */
export function elapsed(fromMs: number, nowMs: number): string {
  const total = Math.max(0, Math.floor((nowMs - fromMs) / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}m ${total % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}
