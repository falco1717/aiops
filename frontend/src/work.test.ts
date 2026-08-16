/**
 * What the working indicator claims, exercised directly.
 *
 * The reason this is a module and not logic inside the component is the reason
 * `queue.ts` is: the interesting decisions are not about rendering. They are
 * about what a turn is honestly said to be doing when all you have is the tail
 * of its event stream, what a *queued* turn may be said to be doing (nothing —
 * it has not been handed to an agent), and whose work a row belongs to in a
 * shared session.
 *
 * The other thing pinned here is that the indicator reads a turn with the same
 * rules the transcript does. If somebody re-derives "what it is doing" locally
 * instead of going through `turnProgress`, the wording drifts and these fail.
 */
import { describe, expect, it } from "vitest";
import type { ActiveRun, WorkEvent } from "./types";
import { summarizeRun, workView } from "./work";

const T0 = Date.parse("2026-01-01T00:00:00Z");

function ev(seq: number, over: Partial<WorkEvent> = {}): WorkEvent {
  return {
    run_id: 1,
    seq,
    kind: "assistant",
    text: null,
    tool_name: null,
    parent_tool_use_id: null,
    agent_name: null,
    ...over,
  };
}

function active(over: Partial<ActiveRun> = {}): ActiveRun {
  return {
    run_id: 1,
    session_id: "s1",
    session_title: "Rebuild the index",
    status: "running",
    provider: "claude",
    prompt: "rebuild the index",
    requested_by_id: 1,
    requested_by: "jordan",
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    tools: 0,
    recent: [],
    ...over,
  };
}

describe("summarizeRun", () => {
  it("names the tool a running turn is on, with its argument", () => {
    const run = active({
      tools: 3,
      recent: [ev(1, { kind: "tool_use", tool_name: "Bash", text: "npm run build\nmore" })],
    });
    const s = summarizeRun(run, T0 + 74_000, 1);
    expect(s.running).toBe(true);
    expect(s.doing).toBe("Bash");
    expect(s.detail).toBe("npm run build");
    expect(s.tools).toBe(3);
    expect(s.age).toBe("1m 14s");
  });

  it("uses the transcript's own words for a turn that is not on a tool", () => {
    // "thinking" rather than a third vocabulary for the same state: this must
    // come from turnProgress, not from a local guess.
    const s = summarizeRun(active({ recent: [ev(1, { kind: "tool_result", text: "ok" })] }), T0, 1);
    expect(s.doing).toBe("thinking");
    expect(s.detail).toBeNull();
  });

  it("says a queued turn is waiting, and gives it no clock", () => {
    // The distinction that matters: "starting up" is a turn an agent has been
    // handed and has not spoken yet. A queued turn has not been handed over at
    // all, and it must not borrow the running vocabulary.
    const s = summarizeRun(
      active({ status: "queued", started_at: null, recent: [] }),
      T0 + 600_000,
      1,
    );
    expect(s.running).toBe(false);
    expect(s.doing).toBe("waiting to start");
    expect(s.age).toBeNull();
    expect(s.tasks).toEqual([]);
  });

  it("counts the clock from the start of the turn, not from when it was sent", () => {
    const s = summarizeRun(
      active({ created_at: "2026-01-01T00:00:00Z", started_at: "2026-01-01T00:05:00Z" }),
      T0 + 320_000,
      1,
    );
    expect(s.age).toBe("20s");
  });

  it("lists the background tasks still open under the turn", () => {
    const run = active({
      recent: [
        ev(1, { kind: "tool_use", tool_name: "Task", text: "survey the tree" }),
        ev(2, { parent_tool_use_id: "t1", agent_name: "Explore", text: "reading src/" }),
      ],
    });
    expect(summarizeRun(run, T0, 1).tasks).toEqual([
      { name: "Explore", activity: "reading src/" },
    ]);
  });

  it("calls an unnamed one a background task rather than inventing a subagent", () => {
    const run = active({
      recent: [ev(1, { parent_tool_use_id: "t1", kind: "system", text: "running a check" })],
    });
    expect(summarizeRun(run, T0, 1).tasks).toEqual([
      { name: "background task", activity: "running a check" },
    ]);
  });

  it("counts one interleaved task once, not once per fragment", () => {
    // A subagent's steps arrive between the main loop's, so one task comes back
    // from buildRows as several consecutive-run groups. In a transcript each
    // fragment belongs where it happened; here five fragments are one task, and
    // listing it five times says five things are running.
    const run = active({
      recent: [
        ev(1, { kind: "tool_use", tool_name: "Bash", text: "step one" }),
        ev(2, { parent_tool_use_id: "t1", agent_name: "Explore", text: "reading a" }),
        ev(3, { kind: "tool_use", tool_name: "Bash", text: "step two" }),
        ev(4, { parent_tool_use_id: "t1", agent_name: "Explore", text: "reading b" }),
        ev(5, { kind: "tool_use", tool_name: "Bash", text: "step three" }),
        ev(6, { parent_tool_use_id: "t1", agent_name: "Explore", text: "reading c" }),
      ],
    });
    // And the line carries the latest thing it said, not the first.
    expect(summarizeRun(run, T0, 1).tasks).toEqual([
      { name: "Explore", activity: "reading c" },
    ]);
  });

  it("keeps two genuinely different tasks apart", () => {
    const run = active({
      recent: [
        ev(1, { parent_tool_use_id: "t1", agent_name: "Explore", text: "reading" }),
        ev(2, { parent_tool_use_id: "t2", agent_name: "Plan", text: "sketching" }),
      ],
    });
    expect(summarizeRun(run, T0, 1).tasks).toEqual([
      { name: "Explore", activity: "reading" },
      { name: "Plan", activity: "sketching" },
    ]);
  });

  it("drops a task the main loop has moved on from", () => {
    const run = active({
      recent: [
        ev(1, { parent_tool_use_id: "t1", agent_name: "Explore", text: "reading src/" }),
        ev(2, { kind: "assistant", text: "Here is what I found." }),
      ],
    });
    expect(summarizeRun(run, T0, 1).tasks).toEqual([]);
  });

  it("names somebody else's turn and says nothing about the reader's own", () => {
    expect(summarizeRun(active({ requested_by_id: 7, requested_by: "walt" }), T0, 1).who).toBe(
      "walt",
    );
    expect(summarizeRun(active({ requested_by_id: 1, requested_by: "jordan" }), T0, 1).who).toBeNull();
    // Turns older than the column are genuinely unattributed; guessing the
    // reader is the sender is the bug that fix exists to prevent.
    expect(summarizeRun(active({ requested_by_id: null, requested_by: null }), T0, 1).who).toBeNull();
  });
});

describe("workView", () => {
  it("still names itself when nothing is happening", () => {
    // An indicator that appears only once there is something to see cannot be
    // found before it is needed, which is the complaint it answers.
    const v = workView([], T0, 1);
    expect(v.busy).toBe(false);
    expect(v.label).toBe("Nothing running");
    expect(v.description).toContain("No agent is working");
  });

  it("counts running turns and the ones queued behind them", () => {
    const v = workView(
      [
        active({ run_id: 1 }),
        active({ run_id: 2, session_id: "s2" }),
        active({ run_id: 3, status: "queued", started_at: null }),
      ],
      T0,
      1,
    );
    expect(v.running).toBe(2);
    expect(v.waiting).toBe(1);
    expect(v.label).toBe("2 turns running");
    expect(v.description).toBe("2 turns running, 1 waiting — in sessions you can see.");
  });

  it("does not say '1 turns'", () => {
    const v = workView([active({ run_id: 1 })], T0, 1);
    expect(v.label).toBe("1 turn running");
    expect(v.description).toBe("1 turn running — in sessions you can see.");
  });

  it("reports a queue with nothing running, which is a real state after a stop", () => {
    const v = workView([active({ run_id: 3, status: "queued", started_at: null })], T0, 1);
    expect(v.busy).toBe(true);
    expect(v.label).toBe("1 waiting");
  });
});
