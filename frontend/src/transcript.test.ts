/**
 * The transcript rules, against the shapes the CLI actually produced.
 *
 * Every sequence below was copied from the production `events` table rather
 * than imagined, because the whole point of these rules is that they match what
 * the agent emits. The run numbers in the test names are the real ones, so a
 * failure can be checked against the record that motivated it.
 *
 * The rule that most needs a test is not the deduplication — it is the limit on
 * it. Runs 56, 90 and 93 all ended badly, and in every one of them the `result`
 * event is the only thing the CLI said about it. A change that quietly drops
 * `result` to fix the double-rendering breaks those three, which is why they
 * are here.
 */
import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "./transcript";
import { buildRows, elapsed, resultRole, turnProgress } from "./transcript";

let seq = 0;

function ev(kind: string, text: string | null, extra: Partial<TranscriptEvent> = {}): TranscriptEvent {
  seq += 1;
  return {
    run_id: 1,
    seq,
    kind,
    text,
    tool_name: null,
    parent_tool_use_id: null,
    agent_name: null,
    ...extra,
  };
}

const bodies = (rows: ReturnType<typeof buildRows>) =>
  rows.map((r) =>
    r.type === "event" ? `${r.event.kind}:${r.event.text}` : r.type === "outcome" ? `outcome:${r.text}` : `sub:${r.parentId}`,
  );

describe("the duplicated final message", () => {
  it("drops a result that repeats the assistant text it follows (42 of 46 recorded)", () => {
    const reply = "Found it — and this one's conclusive.\n\n## Root cause\n\nBoth databases…";
    const rows = buildRows([ev("assistant", reply), ev("result", reply)]);
    expect(bodies(rows)).toEqual([`assistant:${reply}`]);
  });

  it("still drops it when the copy differs only in surrounding whitespace", () => {
    const rows = buildRows([ev("assistant", "done here"), ev("result", "done here\n")]);
    expect(rows).toHaveLength(1);
  });

  it("keeps the assistant copy, not the result copy, so nothing on screen moves", () => {
    const rows = buildRows([ev("assistant", "same words"), ev("result", "same words")]);
    expect(rows[0]).toMatchObject({ type: "event", event: { kind: "assistant" } });
  });

  it("compares against the last reply, not the last event (run 90's shape)", () => {
    const reply = "It doesn't resolve — no DNS record for that hostname.";
    const rows = buildRows([
      ev("assistant", reply),
      ev("tool_use", "python3 -c …", { tool_name: "Bash" }),
      ev("tool_result", "NO RESOLVE"),
      ev("result", reply),
    ]);
    expect(bodies(rows)).toEqual([`assistant:${reply}`, "tool_use:python3 -c …", "tool_result:NO RESOLVE"]);
  });

  it("does not let one turn's reply silence the next turn's result", () => {
    const rows = buildRows([
      ev("assistant", "first answer"),
      ev("result", "first answer"),
      ev("result", "first answer"),
    ]);
    // The second result has no reply of its own to be a copy of, so it stays.
    expect(bodies(rows)).toEqual(["assistant:first answer", "result:first answer"]);
  });
});

describe("a result that is not a message", () => {
  it("keeps an interrupted turn's outcome — the only record of it (runs 56 and 93)", () => {
    const rows = buildRows([
      ev("user", "[Request interrupted by user]"),
      ev("result", "error_during_execution"),
    ]);
    expect(bodies(rows)).toEqual(["user:[Request interrupted by user]", "outcome:error during execution"]);
    expect(rows[1]).toMatchObject({ error: true });
  });

  it("keeps it even when a reply came first, because it is not that reply (run 90)", () => {
    const rows = buildRows([
      ev("assistant", "Let me confirm that."),
      ev("result", "error_during_execution"),
    ]);
    expect(bodies(rows)).toEqual(["assistant:Let me confirm that.", "outcome:error during execution"]);
  });

  it("drops a bare `success`, which the run's own status already says (run 101 seq 3)", () => {
    expect(buildRows([ev("result", "success")])).toEqual([]);
  });

  it("drops a result with no text at all", () => {
    expect(buildRows([ev("result", "")])).toEqual([]);
  });

  it("draws a result nothing else carries — an auth failure with no reply before it", () => {
    const rows = buildRows([ev("result", "Failed to authenticate: OAuth session expired")]);
    expect(bodies(rows)).toEqual(["result:Failed to authenticate: OAuth session expired"]);
  });

  it("does not mistake a one-word reply for an outcome token", () => {
    expect(resultRole("done", null)).toBe("message");
    expect(resultRole("success", null)).toBe("empty");
    expect(resultRole("error_max_turns", null)).toBe("outcome");
  });
});

describe("the heartbeat lines", () => {
  it("drops the forty consecutive `thinking_tokens` lines run 90 recorded", () => {
    const events = [ev("assistant", "working on it")];
    for (let i = 0; i < 40; i += 1) events.push(ev("system", "thinking_tokens"));
    events.push(ev("assistant", "here it is"));
    expect(bodies(buildRows(events))).toEqual(["assistant:working on it", "assistant:here it is"]);
  });

  it("keeps system lines a person would actually read", () => {
    const rows = buildRows([
      ev("system", "session started (claude-opus-5)"),
      ev("system", "status"),
      ev("system", "plan limit (seven-day): allowed_warning"),
      ev("system", "retrying after authentication_failed (attempt 1/10)"),
    ]);
    expect(bodies(rows)).toEqual([
      "system:session started (claude-opus-5)",
      "system:plan limit (seven-day): allowed_warning",
      "system:retrying after authentication_failed (attempt 1/10)",
    ]);
  });

  it("never drops a heartbeat word that came from a task rather than the main loop", () => {
    const rows = buildRows([ev("system", "status", { parent_tool_use_id: "toolu_1" })]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ type: "subagent" });
  });
});

describe("background work", () => {
  it("folds the CLI's doubled narration into one step (run 96's shape)", () => {
    const rows = buildRows([
      ev("tool_use", 'echo "=== RADARR ==="', { tool_name: "Bash" }),
      ev("system", "Fetch Radarr version, profiles, custom formats", { parent_tool_use_id: "toolu_A" }),
      ev("system", "Fetch Radarr version, profiles, custom formats", { parent_tool_use_id: "toolu_A" }),
    ]);
    const group = rows[1];
    expect(group).toMatchObject({ type: "subagent", parentId: "toolu_A", tools: 0 });
    expect(group.type === "subagent" && group.steps).toHaveLength(1);
    expect(group.type === "subagent" && group.activity).toBe(
      "Fetch Radarr version, profiles, custom formats",
    );
  });

  it("names a group from the agent name when the CLI supplies one", () => {
    const rows = buildRows([
      ev("system", "Reading the config", { parent_tool_use_id: "toolu_A" }),
      ev("tool_use", "cat x", { tool_name: "Read", parent_tool_use_id: "toolu_A", agent_name: "Explore" }),
    ]);
    expect(rows[0]).toMatchObject({ type: "subagent", name: "Explore", tools: 1 });
    expect(rows[0].type === "subagent" && rows[0].activity).toBe("Read · cat x");
  });

  it("reads two tasks issued at once as two groups (run 96 seq 21-28)", () => {
    const rows = buildRows([
      ev("tool_use", "radarr", { tool_name: "Bash" }),
      ev("tool_use", "sonarr", { tool_name: "Bash" }),
      ev("system", "Fetch Radarr", { parent_tool_use_id: "toolu_A" }),
      ev("tool_result", "=== RADARR ==="),
      ev("system", "Fetch Sonarr", { parent_tool_use_id: "toolu_B" }),
    ]);
    expect(bodies(rows)).toEqual([
      "tool_use:radarr",
      "tool_use:sonarr",
      "sub:toolu_A",
      "tool_result:=== RADARR ===",
      "sub:toolu_B",
    ]);
  });
});

describe("what is still running", () => {
  const parallel = () => [
    ev("tool_use", "radarr", { tool_name: "Bash" }),
    ev("tool_use", "sonarr", { tool_name: "Bash" }),
    ev("system", "Fetch Radarr", { parent_tool_use_id: "toolu_A" }),
    ev("system", "Fetch Sonarr", { parent_tool_use_id: "toolu_B" }),
  ];

  it("shows both concurrent tasks as live while the turn is going", () => {
    const rows = buildRows(parallel(), { live: true });
    expect(rows.filter((r) => r.type === "subagent" && r.running)).toHaveLength(2);
  });

  it("stops calling one live once its result has come back", () => {
    const rows = buildRows([...parallel(), ev("tool_result", "=== RADARR ===")], { live: true });
    expect(rows.filter((r) => r.type === "subagent" && r.running)).toHaveLength(0);
  });

  it("never claims work is in flight in a finished transcript", () => {
    const rows = buildRows(parallel(), { live: false });
    expect(rows.filter((r) => r.type === "subagent" && r.running)).toHaveLength(0);
  });

  it("keeps a task live past another tool call, which does not mean it returned", () => {
    const rows = buildRows(
      [
        ev("system", "Poll sampler output", { parent_tool_use_id: "toolu_A" }),
        ev("tool_use", "another", { tool_name: "Bash" }),
      ],
      { live: true },
    );
    expect(rows[0]).toMatchObject({ running: true });
  });
});

describe("what the running turn is doing", () => {
  it("names the tool it is on and shows the command's first line", () => {
    const progress = turnProgress([
      ev("assistant", "Checking."),
      ev("tool_use", "timeout 110 ssh example-prod-sb '\ndate\n'", { tool_name: "Bash" }),
    ]);
    expect(progress).toMatchObject({ doing: "Bash", detail: "timeout 110 ssh example-prod-sb '", tools: 1, steps: 2 });
  });

  it("says it is writing while reply text is streaming in", () => {
    const progress = turnProgress([ev("tool_result", "ok")], { streaming: true });
    expect(progress.doing).toBe("writing the reply");
  });

  it("calls a heartbeat what it is — thinking — rather than showing the word", () => {
    expect(turnProgress([ev("tool_result", "ok"), ev("system", "thinking_tokens")]).doing).toBe("thinking");
  });

  it("does not count heartbeats as steps", () => {
    const events = [ev("assistant", "hi")];
    for (let i = 0; i < 10; i += 1) events.push(ev("system", "thinking_tokens"));
    expect(turnProgress(events).steps).toBe(1);
  });

  it("reports a task's own description while one is the latest thing to happen", () => {
    const progress = turnProgress([
      ev("tool_use", "poll", { tool_name: "Bash" }),
      ev("system", "Poll sampler output", { parent_tool_use_id: "toolu_A" }),
    ]);
    expect(progress.doing).toBe("Poll sampler output");
  });

  it("has something to say before the first event arrives", () => {
    expect(turnProgress([]).doing).toBe("starting up");
  });
});

describe("elapsed", () => {
  it("counts seconds for the first minute", () => {
    expect(elapsed(0, 43_000)).toBe("43s");
  });
  it("counts minutes and seconds after that", () => {
    expect(elapsed(0, 83_000)).toBe("1m 23s");
  });
  it("drops the seconds past an hour", () => {
    expect(elapsed(0, 3_900_000)).toBe("1h 5m");
  });
  it("never counts backwards from a clock that disagrees", () => {
    expect(elapsed(5_000, 0)).toBe("0s");
  });
});
