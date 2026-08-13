import type * as React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, openSocket } from "../api";
import type { AgentEvent, Run, Session, WsMessage } from "../types";

type ChatEvent = Pick<AgentEvent, "run_id" | "seq" | "kind" | "text" | "tool_name"> & {
  id?: number;
  is_error?: boolean;
};

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

  const bodyRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  const mergeEvents = useCallback((incoming: ChatEvent[]) => {
    setEvents((prev) => {
      const byKey = new Map(prev.map((e) => [eventKey(e), e]));
      for (const e of incoming) byKey.set(eventKey(e), { ...byKey.get(eventKey(e)), ...e });
      return [...byKey.values()].sort(
        (a, b) => a.run_id - b.run_id || a.seq - b.seq,
      );
    });
  }, []);

  const reload = useCallback(async () => {
    try {
      const t = await api.transcript(sessionId);
      setSession(t.session);
      setRuns(t.runs);
      mergeEvents(t.events);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [sessionId, mergeEvents]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Live feed. Reconnects on drop and refetches so nothing is missed.
  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      socket = openSocket(sessionId);
      socket.onopen = () => setConnected(true);
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
              },
            ]);
          }
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
  }, [sessionId, mergeEvents, reload, onChanged]);

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
        <h1>{session?.title ?? "…"}</h1>
        {session && (
          <>
            <span className="pill">{session.provider}</span>
            {session.model && <span className="pill">{session.model}</span>}
            <span className={`pill ${session.status}`}>{session.status}</span>
          </>
        )}
        <span className={`pill ${connected ? "ok" : "failed"}`}>
          {connected ? "live" : "reconnecting"}
        </span>
        {activeRun && (
          <button className="danger" onClick={cancel}>
            Stop
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

            {(eventsByRun.get(run.id) ?? []).map((e) => (
              <Bubble key={eventKey(e)} event={e} />
            ))}

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
              <div className="msg system">cost ${run.cost_usd.toFixed(4)}</div>
            )}
          </div>
        ))}
      </div>

      <form className="chat-foot" onSubmit={send}>
        <textarea
          rows={3}
          value={prompt}
          placeholder={
            activeRun ? "Agent is working — stop it or wait…" : "Describe the task…  (Ctrl+Enter to send)"
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
          {session?.provider_session_id && (
            <span className="mono" style={{ color: "var(--text-dim)" }}>
              {session.provider} session {session.provider_session_id.slice(0, 12)}…
            </span>
          )}
        </div>
      </form>
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
