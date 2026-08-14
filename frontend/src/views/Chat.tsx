import type * as React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, openSocket } from "../api";
import type {
  Account,
  AgentEvent,
  Approval,
  ApprovalMode,
  Capability,
  Run,
  Session,
  WsMessage,
} from "../types";

type ChatEvent = Pick<
  AgentEvent,
  "run_id" | "seq" | "kind" | "text" | "tool_name" | "parent_tool_use_id" | "agent_name"
> & {
  id?: number;
  is_error?: boolean;
};

/**
 * Main-loop messages carry a null parent_tool_use_id; a subagent's carry the id
 * of the Agent tool call that spawned it. Consecutive events sharing a parent
 * are folded into one collapsible group, which reads the same as the nesting
 * Claude Code shows without needing to resolve tool-call ids.
 */
type Row =
  | { type: "event"; event: ChatEvent }
  | { type: "subagent"; parentId: string; name: string; events: ChatEvent[] };

function groupSubagents(events: ChatEvent[]): Row[] {
  const rows: Row[] = [];
  for (const e of events) {
    if (!e.parent_tool_use_id) {
      rows.push({ type: "event", event: e });
      continue;
    }
    const last = rows[rows.length - 1];
    if (last?.type === "subagent" && last.parentId === e.parent_tool_use_id) {
      last.events.push(e);
      if (!last.name && e.agent_name) last.name = e.agent_name;
    } else {
      rows.push({
        type: "subagent",
        parentId: e.parent_tool_use_id,
        name: e.agent_name ?? "",
        events: [e],
      });
    }
  }
  return rows;
}

const eventKey = (e: ChatEvent) => `${e.run_id}:${e.seq}`;

/** Kinds that mean the streaming buffer for this run has been superseded. */
const FLUSHES_LIVE = new Set(["assistant", "thinking", "result", "tool_use"]);

export default function Chat({
  sessionId,
  onChanged,
}: {
  sessionId: string;
  onChanged: () => void;
}) {
  const [session, setSession] = useState<Session | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [live, setLive] = useState<Record<number, string>>({});
  const [connected, setConnected] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);

  const navigate = useNavigate();
  const bodyRef = useRef<HTMLDivElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const pinnedRef = useRef(true);

  // Highest persisted event id we hold, so a reconnect can ask for just the
  // gap instead of the whole transcript. Websocket frames carry no id, so this
  // only advances on a fetch — worst case we refetch a few events we already
  // have, which mergeEvents dedupes.
  const lastEventIdRef = useRef(0);

  const mergeEvents = useCallback((incoming: ChatEvent[]) => {
    for (const e of incoming) {
      if (typeof e.id === "number" && e.id > lastEventIdRef.current) lastEventIdRef.current = e.id;
    }
    setEvents((prev) => {
      const byKey = new Map(prev.map((e) => [eventKey(e), e]));
      for (const e of incoming) byKey.set(eventKey(e), { ...byKey.get(eventKey(e)), ...e });
      return [...byKey.values()].sort(
        (a, b) => a.run_id - b.run_id || a.seq - b.seq,
      );
    });
  }, []);

  const reload = useCallback(
    async (incremental = false) => {
      try {
        const t = await api.transcript(sessionId, incremental ? lastEventIdRef.current : 0);
        setSession(t.session);
        setRuns(t.runs);
        mergeEvents(t.events);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [sessionId, mergeEvents],
  );

  useEffect(() => {
    lastEventIdRef.current = 0;
    void reload();
  }, [reload]);

  // Skills and slash commands depend on the session's workspace, so they are
  // fetched per session rather than globally.
  useEffect(() => {
    api
      .capabilities(sessionId)
      .then(setCapabilities)
      .catch(() => setCapabilities([]));
  }, [sessionId]);

  // Only needed to put a name to the account ids on a run.
  useEffect(() => {
    api.accounts().then(setAccounts).catch(() => setAccounts([]));
  }, []);

  // An agent is blocked on each of these, so they must survive a page reload —
  // the websocket only announces new ones.
  const loadApprovals = useCallback(async () => {
    try {
      setApprovals(await api.approvals({ session_id: sessionId, status: "pending" }));
    } catch {
      /* the transcript is still usable without them */
    }
  }, [sessionId]);

  useEffect(() => {
    void loadApprovals();
  }, [loadApprovals]);

  const accountName = (id: number | null) =>
    accounts.find((a) => a.id === id)?.name ?? "an account";

  // Live feed. Reconnects on drop and refetches so nothing is missed.
  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;
    let everConnected = false;

    const connect = () => {
      socket = openSocket(sessionId);
      socket.onopen = () => {
        setConnected(true);
        // Anything the agent emitted while the socket was down never arrived,
        // so pull the gap. Skipped on the first open — the initial load has it.
        if (everConnected) {
          void reload(true);
          void loadApprovals();
        }
        everConnected = true;
      };
      socket.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 2000);
      };
      socket.onmessage = (raw) => {
        const msg: WsMessage = JSON.parse(raw.data);
        if (msg.type === "event") {
          if (msg.kind === "delta") {
            setLive((prev) => ({ ...prev, [msg.run_id]: (prev[msg.run_id] ?? "") + (msg.text ?? "") }));
            return;
          }
          if (FLUSHES_LIVE.has(msg.kind)) {
            setLive((prev) => {
              if (!(msg.run_id in prev)) return prev;
              const next = { ...prev };
              delete next[msg.run_id];
              return next;
            });
          }
          if (typeof msg.seq === "number") {
            mergeEvents([
              {
                run_id: msg.run_id,
                seq: msg.seq,
                kind: msg.kind,
                text: msg.text,
                tool_name: msg.tool_name,
                is_error: msg.is_error,
                parent_tool_use_id: msg.parent_tool_use_id ?? null,
                agent_name: msg.agent_name ?? null,
              },
            ]);
          }
        } else if (msg.type === "approval.requested") {
          setApprovals((prev) =>
            prev.some((a) => a.id === msg.approval_id)
              ? prev
              : [
                  ...prev,
                  {
                    id: msg.approval_id,
                    run_id: msg.run_id,
                    session_id: msg.session_id,
                    provider: msg.provider,
                    kind: msg.kind,
                    tool_name: msg.tool_name,
                    summary: msg.summary,
                    request: msg.request,
                    status: "pending",
                    decided_by_id: null,
                    decided_at: null,
                    note: null,
                    created_at: new Date().toISOString(),
                  },
                ],
          );
        } else if (msg.type === "approval.resolved") {
          // Someone answered — possibly in another tab, possibly it timed out.
          setApprovals((prev) => prev.filter((a) => a.id !== msg.approval_id));
        } else if (msg.type === "run.started" || msg.type === "run.finished") {
          setLive((prev) => {
            const next = { ...prev };
            delete next[msg.run_id];
            return next;
          });
          void reload();
          onChanged();
        }
      };
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, [sessionId, mergeEvents, reload, loadApprovals, onChanged]);

  // Keep the newest output in view, unless the reader has scrolled up.
  useEffect(() => {
    const el = bodyRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [events, live, runs]);

  const onScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const activeRun = useMemo(
    () => runs.find((r) => r.status === "running" || r.status === "queued"),
    [runs],
  );

  const send = async (event: React.FormEvent | React.KeyboardEvent) => {
    event.preventDefault();
    if (!prompt.trim() || activeRun) return;
    setSending(true);
    setError(null);
    try {
      await api.prompt(sessionId, prompt);
      setPrompt("");
      pinnedRef.current = true;
      await reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSending(false);
    }
  };

  const cancel = async () => {
    if (!activeRun) return;
    try {
      await api.cancelRun(activeRun.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const saveTitle = async (event: React.FormEvent) => {
    event.preventDefault();
    const title = draftTitle.trim();
    if (!title) return;
    setRenaming(false);
    try {
      setSession(await api.renameSession(sessionId, title));
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async () => {
    if (
      !confirm(
        `Delete "${session?.title ?? "this session"}"?\n\n` +
          "The transcript and its run history go with it. The agent's work on " +
          "disk is left untouched.",
      )
    )
      return;
    try {
      await api.deleteSession(sessionId);
      onChanged();
      navigate("/sessions");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  /** Insert a `/name ` token at the caret. */
  const insertCapability = (name: string) => {
    const el = promptRef.current;
    const token = `/${name} `;
    if (!el) {
      setPrompt((p) => (p ? `${p}\n${token}` : token));
    } else {
      const start = el.selectionStart ?? prompt.length;
      const end = el.selectionEnd ?? start;
      setPrompt(prompt.slice(0, start) + token + prompt.slice(end));
      window.setTimeout(() => {
        el.focus();
        const at = start + token.length;
        el.setSelectionRange(at, at);
      }, 0);
    }
    setPickerOpen(false);
  };

  const eventsByRun = useMemo(() => {
    const map = new Map<number, ChatEvent[]>();
    for (const e of events) {
      const list = map.get(e.run_id) ?? [];
      list.push(e);
      map.set(e.run_id, list);
    }
    return map;
  }, [events]);

  return (
    <div className="chat">
      <div className="chat-head">
        <Link to="/sessions" className="back-link" aria-label="Back to sessions">
          ←
        </Link>
        {renaming ? (
          <form className="rename-form" onSubmit={saveTitle}>
            <input
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && setRenaming(false)}
              autoFocus
              maxLength={255}
            />
            <button className="primary" type="submit">
              Save
            </button>
            <button type="button" onClick={() => setRenaming(false)}>
              Cancel
            </button>
          </form>
        ) : (
          <h1
            className="session-title"
            title="Click to rename"
            onClick={() => {
              setDraftTitle(session?.title ?? "");
              setRenaming(true);
            }}
          >
            {session?.title ?? "…"}
          </h1>
        )}
        {session && !renaming && (
          <>
            <span className="pill">{session.provider}</span>
            {session.model && <span className="pill">{session.model}</span>}
            <span className={`pill ${session.status}`}>{session.status}</span>
          </>
        )}
        <span className={`pill ${connected ? "ok" : "failed"}`}>
          {connected ? "live" : "reconnecting"}
        </span>
        {session && !renaming && (
          <select
            className="approval-select"
            value={session.approval_mode ?? "ask"}
            title="How this session handles tool permissions"
            onChange={async (e) => {
              const approval_mode = e.target.value as ApprovalMode;
              try {
                setSession(await api.patchSession(sessionId, { approval_mode }));
              } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
              }
            }}
          >
            <option value="ask">Ask me each time</option>
            <option value="auto">Auto-approve edits</option>
            <option value="bypass">Bypass all checks</option>
          </select>
        )}
        {activeRun && (
          <button className="danger" onClick={cancel}>
            Stop
          </button>
        )}
        {!renaming && (
          <button className="danger" onClick={remove} title="Delete this session">
            Delete
          </button>
        )}
      </div>

      <div className="chat-body" ref={bodyRef} onScroll={onScroll}>
        {error && <div className="error-banner">{error}</div>}
        {runs.length === 0 && <div className="empty">Send the first task below.</div>}
        {runs.map((run) => (
          <div key={run.id} style={{ display: "contents" }}>
            <div className="msg prompt">
              <div className="who">
                you{run.schedule_id ? " (scheduled)" : ""}
              </div>
              <pre>{run.prompt}</pre>
            </div>

            {/* A silent failover looks like the first account simply worked.
                Say which one actually answered. */}
            {run.failed_over_from_id !== null && (
              <div className="msg system">
                {accountName(run.failed_over_from_id)} hit its limit — switched to{" "}
                {accountName(run.account_id)}.
              </div>
            )}

            {groupSubagents(eventsByRun.get(run.id) ?? []).map((row) =>
              row.type === "event" ? (
                <Bubble key={eventKey(row.event)} event={row.event} />
              ) : (
                <SubagentGroup
                  key={`sub:${row.parentId}:${eventKey(row.events[0])}`}
                  name={row.name}
                  events={row.events}
                />
              ),
            )}

            {live[run.id] && (
              <div className="msg assistant live">
                <div className="who">assistant · streaming</div>
                <pre>{live[run.id]}</pre>
              </div>
            )}

            {run.status !== "succeeded" && run.status !== "running" && run.status !== "queued" && (
              <div className={`msg ${run.status === "cancelled" ? "system" : "error"}`}>
                <div className="who">
                  run {run.status}
                  {run.exit_code !== null ? ` · exit ${run.exit_code}` : ""}
                </div>
                {run.error && <pre>{run.error}</pre>}
              </div>
            )}

            {run.cost_usd != null && (
              <div
                className="msg system"
                title={
                  "What these tokens would cost at pay-as-you-go API rates. " +
                  "When the CLI is signed in with a Claude subscription, this is " +
                  "not an additional charge — usage counts against your plan."
                }
              >
                ≈ ${run.cost_usd.toFixed(4)} at API rates
              </div>
            )}
          </div>
        ))}
      </div>

      {/* The agent is stopped dead until one of these is answered, so they sit
          between the transcript and the composer rather than inline. */}
      {approvals.map((approval) => (
        <ApprovalCard
          key={approval.id}
          approval={approval}
          onDone={(id) => setApprovals((prev) => prev.filter((a) => a.id !== id))}
        />
      ))}

      <form className="chat-foot" onSubmit={send}>
        {pickerOpen && (
          <div className="picker">
            <div className="picker-head">
              <strong>Skills &amp; commands</strong>
              <button type="button" onClick={() => setPickerOpen(false)}>
                Close
              </button>
            </div>
            {capabilities.length === 0 ? (
              <div className="empty" style={{ padding: 14 }}>
                Nothing found. Skills live in <code>.claude/skills/</code> in the workspace or in
                the container's <code>~/.claude/</code>.
              </div>
            ) : (
              <ul>
                {capabilities.map((c) => (
                  <li key={`${c.kind}:${c.name}`}>
                    <button type="button" onClick={() => insertCapability(c.name)}>
                      <span className="cap-name">/{c.name}</span>
                      <span className={`pill ${c.kind === "skill" ? "ok" : ""}`}>{c.kind}</span>
                      <span className="cap-src">{c.source}</span>
                      {c.description && <span className="cap-desc">{c.description}</span>}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        <textarea
          ref={promptRef}
          rows={3}
          value={prompt}
          placeholder={
            activeRun
              ? "Agent is working — stop it or wait…"
              : "Describe the task…  (Ctrl+Enter to send, / for skills)"
          }
          disabled={!!activeRun}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send(e);
          }}
        />
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" type="submit" disabled={!!activeRun || sending || !prompt.trim()}>
            {sending ? "Sending…" : "Send"}
          </button>
          <button
            type="button"
            onClick={() => setPickerOpen((v) => !v)}
            title="Insert a skill or slash command"
          >
            / Skills
            {capabilities.length > 0 && (
              <span className="pill" style={{ marginLeft: 6 }}>{capabilities.length}</span>
            )}
          </button>
          {session?.provider_session_id && (
            <span className="mono session-id" style={{ color: "var(--text-dim)" }}>
              {session.provider} session {session.provider_session_id.slice(0, 12)}…
            </span>
          )}
        </div>
      </form>
    </div>
  );
}

function SubagentGroup({ name, events }: { name: string; events: ChatEvent[] }) {
  // Open while the subagent is still working, so you can watch it think; the
  // reader can collapse it once it has finished.
  const [open, setOpen] = useState(true);
  const tools = events.filter((e) => e.kind === "tool_use").length;
  return (
    <div className="subagent">
      <button className="subagent-head" onClick={() => setOpen((v) => !v)}>
        <span className="subagent-caret">{open ? "▾" : "▸"}</span>
        <span className="subagent-name">{name || "subagent"}</span>
        <span className="subagent-meta">
          {events.length} step{events.length === 1 ? "" : "s"}
          {tools > 0 && ` · ${tools} tool call${tools === 1 ? "" : "s"}`}
        </span>
      </button>
      {open && (
        <div className="subagent-body">
          {events.map((e) => (
            <Bubble key={eventKey(e)} event={e} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One blocked tool call, with the two buttons that unblock it.
 *
 * The agent process is genuinely parked on this — it is not a notification —
 * so the card states what will run and stays until it is answered.
 */
function ApprovalCard({
  approval,
  onDone,
}: {
  approval: Approval;
  onDone: (id: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showRaw, setShowRaw] = useState(false);

  const answer = async (allowed: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await api.decideApproval(
        approval.id,
        allowed,
        allowed ? null : "Denied by the AIOps operator.",
      );
      onDone(approval.id);
    } catch (err) {
      // A 409 means it was already answered elsewhere or the run ended, so the
      // card is stale either way — show why, then clear it.
      setError(err instanceof Error ? err.message : String(err));
      window.setTimeout(() => onDone(approval.id), 2500);
    } finally {
      setBusy(false);
    }
  };

  const detail = approval.summary ?? approval.tool_name ?? "a tool call";

  return (
    <div className="approval">
      <div className="approval-head">
        <span className="pill warn">needs approval</span>
        <strong>{approval.tool_name ?? approval.kind}</strong>
        <span className="approval-provider">{approval.provider}</span>
      </div>
      <pre className="approval-detail">{detail}</pre>
      {approval.request && Object.keys(approval.request).length > 0 && (
        <>
          <button type="button" className="linkish" onClick={() => setShowRaw((v) => !v)}>
            {showRaw ? "Hide" : "Show"} full request
          </button>
          {showRaw && <pre className="approval-raw">{JSON.stringify(approval.request, null, 2)}</pre>}
        </>
      )}
      {error && <div className="error-banner">{error}</div>}
      <div className="row approval-actions">
        <button className="primary" type="button" disabled={busy} onClick={() => answer(true)}>
          Accept
        </button>
        <button className="danger" type="button" disabled={busy} onClick={() => answer(false)}>
          Deny
        </button>
        <span className="approval-hint">The agent is waiting.</span>
      </div>
    </div>
  );
}

function Bubble({ event }: { event: ChatEvent }) {
  const label =
    event.kind === "tool_use"
      ? `tool · ${event.tool_name ?? "?"}`
      : event.kind === "tool_result"
        ? "tool result"
        : event.kind;

  if (event.kind === "system") {
    return <div className="msg system">{event.text}</div>;
  }

  const long = (event.text?.length ?? 0) > 1200;
  return (
    <div className={`msg ${event.kind}${event.is_error ? " error" : ""}`}>
      <div className="who">{label}</div>
      {long ? (
        <details>
          <summary>{event.text!.slice(0, 200)}… ({event.text!.length} chars)</summary>
          <pre>{event.text}</pre>
        </details>
      ) : (
        <pre>{event.text}</pre>
      )}
    </div>
  );
}
