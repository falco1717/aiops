/**
 * The composer's queue rules, exercised directly.
 *
 * `queueView` and `composerState` are pure functions of a run list, which is why
 * they are a module rather than three `useMemo`s inside Chat.tsx — the component
 * needs a DOM and this repo has no DOM test rig, and the interesting cases here
 * are not about rendering. They are about which turn is untouchable (the one the
 * agent is on), which can still be taken back (every one behind it), and what
 * the box is allowed to claim while the agent works.
 *
 * The rules that matter most are the honest ones. A queued message is not
 * steering: the CLIs run headless, one process per turn, so nothing can be
 * handed to a turn that has already started. If the copy ever starts implying
 * otherwise, these fail.
 */
import { describe, expect, it } from "vitest";
import type { Run } from "./types";
import { canWithdraw, composerState, queueView } from "./queue";

/** A run, with only the fields the queue rules read. */
function run(id: number, status: string, requested_by_id: number | null = 1): Run {
  return {
    id,
    session_id: "s",
    schedule_id: null,
    prompt: `p${id}`,
    provider: "claude",
    model: null,
    carries_handoff: false,
    requested_by_id,
    status,
    exit_code: null,
    error: null,
    account_id: null,
    failed_over_from_id: null,
    input_tokens: null,
    output_tokens: null,
    context_tokens: null,
    cost_usd: null,
    command: [],
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
  };
}

const idle = queueView([run(1, "succeeded"), run(2, "failed")]);

describe("which turn is in flight", () => {
  it("has nothing active when every turn has finished", () => {
    expect(idle.active).toBeNull();
    expect(idle.waiting).toEqual([]);
    expect(idle.busy).toBe(false);
  });

  it("treats an empty session as idle", () => {
    expect(queueView([]).busy).toBe(false);
  });

  it("names the running turn as the active one", () => {
    const v = queueView([run(1, "succeeded"), run(2, "running")]);
    expect(v.active?.id).toBe(2);
    expect(v.waiting).toEqual([]);
    expect(v.busy).toBe(true);
  });

  it("counts a turn that has been picked up but is still 'queued' as active", () => {
    // The server flips the row to 'running' a moment after it dispatches it.
    // In that window the turn is no more withdrawable than a running one.
    const v = queueView([run(7, "queued")]);
    expect(v.active?.id).toBe(7);
    expect(v.waiting).toEqual([]);
  });

  it("puts everything behind the running turn in the queue", () => {
    const v = queueView([run(1, "running"), run(2, "queued"), run(3, "queued")]);
    expect(v.active?.id).toBe(1);
    expect(v.waiting.map((r) => r.id)).toEqual([2, 3]);
  });

  it("prefers the running turn even when an older row is still queued", () => {
    const v = queueView([run(9, "queued"), run(10, "running")]);
    expect(v.active?.id).toBe(10);
    expect(v.waiting.map((r) => r.id)).toEqual([9]);
  });
});

describe("queue order is by id, not by arrival in the array", () => {
  it("sorts a transcript that came back out of order", () => {
    const v = queueView([run(4, "queued"), run(1, "running"), run(3, "queued"), run(2, "queued")]);
    expect(v.active?.id).toBe(1);
    expect(v.waiting.map((r) => r.id)).toEqual([2, 3, 4]);
  });

  it("ignores finished turns when deciding the order", () => {
    const v = queueView([run(1, "succeeded"), run(2, "cancelled"), run(3, "running"), run(4, "queued")]);
    expect(v.waiting.map((r) => r.id)).toEqual([4]);
  });
});

describe("what may be taken back", () => {
  const v = queueView([run(1, "running"), run(2, "queued"), run(3, "queued")]);

  it("lets a queued message be withdrawn", () => {
    expect(canWithdraw(run(2, "queued"), v)).toBe(true);
    expect(canWithdraw(run(3, "queued"), v)).toBe(true);
  });

  it("never offers to withdraw the turn the agent is on", () => {
    expect(canWithdraw(run(1, "running"), v)).toBe(false);
  });

  it("never offers to withdraw the turn that is starting", () => {
    const starting = queueView([run(5, "queued"), run(6, "queued")]);
    expect(canWithdraw(run(5, "queued"), starting)).toBe(false);
    expect(canWithdraw(run(6, "queued"), starting)).toBe(true);
  });

  it("never offers to withdraw a turn that already finished", () => {
    expect(canWithdraw(run(1, "succeeded"), v)).toBe(false);
    expect(canWithdraw(run(99, "failed"), v)).toBe(false);
  });
});

describe("what the composer says while the agent works", () => {
  const busy = queueView([run(1, "running")]);
  const twoWaiting = queueView([run(1, "running"), run(2, "queued"), run(3, "queued")]);

  it("says nothing extra when the session is idle", () => {
    expect(composerState(idle, { enterSends: true, sending: false }).notice).toBeNull();
  });

  it("sends, rather than queues, when nothing is in flight", () => {
    expect(composerState(idle, { enterSends: true, sending: false }).sendLabel).toBe("Send");
  });

  it("calls the button Queue while a turn is running", () => {
    expect(composerState(busy, { enterSends: true, sending: false }).sendLabel).toBe("Queue");
  });

  it("says the message does not reach the turn in progress", () => {
    const notice = composerState(busy, { enterSends: true, sending: false }).notice ?? "";
    expect(notice).toContain("queued");
    expect(notice).toContain("does not reach the turn in progress");
  });

  it("never claims the agent will see it, or that it can be steered", () => {
    for (const view of [busy, twoWaiting]) {
      const notice = composerState(view, { enterSends: true, sending: false }).notice ?? "";
      const placeholder = composerState(view, { enterSends: true, sending: false }).placeholder;
      for (const lie of ["will see", "right away", "immediately", "steer", "redirect"]) {
        expect(notice.toLowerCase()).not.toContain(lie);
        expect(placeholder.toLowerCase()).not.toContain(lie);
      }
    }
  });

  it("counts what is already waiting, in a sentence that reads", () => {
    const one = composerState(
      queueView([run(1, "running"), run(2, "queued")]),
      { enterSends: true, sending: false },
    ).notice;
    expect(one).toContain("1 message is already waiting");
    expect(one).toContain("yours goes after it");

    const two = composerState(twoWaiting, { enterSends: true, sending: false }).notice;
    expect(two).toContain("2 messages are already waiting");
    expect(two).toContain("yours goes after them");
  });

  it("does not talk about a queue when there is nothing in it yet", () => {
    expect(composerState(busy, { enterSends: true, sending: false }).notice).not.toContain(
      "already waiting",
    );
  });

  it("keeps the box usable, and says what Enter does, at either layout", () => {
    expect(composerState(busy, { enterSends: true, sending: false }).placeholder).toContain(
      "Enter to send",
    );
    expect(composerState(busy, { enterSends: false, sending: false }).placeholder).toContain(
      "Ctrl+Enter to send",
    );
  });

  it("reports the in-flight submit under its own name", () => {
    expect(composerState(busy, { enterSends: true, sending: true }).sendLabel).toBe("Queueing…");
    expect(composerState(idle, { enterSends: true, sending: true }).sendLabel).toBe("Sending…");
  });
});
