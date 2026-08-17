import type * as React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api, openSocket } from "../api";
import { EFFORT_HINT, effortChoices } from "../effort";
import { displayName, fullName, nameById } from "../names";
import type { Suggestion, TokenMatch } from "../mentions";
import { activeToken, applySuggestion, emptyHint, suggestionsFor } from "../mentions";
import { composerKeyAction } from "../composerKeys";
import { canWithdraw, composerState, queueView } from "../queue";
import { shotsIn } from "../screenshots";
import { ScreenshotStrip } from "../components/Screenshots";
import type { SubagentRow, TranscriptEvent } from "../transcript";
import { buildRows, elapsed, turnProgress } from "../transcript";
import { parseUtc } from "../time";
import type { Draft } from "../questions";
import {
  emptyDraft,
  isAnswered,
  setOther,
  toAnswers,
  toggleOption,
  validate,
} from "../questions";
import type {
  Account,
  Approval,
  ApprovalMode,
  ApprovalQuestion,
  Attachment,
  Capability,
  Exposure,
  Preset,
  ProviderInfo,
  Run,
  Session,
  SessionFiles,
  Target,
  Team,
  User,
  UserSummary,
  WsMessage,
} from "../types";

/**
 * The phone layout, as the stylesheet defines it.
 *
 * There was no JavaScript-side notion of "mobile" in this codebase before this
 * — the split was drawn entirely in CSS — and inventing a second definition
 * would guarantee the two drift apart. So this is the *same string* the mobile
 * block in `styles.css` opens with, and desktop is its exact complement rather
 * than a `min-width` a fractional viewport could fall between.
 */
const MOBILE_QUERY = "(max-width: 860px)";

/** True at the desktop layout. Re-reads on resize, so a dragged window keeps up. */
function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(
    () => typeof window === "undefined" || !window.matchMedia(MOBILE_QUERY).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = () => setIsDesktop(!mq.matches);
    onChange();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return isDesktop;
}

/**
 * Main-loop messages carry a null parent_tool_use_id; work done under a tool
 * call — a subagent, or a task the CLI narrates — carries that call's id. How
 * those become rows, and which of them say something new, lives in
 * `transcript.ts` next to the recorded evidence for each rule.
 */
type ChatEvent = TranscriptEvent;

const eventKey = (e: ChatEvent) => `${e.run_id}:${e.seq}`;

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Sent with a message that carried files but no words of its own. */
const ATTACHMENTS_ONLY_PROMPT = "Take a look at the attached file(s).";

/** ids for the combobox wiring; the listbox is a singleton per composer. */
const LISTBOX_ID = "composer-suggestions";
const optionId = (index: number) => `${LISTBOX_ID}-option-${index}`;

/**
 * What to call whoever sent a turn.
 *
 * Three cases, and the third is the one that matters. `requested_by_id` was
 * added after sessions became shareable and was never backfilled, so old turns
 * carry no sender at all. Falling back to "you" there is exactly the bug this
 * replaces — in a shared transcript the reader is usually not the author — and
 * falling back to the session owner would be a guess dressed up as a fact. So
 * an unattributed turn says so.
 */
function turnAuthor(run: Run, me: User, directory: UserSummary[]): string {
  if (run.requested_by_id === null) return "sent before AIOps recorded senders";
  if (run.requested_by_id === me.id) return "you";
  const who = directory.find((u) => u.id === run.requested_by_id);
  // A sender who has since been deleted still has an id on the run.
  return who ? displayName(who) : "a since-removed user";
}

/**
 * Where a waiting message sits in the line.
 *
 * "next turn" rather than "1st", because the useful fact is not its index — it
 * is that exactly one turn has to finish before this one starts.
 */
function position(run: Run, waiting: Run[]): string {
  const at = waiting.findIndex((r) => r.id === run.id);
  return at <= 0 ? "next turn" : `${at + 1} turns away`;
}

/** Kinds that mean the streaming buffer for this run has been superseded. */
const FLUSHES_LIVE = new Set(["assistant", "thinking", "result", "tool_use"]);

export default function Chat({
  sessionId,
  me,
  onChanged,
}: {
  sessionId: string;
  me: User;
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
  // What `@` offers: exactly the systems this user can reach, which is exactly
  // what a turn of theirs will be given. Anything else would be a menu of names
  // the run cannot resolve.
  const [targets, setTargets] = useState<Target[]>([]);
  // Where the caret is, tracked because the suggestion menu is a function of
  // the word the caret is *in* — not of the whole prompt.
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  // The token Escape was pressed on. Typing changes the token, which is what
  // brings the menu back; moving the caret back to the same word does not.
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  // Somebody else's answers to the approvals above, kept on screen briefly so
  // a card that disappears is attributable rather than mysterious.
  const [decisions, setDecisions] = useState<{ id: number; text: string }[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  // Who can read this session, and which of *my* systems a turn of mine would
  // reach in it. Both come from the server, which is the only side that can see
  // the team memberships and the credential grants that decide them.
  const [exposure, setExposure] = useState<Exposure | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [acking, setAcking] = useState(false);
  const [teams, setTeams] = useState<Team[]>([]);
  const [directory, setDirectory] = useState<UserSummary[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  // Phone only: the settings and the destructive buttons live behind this, so
  // the header does not spend a third of a small screen on things you press
  // once a week. The stylesheet ignores it above 860px.
  const [toolsOpen, setToolsOpen] = useState(false);

  const navigate = useNavigate();
  const bodyRef = useRef<HTMLDivElement>(null);
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pinnedRef = useRef(true);
  // Read by the document-level Escape handler for the ⋯ menu, which must not
  // fire while the suggestion list is open — see `closeTools` below.
  const suggestOpenRef = useRef(false);

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
        // Always the full set, including files still waiting in the composer, so
        // an incremental refetch cannot make a staged upload disappear.
        setAttachments(t.attachments);
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

  // The `@` menu. Not session-scoped: /api/targets is already the caller's own
  // reachable set, which is the same set the runner materialises for their turn.
  useEffect(() => {
    api.targets().then(setTargets).catch(() => setTargets([]));
  }, []);

  // For the effort control: which levels this CLI accepts is the CLI's answer,
  // not ours, so the list has to come from the server. The presets come along
  // because a session that has not chosen an effort inherits its preset's.
  useEffect(() => {
    api.providers().then(setProviders).catch(() => setProviders([]));
    api.presets().then(setPresets).catch(() => setPresets([]));
  }, []);

  // Names for the ids on the session itself — which team owns it, who it was
  // shared with. Both endpoints are open to any signed-in user.
  useEffect(() => {
    api.teams().then(setTeams).catch(() => setTeams([]));
    api.userDirectory().then(setDirectory).catch(() => setDirectory([]));
  }, []);

  const loadExposure = useCallback(async () => {
    try {
      setExposure(await api.exposure(sessionId));
    } catch {
      // Not fatal to the conversation. The prompt endpoint refuses on its own
      // if an acknowledgement is owed, so a failure here cannot lose the check.
      setExposure(null);
    }
  }, [sessionId]);

  // Refetched whenever the sharing on the session row changes, so adding
  // somebody re-draws the warning with their name in it straight away. A team
  // gaining a member changes nothing here, which is why `send` asks again at the
  // moment it matters rather than trusting this copy.
  const audience = session
    ? `${session.owner_id}|${session.team_id}|${[...session.shared_user_ids].sort().join(",")}`
    : "";

  useEffect(() => {
    void loadExposure();
  }, [loadExposure, audience]);

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

  const providerLabel = (name: string | null) =>
    (name && providers.find((p) => p.name === name)?.label) ?? name ?? "the agent";

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
                    questions: msg.questions ?? [],
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
          // Whether it was a question is only known from the card that is being
          // removed, so it is read on the way past.
          let wasQuestion = false;
          setApprovals((prev) => {
            wasQuestion = prev.some(
              (a) => a.id === msg.approval_id && a.questions.length > 0,
            );
            return prev.filter((a) => a.id !== msg.approval_id);
          });
          // Whose answer it was. In a shared session the card can vanish under
          // somebody who was reading it, and "it disappeared" is not an answer
          // to "who let the agent run that". Skipped for your own decisions,
          // which you just made, and for expiries, which nobody made.
          // A question that nobody answers is also worth saying out loud. It
          // ends as a denial on the wire, so without this it is indistinguishable
          // from somebody having deliberately said no — and the agent carries on
          // with a choice the person never made. (The turn's own transcript
          // keeps the durable record; this is for whoever is watching.)
          const unanswered = wasQuestion && !msg.decided_by && msg.status === "expired";
          if ((msg.decided_by && msg.decided_by_id !== me.id) || unanswered) {
            const what = unanswered
              ? "Nobody answered the agent's questions in time, so it carried on without them."
              : wasQuestion
                ? msg.status === "allowed"
                  ? `${msg.decided_by} answered the agent's questions.`
                  : `${msg.decided_by} declined to answer the agent's questions.`
                : `${msg.decided_by} ${msg.status} this tool call.`;
            setDecisions((prev) => [
              ...prev.filter((d) => d.id !== msg.approval_id),
              { id: msg.approval_id, text: what },
            ]);
            window.setTimeout(
              () => setDecisions((prev) => prev.filter((d) => d.id !== msg.approval_id)),
              12000,
            );
          }
        } else if (
          msg.type === "run.queued" ||
          msg.type === "run.started" ||
          msg.type === "run.finished"
        ) {
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
  }, [sessionId, mergeEvents, reload, loadApprovals, onChanged, me.id]);

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

  // The turn in flight, and the messages waiting behind it. The rules — which
  // one counts as active, what may still be taken back, and what the composer
  // is allowed to claim while the agent works — are in `queue.ts` and tested
  // there rather than derived inline here.
  const queue = useMemo(() => queueView(runs), [runs]);
  const queuedIds = useMemo(() => new Set(queue.waiting.map((r) => r.id)), [queue]);

  /** Uploaded but not yet sent — what the composer is holding. */
  const staged = useMemo(() => attachments.filter((a) => a.run_id === null), [attachments]);

  const attachmentsByRun = useMemo(() => {
    const map = new Map<number, Attachment[]>();
    for (const a of attachments) {
      if (a.run_id === null) continue;
      map.set(a.run_id, [...(map.get(a.run_id) ?? []), a]);
    }
    return map;
  }, [attachments]);

  /** One shared path for the picker, the drop target and the paste handler. */
  const stage = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      setUploading(true);
      setError(null);
      try {
        for (const file of files) {
          const saved = await api.uploadAttachment(sessionId, file);
          setAttachments((prev) => [...prev, saved]);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setUploading(false);
      }
    },
    [sessionId],
  );

  const unstage = async (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
    try {
      await api.deleteAttachment(sessionId, id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      await reload();
    }
  };

  // The event is optional because the acknowledgement card sends the pending
  // message itself, with nothing to suppress.
  const send = async (event?: { preventDefault: () => void }) => {
    event?.preventDefault();
    const text = prompt.trim() || (staged.length ? ATTACHMENTS_ONLY_PROMPT : "");
    // No longer blocked by a turn in flight: the server queues it. Still
    // blocked by an upload in progress, because the attachment ids this claims
    // do not exist until it finishes.
    if (!text || uploading) return;
    setSending(true);
    setError(null);
    try {
      // Asked fresh rather than read off the copy above: the people who can see
      // this session may have changed since it was drawn — a team gaining a
      // member leaves no trace on the session row — and consenting to Bob
      // reading your output is not consenting to Carol.
      const now = await api.exposure(sessionId).catch(() => null);
      if (now) setExposure(now);
      if (now?.needs_acknowledgement) {
        setConfirming(true);
        return;
      }
      await api.prompt(sessionId, text, staged.map((a) => a.id));
      setPrompt("");
      pinnedRef.current = true;
      await reload();
      onChanged();
    } catch (err) {
      // 428 is the server refusing a turn that would expose this user's systems
      // to people they have not been told about. It is the same check as above,
      // enforced where it cannot be skipped — so answer it, don't just report it.
      if (err instanceof ApiError && err.status === 428) {
        setConfirming(true);
        await loadExposure();
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSending(false);
    }
  };

  /**
   * Record the acknowledgement, then send the message that prompted it.
   *
   * The audience on screen goes back with it, so if somebody was added while
   * this card was being read the server refuses and the question is asked again
   * about the larger group rather than being answered about the smaller one.
   */
  const acknowledgeAndSend = async () => {
    if (!exposure) return;
    setAcking(true);
    setError(null);
    try {
      setExposure(
        await api.acknowledgeExposure(sessionId, exposure.viewers.map((v) => v.id)),
      );
      setConfirming(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      await loadExposure();
      return;
    } finally {
      setAcking(false);
    }
    await send();
  };

  /**
   * Stop the agent, and mean it.
   *
   * One call for the whole session rather than a cancel on the running turn:
   * with a queue behind it, killing only the turn in flight lets the next
   * queued message start a second later, and the button would look broken while
   * the agent carried on working. So Stop empties the queue too — and says so
   * first, because those are other people's messages in a shared session.
   */
  const stop = async () => {
    if (!queue.busy) return;
    const waiting = queue.waiting.length;
    if (
      waiting > 0 &&
      !confirm(
        `Stop this turn and discard the ${waiting} message${waiting === 1 ? "" : "s"} ` +
          `queued behind it?\n\n` +
          `The queued message${waiting === 1 ? " has" : "s have"} not been sent to the ` +
          `agent yet, so nothing of ${waiting === 1 ? "its" : "their"} work is lost — ` +
          `but ${waiting === 1 ? "it" : "they"} will not run, and in a shared session ` +
          `${waiting === 1 ? "it" : "some of them"} may not be yours.`,
      )
    )
      return;
    try {
      await api.stopSession(sessionId);
      await reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  /** Take one queued message back out of the line, before anything runs it. */
  const withdraw = async (runId: number) => {
    try {
      await api.withdrawRun(runId);
      await reload();
      onChanged();
    } catch (err) {
      // Most likely a 409: the agent picked it up while the button was being
      // pressed. Re-read so the transcript stops offering to withdraw it.
      setError(err instanceof Error ? err.message : String(err));
      await reload();
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

  /**
   * Hand the conversation to the other CLI.
   *
   * Worth a confirmation because it is not reversible in the way a dropdown
   * implies: the provider session id behind the thread is thrown away, so
   * switching back does not resume what the first agent had either — it is a
   * second handoff. The dialog says what is actually lost rather than asking
   * "are you sure".
   */
  const switchProvider = async (provider: string) => {
    if (!session || provider === session.provider) return;
    const from = providerLabel(session.provider);
    const to = providerLabel(provider);
    const hasHistory = runs.length > 0;
    if (
      hasHistory &&
      !confirm(
        `Hand this conversation to ${to}?\n\n` +
          `${to} cannot read ${from}'s session, so it does not continue this thread — ` +
          `it starts a new one. Its first message is prefixed with a written summary of ` +
          `the transcript so far, assembled by AIOps.\n\n` +
          `Anything ${from} worked out but never said out loud here is lost, and the ` +
          `model, account and preset are reset to whatever ${to} can actually use.`,
      )
    )
      return;
    try {
      setSession(await api.patchSession(sessionId, { provider }));
      await reload();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      // The select is showing the provider the user picked, which is now a lie.
      await reload();
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

  // --- inline autocomplete -------------------------------------------
  //
  // A menu that follows the word the caret is in, rather than a panel behind a
  // button. Everything below is derived from (prompt, caret): there is no
  // "menu is open" flag to get out of step with the text, which is how these
  // end up hanging over a composer that no longer has a token in it.
  // No longer switched off mid-turn: the composer stays live while the agent
  // works, so the menus that make it usable have to stay live with it.
  const token = useMemo(() => activeToken(prompt, caret), [prompt, caret]);
  const tokenKey = token ? `${token.trigger}${token.start}:${token.query}` : null;
  const suggestions = useMemo(
    () => (token ? suggestionsFor(token, capabilities, targets) : []),
    [token, capabilities, targets],
  );
  const menuOpen = token !== null && tokenKey !== dismissed;
  suggestOpenRef.current = menuOpen;

  // Enter sends at the desktop layout and makes a newline on a phone. The rule
  // itself is in `composerKeys.ts`; this is only the width half of it.
  const isDesktop = useIsDesktop();

  // What the box may say for this state. Kept out of the JSX because the honest
  // wording — queued, not steering — is the point of the feature, and a string
  // buried in a ternary is a string nobody tests.
  const composer = composerState(queue, { enterSends: isDesktop, sending });

  // A new token starts at the top. Keyed on the token rather than on the list,
  // so narrowing a search does not silently leave the highlight on row 7 of a
  // list that now has two rows.
  useEffect(() => {
    setActive(0);
  }, [tokenKey]);

  /** Put the caret where the DOM says it is. */
  const syncCaret = (el: HTMLTextAreaElement) => setCaret(el.selectionStart ?? 0);

  /** Replace the live token with a chosen suggestion. */
  const acceptSuggestion = useCallback(
    (choice: Suggestion, at: TokenMatch) => {
      const { text: next, caret: caretAt } = applySuggestion(prompt, at, choice.insert);
      setPrompt(next);
      setCaret(caretAt);
      setDismissed(null);
      // After the state lands, or setSelectionRange runs against the old value
      // and the caret jumps to the end of the previous text.
      window.setTimeout(() => {
        const el = promptRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(caretAt, caretAt);
      }, 0);
    },
    [prompt],
  );

  /**
   * Open the menu from a button rather than from typing.
   *
   * Inserts the trigger character instead of setting a flag, so the button and
   * the keyboard reach the *same* list through the same code path — a separate
   * "picker is open" state was the old design and it could disagree with the
   * text in the box.
   */
  const openTrigger = (trigger: "/" | "@") => {
    const el = promptRef.current;
    const at = el ? (el.selectionStart ?? prompt.length) : prompt.length;
    const end = el ? (el.selectionEnd ?? at) : at;
    // A trigger only counts at the start of a word, so it needs a space in
    // front of it unless there already is one.
    const needsSpace = at > 0 && !/\s/.test(prompt[at - 1]);
    const insert = `${needsSpace ? " " : ""}${trigger}`;
    const next = prompt.slice(0, at) + insert + prompt.slice(end);
    const caretAt = at + insert.length;
    setPrompt(next);
    setCaret(caretAt);
    setDismissed(null);
    window.setTimeout(() => {
      const box = promptRef.current;
      if (!box) return;
      box.focus();
      box.setSelectionRange(caretAt, caretAt);
    }, 0);
  };

  // --- dismissing the ⋯ menu -----------------------------------------
  //
  // On a phone `.chat-tools.open` is absolutely positioned over most of the
  // screen, and until now the only way to put it away was the 42px button that
  // opened it. Escape and a tap outside are what anyone expects of a menu.
  //
  // Share and Files close with it on every path, not just the button's: they
  // are opened from inside this menu, so leaving one up after the menu has gone
  // reads as a stuck window. Both panels render outside the `.chat-tools`
  // element, so they are marked as part of its region rather than counted as
  // "outside".
  const closeTools = useCallback(() => {
    setToolsOpen(false);
    setShareOpen(false);
    setFilesOpen(false);
  }, []);

  useEffect(() => {
    if (!toolsOpen) return;
    const outside = (event: Event) => {
      const el = event.target instanceof Element ? event.target : null;
      if (el?.closest("[data-tools-region]")) return;
      // Deliberately no preventDefault: this is a menu, not a modal. The tap
      // that dismisses it still lands on whatever it was aimed at, so the next
      // tap is not swallowed and the composer does not feel dead.
      closeTools();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // The suggestion list is the more immediate thing and takes Escape first;
      // this menu gets the next one. The textarea's own handler also stops the
      // event, and this is the belt to that pair of braces.
      if (suggestOpenRef.current) return;
      closeTools();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", onKey);
    };
  }, [toolsOpen, closeTools]);

  const efforts = effortChoices(
    providers.find((p) => p.name === session?.provider),
    session?.model ?? null,
  );
  // What the turn will actually run at: a session that never chose falls back
  // to its preset, which is what the runner does when it builds the command.
  const effort =
    session?.effort ?? presets.find((p) => p.id === session?.preset_id)?.effort ?? null;

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
            // Carries the name as well as the hint: the heading truncates, so
            // this is the only place a long one can be read in full.
            title={`${session?.title ?? ""}\nClick to rename`}
            onClick={() => {
              setDraftTitle(session?.title ?? "");
              setRenaming(true);
            }}
          >
            {session?.title ?? "…"}
          </h1>
        )}
        {/* `display: contents` on desktop, so these two wrappers change nothing
            there; on a phone they become their own rows and the second one
            folds away behind the ⋯ button. */}
        {!renaming && (
          <div className="chat-meta">
            {session && (
              <>
                <span className="pill">{session.provider}</span>
                {session.model && <span className="pill">{session.model}</span>}
                {effort && (
                  <span className="pill" title="Reasoning effort for this session">
                    effort {effort}
                  </span>
                )}
                <span className={`pill ${session.status}`}>{session.status}</span>
                {session.team_id !== null && (
                  <span className="pill" title="Everyone in this team can see this session">
                    {teams.find((t) => t.id === session.team_id)?.name ?? "team"}
                  </span>
                )}
              </>
            )}
            <span className={`pill ${connected ? "ok" : "failed"}`}>
              {connected ? "live" : "reconnecting"}
            </span>
          </div>
        )}
        {/* Stopping a running agent is the one thing that must never be a tap
            away behind a menu. */}
        {queue.busy && (
          <button
            className="danger"
            onClick={stop}
            title={
              queue.waiting.length > 0
                ? `Stop the turn in progress and discard the ${queue.waiting.length} ` +
                  "message(s) queued behind it."
                : "Stop the turn in progress."
            }
          >
            Stop
            {queue.waiting.length > 0 && (
              <span className="pill" style={{ marginLeft: 6 }}>
                +{queue.waiting.length}
              </span>
            )}
          </button>
        )}
        {!renaming && (
          <button
            type="button"
            data-tools-region
            className={`icon-btn chat-more${toolsOpen ? " open" : ""}`}
            aria-expanded={toolsOpen}
            aria-haspopup="menu"
            onClick={() => {
              // The panels are opened from inside this menu, so they belong to
              // it: leaving Share up after the menu it was launched from has
              // gone reads as a stuck window rather than a choice. Escape and
              // an outside tap go through `closeTools` for the same reason.
              if (toolsOpen) closeTools();
              else setToolsOpen(true);
            }}
            title="Session settings and actions"
          >
            ⋯
          </button>
        )}
        {!renaming && (
          <div data-tools-region className={`chat-tools${toolsOpen ? " open" : ""}`}>
            {/* Which agent runs this conversation. Dead while anything is
                outstanding: the prompt has already gone to one CLI, and
                switching underneath it would leave the reply attributed to the
                other. Deliberately *not* relaxed alongside the composer — a
                switch throws away the provider session id every queued turn is
                going to resume from, so unlike a message it cannot simply wait
                its turn. The server refuses it with a 409 either way. */}
            {session && providers.length > 0 && (
              <select
                className="approval-select"
                value={session.provider}
                disabled={queue.busy}
                title={
                  queue.busy
                    ? `${providerLabel(session.provider)} is in the middle of a turn` +
                      `${queue.waiting.length > 0 ? `, with ${queue.waiting.length} message(s) queued behind it` : ""}. ` +
                      "Switching agents now would leave that turn attributed to the wrong " +
                      "one and abandon the session every queued message resumes from — " +
                      "stop it or let it finish first."
                    : "Which agent runs this conversation. Switching hands it over rather " +
                      "than continuing it: the new agent cannot read the other's session, " +
                      "so it starts fresh from a written summary of the transcript."
                }
                onChange={(e) => void switchProvider(e.target.value)}
              >
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    Agent: {p.label}
                  </option>
                ))}
              </select>
            )}
            {session && (
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
            {session && efforts.length > 0 && (
              <select
                className="approval-select"
                value={session.effort ?? ""}
                title={EFFORT_HINT}
                onChange={async (e) => {
                  try {
                    setSession(
                      await api.patchSession(sessionId, { effort: e.target.value || null }),
                    );
                  } catch (err) {
                    setError(err instanceof Error ? err.message : String(err));
                  }
                }}
              >
                <option value="">Default effort</option>
                {efforts.map((level) => (
                  <option key={level} value={level}>
                    Effort: {level}
                  </option>
                ))}
              </select>
            )}
            <button
              type="button"
              onClick={() => setFilesOpen((v) => !v)}
              title="Files in this session's working directory"
            >
              Files
            </button>
            {session && (
              <button
                type="button"
                onClick={() => setShareOpen((v) => !v)}
                title="Who else can see this session"
              >
                Share
              </button>
            )}
            {/* Deleting is the owner's call: a session shared into a team is
                other people's work too. */}
            {session && (me.is_admin || session.owner_id === me.id) && (
              <button className="danger" onClick={remove} title="Delete this session">
                Delete
              </button>
            )}
          </div>
        )}
      </div>

      {shareOpen && session && (
        <Sharing
          session={session}
          me={me}
          teams={teams}
          users={directory}
          onSaved={(updated) => {
            setSession(updated);
            onChanged();
          }}
        />
      )}

      <div className="chat-body" ref={bodyRef} onScroll={onScroll}>
        {error && <div className="error-banner">{error}</div>}
        {runs.length === 0 && <div className="empty">Send the first task below.</div>}
        {runs.map((run) => (
          <div key={run.id} style={{ display: "contents" }}>
            {/* A message nobody has read yet is drawn as one: dashed and dimmed
                rather than the solid bubble a sent turn gets. The difference is
                not decoration — the agent genuinely has not seen it, and a
                queued message that looks identical to a delivered one is the
                whole misunderstanding this feature could create. */}
            <div className={`msg prompt${queuedIds.has(run.id) ? " queued" : ""}`}>
              <div className="who">
                {/* Who actually sent it, read off the run. A session can be
                    shared, so "you" was wrong for every turn somebody else
                    typed — and a scheduled turn is attributed to the schedule's
                    owner, which is whose credentials it ran with. */}
                <span
                  className={
                    run.requested_by_id === null ? "turn-who unattributed" : "turn-who"
                  }
                  title={
                    run.requested_by_id === null
                      ? "This turn predates AIOps recording who sent each message, and " +
                        "the record was never reconstructed. It is not necessarily yours."
                      : run.requested_by_id === me.id
                        ? "You sent this turn."
                        : `Sent by ${fullName(
                            directory.find((u) => u.id === run.requested_by_id),
                          )}.`
                  }
                >
                  {turnAuthor(run, me, directory)}
                </span>
                {run.schedule_id ? " (scheduled)" : ""}
                {/* Per turn, not per session: a switched conversation is a
                    mixed one, and reading this off the session would relabel
                    every earlier turn as the agent selected now. */}
                {run.provider && (
                  <span
                    className="turn-agent"
                    title={
                      `This turn was answered by ${providerLabel(run.provider)}` +
                      `${run.model ? ` (${run.model})` : ""}. Other turns in this ` +
                      "conversation may have been answered by a different agent."
                    }
                  >
                    {" → "}
                    {run.provider}
                    {run.model ? ` · ${run.model}` : ""}
                  </span>
                )}
              </div>
              <pre>{run.prompt}</pre>
              {(attachmentsByRun.get(run.id) ?? []).length > 0 && (
                <div className="attach-strip sent">
                  {(attachmentsByRun.get(run.id) ?? []).map((a) => (
                    <AttachmentTile key={a.id} sessionId={sessionId} attachment={a} />
                  ))}
                </div>
              )}
              {/* Says what is true of it rather than "pending": that it has not
                  been delivered, where it sits in the line, and that it is
                  still take-back-able. Files queue with it — the run already
                  owns them, so they go out with the message when its turn
                  comes. */}
              {canWithdraw(run, queue) && (
                <div className="queued-foot">
                  <span className="pill queued">
                    queued · {position(run, queue.waiting)}
                  </span>
                  <span className="queued-note">
                    Not delivered. It runs as its own turn once the agent finishes the
                    work above — it cannot change what that turn is doing.
                  </span>
                  <button
                    type="button"
                    className="queued-undo"
                    onClick={() => void withdraw(run.id)}
                    title="Take this message back out of the queue"
                  >
                    Withdraw
                  </button>
                </div>
              )}
            </div>

            {/* The turn that carried the handoff. Said here rather than only in
                the header, because it is the reason this reply may read as
                slightly out of step with the one above it. */}
            {run.carries_handoff && (
              <div className="msg system">
                {providerLabel(run.provider)} received this message with a written summary of
                the earlier turns — it could not read them itself.
              </div>
            )}

            {/* A silent failover looks like the first account simply worked.
                Say which one actually answered. */}
            {run.failed_over_from_id !== null && (
              <div className="msg system">
                {accountName(run.failed_over_from_id)} hit its limit — switched to{" "}
                {accountName(run.account_id)}.
              </div>
            )}

            {buildRows(eventsByRun.get(run.id) ?? [], {
              live: run.status === "running",
            }).map((row) =>
              row.type === "event" ? (
                <Bubble key={eventKey(row.event)} event={row.event} />
              ) : row.type === "outcome" ? (
                /* Not a message — the CLI's own one-word account of a turn that
                   did not end with a reply. Kept because for an interrupted run
                   it is the only thing the agent said about it. */
                <div key={row.key} className={`msg system outcome${row.error ? " bad" : ""}`}>
                  the agent's turn ended: {row.text}
                </div>
              ) : (
                <SubagentGroup key={`sub:${row.parentId}:${eventKey(row.steps[0])}`} row={row} />
              ),
            )}

            {live[run.id] && (
              <div className="msg assistant live">
                <div className="who">assistant · streaming</div>
                <pre>{live[run.id]}</pre>
              </div>
            )}

            {/* What the agent is doing right now, in place of the spinner that
                said only that something was. */}
            {run.status === "running" && (
              <WorkingStrip
                run={run}
                events={eventsByRun.get(run.id) ?? []}
                streaming={Boolean(live[run.id])}
              />
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

      {decisions.map((d) => (
        <div key={d.id} className="msg system approval-decision" role="status">
          {d.text}
        </div>
      ))}

      {filesOpen && <FilesPanel sessionId={sessionId} onClose={() => setFilesOpen(false)} />}

      {/* Above the composer for the same reason an approval card is: it is a
          fact about the message about to be sent. Drawn whenever there is
          anything to say, and only then — a warning shown to somebody with no
          stored systems, or in a session nobody else can read, teaches people to
          click past the one that matters. */}
      {exposure?.at_stake && (
        <ExposureNotice
          exposure={exposure}
          confirming={confirming}
          busy={acking || sending}
          onAgree={() => void acknowledgeAndSend()}
          onDefer={() => setConfirming(false)}
        />
      )}

      {/* Sits against the composer because it is a fact about the message about
          to be sent, and it has to be honest rather than reassuring: the agent
          reading it did not have this conversation. */}
      {session?.handoff_pending && (
        <div className="handoff-note">
          <strong>
            {providerLabel(session.provider)} is picking up this conversation from a summary.
          </strong>{" "}
          It has not seen the messages above and cannot — that history belongs to the other
          agent's session. Your next message goes out with a written briefing of the
          transcript so far, once; after that this is an ordinary conversation with{" "}
          {providerLabel(session.provider)}.
        </div>
      )}

      <form
        className={`chat-foot${dragging ? " dropping" : ""}`}
        onSubmit={send}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          // Moving between children fires dragleave on the parent too; only the
          // pointer actually leaving the form should clear the highlight.
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void stage(Array.from(e.dataTransfer.files));
        }}
      >
        {menuOpen && token && (
          <Suggestions
            trigger={token.trigger}
            items={suggestions}
            hint={emptyHint(token, capabilities, targets)}
            active={active}
            onHover={setActive}
            onPick={(choice) => acceptSuggestion(choice, token)}
          />
        )}
        {(staged.length > 0 || uploading) && (
          <div className="attach-strip">
            {staged.map((a) => (
              <AttachmentTile
                key={a.id}
                sessionId={sessionId}
                attachment={a}
                onRemove={() => void unstage(a.id)}
              />
            ))}
            {uploading && <span className="attach-status">Uploading…</span>}
          </div>
        )}
        {/* The one thing this feature must not let the interface imply. An
            enabled box beside a working agent reads as "it is listening", and
            it is not: both CLIs are one headless process per turn with nothing
            on stdin, so a message sent now becomes the next turn and cannot
            touch this one. Said in the composer, where the misunderstanding
            would happen, rather than in a tooltip. */}
        {composer.notice && (
          <div className="queue-notice" role="status">
            {composer.notice}
          </div>
        )}
        <textarea
          ref={promptRef}
          rows={3}
          value={prompt}
          placeholder={composer.placeholder}
          // It is a combobox while the menu is up: a text box that owns a list
          // somebody can arrow through. `aria-expanded` is the part screen
          // readers use to say so, and it has to be present on the input
          // itself, not on the list.
          role="combobox"
          aria-expanded={menuOpen}
          aria-controls={menuOpen && suggestions.length > 0 ? LISTBOX_ID : undefined}
          aria-autocomplete="list"
          aria-activedescendant={
            menuOpen && suggestions.length > 0 ? optionId(active) : undefined
          }
          onChange={(e) => {
            setPrompt(e.target.value);
            syncCaret(e.target);
          }}
          // Fires for caret moves as well as selections, which is what keeps
          // the menu attached to the word the caret is actually in after an
          // arrow key or a click into the middle of the text.
          onSelect={(e) => syncCaret(e.currentTarget)}
          onKeyDown={(e) => {
            // The whole rule — which modifier does what, and what changes when
            // the suggestion list is up or the window is phone-sized — lives in
            // `composerKeys.ts` and is tested there. This switch is only the
            // wiring: preventDefault, and the state each answer implies.
            switch (
              composerKeyAction(e, {
                menuOpen,
                suggestionCount: suggestions.length,
                enterSends: isDesktop,
              })
            ) {
              case "send":
                // `send` calls preventDefault itself, so the Enter that sent
                // the message never also lands a newline in the box.
                void send(e);
                return;
              case "dismiss":
                // Taken here so the ⋯ menu's document-level handler does not
                // also see it. The list is the more immediate thing; the menu,
                // if it is open too, gets the next Escape.
                e.preventDefault();
                e.stopPropagation();
                setDismissed(tokenKey);
                return;
              case "accept":
                e.preventDefault();
                if (token) acceptSuggestion(suggestions[active], token);
                return;
              case "next":
                e.preventDefault();
                setActive((i) => (i + 1) % suggestions.length);
                return;
              case "prev":
                e.preventDefault();
                setActive((i) => (i - 1 + suggestions.length) % suggestions.length);
                return;
              case "pass":
                return;
            }
          }}
          onPaste={(e) => {
            // A screenshot on the clipboard arrives here as a file with no name
            // and no other representation — this is the only place to catch it.
            const pasted = e.clipboardData ? Array.from(e.clipboardData.files) : [];
            if (pasted.length === 0) return;
            e.preventDefault();
            void stage(pasted);
          }}
        />
        <div className="row" style={{ marginTop: 8 }}>
          {/* Not disabled by a turn in flight any more — that is the feature.
              Still disabled while an upload is running, because the ids this
              would claim do not exist yet. */}
          <button
            className="primary"
            type="submit"
            disabled={sending || uploading || (!prompt.trim() && staged.length === 0)}
            title={
              queue.busy
                ? "Queue this as the next turn. The agent is mid-turn and will not " +
                  "see it until that one ends."
                : undefined
            }
          >
            {composer.sendLabel}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              void stage(e.target.files ? Array.from(e.target.files) : []);
              // Without this, re-picking the same file fires no change event.
              e.target.value = "";
            }}
          />
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            title="Attach files or images to the next message"
          >
            📎 Attach
            {staged.length > 0 && (
              <span className="pill" style={{ marginLeft: 6 }}>{staged.length}</span>
            )}
          </button>
          {/* Kept because the inline menu is invisible until you know to type
              the character. Both buttons insert the trigger and let the same
              code path open the same list — there is no second picker. */}
          <button
            type="button"
            onClick={() => openTrigger("/")}
            title="Insert a skill or slash command — or just type / in the box"
          >
            / Skills
            {capabilities.length > 0 && (
              <span className="pill" style={{ marginLeft: 6 }}>{capabilities.length}</span>
            )}
          </button>
          <button
            type="button"
            onClick={() => openTrigger("@")}
            title="Insert a stored system by name — or just type @ in the box"
          >
            @ Systems
            {targets.length > 0 && (
              <span className="pill" style={{ marginLeft: 6 }}>{targets.length}</span>
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

/**
 * The list the composer's `/` and `@` menus draw.
 *
 * A listbox owned by the textarea, not a dialog: focus never leaves the box, so
 * the options are `<li role="option">` rather than buttons, and a pointer press
 * is cancelled before it can move focus.
 */
function Suggestions({
  trigger,
  items,
  hint,
  active,
  onPick,
  onHover,
}: {
  trigger: "/" | "@";
  items: Suggestion[];
  hint: string;
  active: number;
  onPick: (choice: Suggestion) => void;
  onHover: (index: number) => void;
}) {
  const listRef = useRef<HTMLUListElement>(null);

  // Arrowing past the bottom of a scrolled list must bring the row into view;
  // `aria-activedescendant` moves the accessibility cursor but not the pixels.
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`#${CSS.escape(optionId(active))}`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active, items.length]);

  const label = trigger === "/" ? "Skills & commands" : "Stored systems";

  return (
    <div className="picker suggest">
      <div className="picker-head">
        <strong>{label}</strong>
        <span className="picker-keys">↑↓ move · Enter or Tab insert · Esc close</span>
      </div>
      {items.length === 0 ? (
        // Never a silent empty box: a trigger that matches nothing has to say
        // whether the list is empty or the word is wrong, because those want
        // opposite responses from the reader.
        <div className="picker-empty" role="status">
          {hint}
        </div>
      ) : (
        <ul id={LISTBOX_ID} role="listbox" aria-label={label} ref={listRef}>
          {items.map((s, i) => (
            <li
              key={s.key}
              id={optionId(i)}
              role="option"
              aria-selected={i === active}
              className={`suggest-row${i === active ? " active" : ""}`}
              // Two handlers rather than one on pointerdown. mousedown is
              // cancelled purely to keep focus — and the caret — in the
              // textarea we are about to write into; the choice itself is made
              // on click. Picking on pointerdown instead would fire the moment
              // a finger touched a row, so a drag meant to scroll a long list
              // would insert whatever it started on. click is synthesised from
              // a tap, so this stays one code path for mouse and touch.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => onPick(s)}
              onMouseMove={() => onHover(i)}
            >
              <span className="cap-name">{s.label}</span>
              {s.badge && <span className="pill">{s.badge}</span>}
              {s.source && <span className="cap-src">{s.source}</span>}
              {s.detail && <span className="cap-desc">{s.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** "alice", "alice and bob", "alice, bob and carol" — by display name. */
function names(people: UserSummary[]): string {
  // Sorted on what is shown, so the sentence reads in the order it is drawn.
  const list = people.map(displayName).sort();
  if (list.length === 0) return "nobody else";
  if (list.length === 1) return list[0];
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

/**
 * Who is going to read what your stored systems produce in this session.
 *
 * A turn reaches the systems the person who *asked for it* can reach, not the
 * session owner's — so bringing your own systems into somebody else's
 * conversation is allowed, and stays allowed. This is not a gate on that: it is
 * the disclosure that it is happening, which is the part a shared transcript
 * makes non-obvious.
 *
 * The three consequences are spelled out rather than summarised as "this session
 * is shared", because they are not the same risk and only the first is the one
 * people assume. The third is the one nobody thinks of: the other readers can
 * *write* here too, and their messages are context for your next turn, so an
 * instruction they left in the thread can be carried out holding your
 * credentials. That is a confused deputy, not an information leak — they do not
 * have to wait for the key to be printed, they can ask for the host to be used.
 *
 * Names, always. "This session is shared" is not actionable; "bob can read this,
 * and example-prod-sb is reachable from it" is.
 */
function ExposureNotice({
  exposure,
  confirming,
  busy,
  onAgree,
  onDefer,
}: {
  exposure: Exposure;
  confirming: boolean;
  busy: boolean;
  onAgree: () => void;
  onDefer: () => void;
}) {
  const readers = names(exposure.viewers);
  const added = names(exposure.new_viewers);
  const systems = exposure.systems.map((s) => s.slug).join(", ");
  const plural = exposure.new_viewers.length === 1 ? "was" : "were";
  // Whether one person or five, they are "they" — the alternative is guessing at
  // a stranger's pronoun in a security warning.
  const rearmed = exposure.acknowledged_at
    ? `${added} ${plural} added since you agreed to this, so you will be asked to confirm once more before your next message.`
    : "You will be asked to confirm this once before your next message.";

  // Collapsed once it has been agreed to, and remembered per session: this is a
  // standing fact about the conversation, not news, and a wall of text that
  // cannot be put away is a wall of text people stop reading. It cannot be
  // collapsed while confirming — that is the one moment the detail is the point.
  const storageKey = `aiops.exposure.collapsed.${exposure.session_id}`;
  const [collapsed, setCollapsed] = useState(() => {
    if (exposure.needs_acknowledgement) return false;
    try {
      return window.localStorage.getItem(storageKey) !== "0";
    } catch {
      return true; // private browsing; default to out of the way
    }
  });

  const setAndRemember = (next: boolean) => {
    setCollapsed(next);
    try {
      window.localStorage.setItem(storageKey, next ? "1" : "0");
    } catch {
      /* nothing to remember it with */
    }
  };

  // Agreeing is the moment it becomes old news, so it folds away then rather
  // than at the next page load.
  //
  // Genuinely on the transition, via a ref: an effect with these deps also runs
  // after the first commit, and since `acknowledged` is already true by then for
  // anyone who has agreed, a plain condition would re-collapse on every mount —
  // overwriting a remembered "I expanded this" with "collapsed" and making the
  // stored preference write-only.
  const wasAcknowledged = useRef(exposure.acknowledged);
  useEffect(() => {
    const justAgreed = exposure.acknowledged && !wasAcknowledged.current;
    wasAcknowledged.current = exposure.acknowledged;
    if (justAgreed && !exposure.needs_acknowledgement) setAndRemember(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exposure.acknowledged, exposure.needs_acknowledgement]);

  // A re-arm forces it back open whatever was remembered: somebody new can read
  // this now, which is exactly when it stops being old news.
  if (collapsed && !confirming && !exposure.needs_acknowledgement) {
    return (
      <button
        type="button"
        className="exposure-collapsed"
        onClick={() => setAndRemember(false)}
        aria-expanded={false}
        title="What this exposes, and to whom"
      >
        <span className="pill warn">shared</span>
        {readers} can read anything your systems produce here.
        <span className="exposure-more">Details</span>
      </button>
    );
  }

  return (
    <div className={`exposure-note${confirming ? " confirming" : ""}`}>
      <strong>
        {/* A flex row, not a float: the heading wraps as soon as two people can
            read the session, and a right float cannot rise above the line box
            it occurs in — so the ✕ ended up beside the last line. */}
        <span>
          {confirming
            ? `Use your stored systems in a session ${readers} can read?`
            : `${readers} can read this session, and your stored systems are reachable from it.`}
        </span>
        {/* Only when collapsing would actually do something. While a re-arm is
            pending the guard below re-expands regardless, so offering the
            control there is offering a button that does nothing. */}
        {!confirming && !exposure.needs_acknowledgement && (
          <button
            type="button"
            className="exposure-hide"
            onClick={() => setAndRemember(true)}
            aria-label="Collapse this warning"
            aria-expanded={true}
            title="Collapse it"
          >
            ✕
          </button>
        )}
      </strong>
      <p>
        Any turn you send here runs with your systems available to the agent:{" "}
        <span className="mono">{systems}</span>. {readers} cannot reach{" "}
        {exposure.systems.length === 1 ? "it" : "them"} directly, but{" "}
        {confirming ? "by continuing you accept that" : "through this session"}:
      </p>
      <ul>
        <li>
          everything the agent does on those hosts is written into this transcript —
          command output, file contents, whatever is on the far end — and they can
          read all of it;
        </li>
        <li>
          the private key is a real file on disk for the length of each turn, so if
          the agent is asked to print it, the key itself lands in the transcript
          where they can read it;
        </li>
        <li>
          they can type in here too, and their messages are context for your next
          turn — so an instruction one of them leaves in this conversation can be
          carried out by the agent <em>using your credentials</em>, without you
          asking for it.
        </li>
      </ul>
      {confirming ? (
        <>
          <p className="exposure-fine">
            This is recorded against your name and this session, and the transcript
            notes it whenever a turn actually uses those systems. You will be asked
            again if anyone else is given access. Saying no sends nothing and changes
            nothing else — your systems stay yours either way.
          </p>
          <div className="row exposure-actions">
            <button className="primary" type="button" disabled={busy} onClick={onAgree}>
              {busy ? "Sending…" : "I understand — send it"}
            </button>
            <button type="button" disabled={busy} onClick={onDefer}>
              Not now
            </button>
          </div>
        </>
      ) : (
        <p className="exposure-fine">
          Nothing is restricted by this — a turn here still reaches everything you
          can reach.{" "}
          {exposure.needs_acknowledgement
            ? rearmed
            : "You confirmed this; the transcript records each turn that uses them."}
        </p>
      )}
    </div>
  );
}

/**
 * One attached file. Images show themselves — a screenshot is unusable as a
 * filename — and everything else gets a chip.
 */
function AttachmentTile({
  sessionId,
  attachment,
  onRemove,
}: {
  sessionId: string;
  attachment: Attachment;
  onRemove?: () => void;
}) {
  const href = api.attachmentUrl(sessionId, attachment.id);
  const isImage = attachment.content_type.startsWith("image/");
  return (
    <div className={`attach-tile${isImage ? " image" : ""}`}>
      <a href={href} target="_blank" rel="noreferrer" title={attachment.filename}>
        {isImage ? (
          <img className="attach-thumb" src={href} alt={attachment.filename} />
        ) : (
          <span className="attach-icon">▤</span>
        )}
      </a>
      <span className="attach-meta">
        <a href={href} className="attach-name" title={attachment.filename}>
          {attachment.filename}
        </a>
        <span className="attach-size">{formatSize(attachment.size)}</span>
      </span>
      {onRemove && (
        <button type="button" className="attach-remove" onClick={onRemove} title="Remove">
          ×
        </button>
      )}
    </div>
  );
}

/**
 * What the agent left behind, for downloading.
 *
 * The listing is deliberately bounded — a workspace is usually a repo with a
 * build tree in it — so the rule is printed rather than applied silently.
 */
function FilesPanel({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const [data, setData] = useState<SessionFiles | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await api.sessionFiles(sessionId));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="files-panel" data-tools-region>
      <div className="files-head">
        <strong>Files</strong>
        <span className="files-root mono">{data?.root ?? "…"}</span>
        <button type="button" onClick={() => void load()} disabled={loading}>
          {loading ? "Reading…" : "Refresh"}
        </button>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {data && (
        <div className="files-rule">
          Newest first · up to {data.max_files} files, {data.max_depth} levels deep · .git,
          node_modules and cache directories skipped
          {data.truncated && " · more files exist than are listed here"}
        </div>
      )}
      {data && data.files.length === 0 && !error && (
        <div className="empty" style={{ padding: 14 }}>
          Nothing here yet.
        </div>
      )}
      {data && data.files.length > 0 && (
        <ul className="files-list">
          {data.files.map((f) => (
            <li key={f.path}>
              <a href={api.sessionFileUrl(sessionId, f.path)} className="mono">
                {f.path}
              </a>
              <span className="files-size">{formatSize(f.size)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Who else can see this conversation.
 *
 * Read-only unless you own it: a session you were let into is not yours to pass
 * on. Whoever can see it can answer its approvals, so this panel says so — the
 * list here is the list of people who can let this agent run a command.
 */
function Sharing({
  session,
  me,
  teams,
  users,
  onSaved,
}: {
  session: Session;
  me: User;
  teams: Team[];
  users: UserSummary[];
  onSaved: (session: Session) => void;
}) {
  const [teamId, setTeamId] = useState(session.team_id === null ? "" : String(session.team_id));
  const [shared, setShared] = useState<number[]>(session.shared_user_ids);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mine = me.is_admin || session.owner_id === me.id;
  const nameOf = (id: number | null) =>
    id === null ? "nobody" : nameById(users, id, `user ${id}`);

  const save = async (changes: Partial<Session>) => {
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.patchSession(session.id, changes));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (userId: number) => {
    const next = shared.includes(userId)
      ? shared.filter((id) => id !== userId)
      : [...shared, userId];
    setShared(next);
    void save({ shared_user_ids: next });
  };

  return (
    // Opened from the ⋯ menu, so it counts as part of it: a tap in here must
    // not read as a tap outside the menu and put both away.
    <div className="session-share" data-tools-region>
      {error && <div className="error-banner">{error}</div>}
      <fieldset className="sharing">
        <legend>Who can see this session</legend>
        <p className="hint">
          Owned by {nameOf(session.owner_id)}
          {mine ? "" : " — only they can change who else is in"}. Everyone listed here sees
          the transcript and can answer the prompts a paused agent is waiting on.
        </p>
        {/* Said to the person doing the adding, at the moment they add. The
            other half of this warning is above the composer, addressed to
            whoever's credentials are at stake; this half is the fact that
            letting someone in is what puts them in a position to read it. */}
        {mine && (
          <p className="hint">
            Anyone you add can read anything <em>other</em> members' stored systems
            produce here, and can leave instructions the agent carries out with those
            members' credentials on their next turn. Members are warned before their
            systems are used, and asked again whenever you add someone.
          </p>
        )}

        {mine ? (
          <>
            <label className="row share-row">
              <span style={{ margin: 0, flex: 1 }}>Team</span>
              <select
                value={teamId}
                disabled={busy}
                onChange={(e) => {
                  setTeamId(e.target.value);
                  void save({ team_id: e.target.value ? Number(e.target.value) : null });
                }}
              >
                <option value="">No team</option>
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>

            <div className="share-list">
              {users
                .filter((u) => u.id !== session.owner_id)
                .map((u) => (
                  <label key={u.id} className="row share-row">
                    {/* Both names, because letting somebody in is a decision
                        with a consequence and display names are not unique —
                        two people called "Walt" must be tellable apart here. */}
                    <span style={{ margin: 0, flex: 1 }}>{fullName(u)}</span>
                    <input
                      type="checkbox"
                      checked={shared.includes(u.id)}
                      disabled={busy}
                      onChange={() => toggle(u.id)}
                    />
                  </label>
                ))}
            </div>

            <label className="row share-row">
              <span style={{ margin: 0, flex: 1 }}>Hand it over to</span>
              <select
                value=""
                disabled={busy}
                onChange={(e) => {
                  const heir = Number(e.target.value);
                  if (!heir) return;
                  if (
                    !confirm(
                      `Give this session to ${nameOf(heir)}? They decide who can see it ` +
                        "from then on, and you keep access only if they leave you on the list.",
                    )
                  )
                    return;
                  void save({ owner_id: heir });
                }}
              >
                <option value="">Keep it</option>
                {users
                  .filter((u) => u.id !== session.owner_id)
                  .map((u) => (
                    <option key={u.id} value={u.id}>
                      {fullName(u)}
                    </option>
                  ))}
              </select>
            </label>
          </>
        ) : (
          <p className="hint">
            {session.team_id !== null
              ? `Shared through the team ${teams.find((t) => t.id === session.team_id)?.name ?? ""}.`
              : "Shared with you directly."}
            {session.shared_user_ids.length > 0 &&
              ` Also shared with ${session.shared_user_ids
                .map(nameOf)
                .sort()
                .join(", ")}.`}
          </p>
        )}
      </fieldset>
    </div>
  );
}

/**
 * Work happening under one tool call, live while it is happening.
 *
 * The header carries what this one is currently doing rather than only a step
 * count, because that is the question being asked of it — a count answers "is
 * it stuck" but never "on what". While several run at once each gets its own
 * header, so two concurrent tasks read as two things, not as one busy blur.
 *
 * Collapsed by default once finished: the steps are worth having and not worth
 * the screen. A running one opens itself.
 */
function SubagentGroup({ row }: { row: SubagentRow }) {
  const [open, setOpen] = useState<boolean | null>(null);
  const steps = row.steps.length;
  // A running one opens itself so you can watch it — unless its only step is
  // the description already printed in the header, which is the shape most of
  // them have and which would otherwise print itself twice.
  const shown = open ?? (row.running && steps > 1);
  return (
    <div className={`subagent${row.running ? " running" : ""}`}>
      <button
        className="subagent-head"
        onClick={() => setOpen(!shown)}
        aria-expanded={shown}
      >
        <span className="subagent-caret">{shown ? "▾" : "▸"}</span>
        {/* The CLI names a subagent it spawned; it does not name the task it
            narrates around an ordinary tool call, and calling that one a
            "subagent" claimed something untrue about it. */}
        <span className="subagent-name">{row.name || "background task"}</span>
        {row.running && <span className="live-dot" aria-hidden="true" />}
        <span className="subagent-meta">
          {row.activity && <span className="subagent-doing">{row.activity}</span>}
          <span className="subagent-count">
            {steps} step{steps === 1 ? "" : "s"}
            {row.tools > 0 && ` · ${row.tools} tool call${row.tools === 1 ? "" : "s"}`}
            {row.running && " · running"}
          </span>
        </span>
      </button>
      {shown && (
        <div className="subagent-body">
          {row.steps.map((e) => (
            <Bubble key={eventKey(e)} event={e} />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The turn in flight, said out loud.
 *
 * A spinner answers "is anything happening"; the question people actually have
 * of a long turn is "on what, and for how long". Both are readable off events
 * that were already arriving — the tool call it is on, the count of the ones
 * before it — so this costs no new plumbing, only the clock.
 *
 * The clock ticks once a second and only while a turn is running, so an idle
 * session re-renders nothing.
 */
function WorkingStrip({
  run,
  events,
  streaming,
}: {
  run: Run;
  events: ChatEvent[];
  streaming: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const progress = turnProgress(events, { streaming });
  // `started_at` is null for the moment between dispatch and spawn.
  const since = run.started_at ? parseUtc(run.started_at).getTime() : null;
  const age = since !== null && !Number.isNaN(since) ? elapsed(since, now) : null;

  return (
    <div className="working" role="status">
      <div className="working-head">
        <span className="live-dot" aria-hidden="true" />
        <span className="working-doing">{progress.doing}</span>
        {age && <span className="working-time">{age}</span>}
      </div>
      {progress.detail && <pre className="working-detail">{progress.detail}</pre>}
      <div className="working-meta">
        {progress.steps} step{progress.steps === 1 ? "" : "s"}
        {progress.tools > 0 && ` · ${progress.tools} tool call${progress.tools === 1 ? "" : "s"}`}
      </div>
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

  // An agent that is *asking* rather than requesting permission gets a form, not
  // two buttons. Accepting the generic card here told the model "the user did
  // not answer the questions", which is the whole reason this branch exists.
  if (approval.questions.length > 0) {
    return <QuestionCard approval={approval} onDone={onDone} />;
  }

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

/**
 * The agent's own questions, as a form.
 *
 * Claude's AskUserQuestion tool comes down the permission pipe like anything
 * else, so it used to arrive as Accept/Deny over an invisible question. Every
 * option's *description* is rendered, not just its label: in a real example
 * ("Device HDR", four playback options) the labels were near-interchangeable
 * and the descriptions carried the entire decision.
 *
 * Declining is still a plain deny, and still works — the agent is told nobody
 * answered rather than being left parked.
 */
function QuestionCard({
  approval,
  onDone,
}: {
  approval: Approval;
  onDone: (id: number) => void;
}) {
  const questions = approval.questions;
  const [draft, setDraft] = useState<Draft>(() => emptyDraft(questions));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only after a failed submit, so a form nobody has touched yet is not already
  // scolding them about the questions they have not reached.
  const [tried, setTried] = useState(false);

  const problem = validate(questions, draft);

  const send = async () => {
    setTried(true);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.decideApproval(approval.id, true, null, toAnswers(questions, draft));
      onDone(approval.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const decline = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.decideApproval(
        approval.id,
        false,
        "The operator did not answer these questions. Carry on without them, or stop and "
          + "say what you still need to know.",
      );
      onDone(approval.id);
    } catch (err) {
      // A 409 means somebody else answered, or the run ended; either way this
      // card is stale, so it says why and then goes.
      setError(err instanceof Error ? err.message : String(err));
      window.setTimeout(() => onDone(approval.id), 2500);
      setBusy(false);
    }
  };

  return (
    <div className="approval approval-question">
      <div className="approval-head">
        <span className="pill warn">needs an answer</span>
        <strong>{questions.length === 1 ? "The agent is asking" : `${questions.length} questions`}</strong>
        <span className="approval-provider">{approval.provider}</span>
      </div>

      {questions.map((question) => (
        <QuestionBlock
          key={question.question}
          question={question}
          draft={draft}
          disabled={busy}
          missing={tried && !isAnswered(draft, question)}
          onToggle={(label) => setDraft((d) => toggleOption(d, question, label))}
          onOther={(text) => setDraft((d) => setOther(d, question, text))}
        />
      ))}

      {error && <div className="error-banner">{error}</div>}
      <div className="row approval-actions">
        <button className="primary" type="button" disabled={busy} onClick={send}>
          Send answers
        </button>
        <button type="button" disabled={busy} onClick={decline}>
          Don’t answer
        </button>
        <span className="approval-hint">The agent is waiting.</span>
      </div>
    </div>
  );
}

/** One question: its heading, its options with their descriptions, and Other. */
function QuestionBlock({
  question,
  draft,
  disabled,
  missing,
  onToggle,
  onOther,
}: {
  question: ApprovalQuestion;
  draft: Draft;
  disabled: boolean;
  missing: boolean;
  onToggle: (label: string) => void;
  onOther: (text: string) => void;
}) {
  const entry = draft[question.question] ?? { chosen: [], other: "" };
  // Native radios need a name to group by, and the question's own text is the
  // only identifier it has. Escaped so a question containing quotes cannot
  // collide with another.
  const group = `q-${encodeURIComponent(question.question)}`;

  return (
    <fieldset className={`question${missing ? " missing" : ""}`}>
      <legend>
        {question.header && <span className="question-header">{question.header}</span>}
        <span className="question-text">{question.question}</span>
        {question.multi_select && <span className="question-hint">choose any</span>}
      </legend>

      {question.options.map((option) => (
        <label className="question-option" key={option.label}>
          <input
            type={question.multi_select ? "checkbox" : "radio"}
            name={group}
            disabled={disabled}
            checked={entry.chosen.includes(option.label)}
            onChange={() => onToggle(option.label)}
          />
          <span>
            <span className="option-label">{option.label}</span>
            {option.description && (
              <span className="option-description">{option.description}</span>
            )}
          </span>
        </label>
      ))}

      <label className="question-other">
        <span className="option-label">Something else</span>
        <input
          type="text"
          value={entry.other}
          disabled={disabled}
          placeholder="Answer in your own words"
          onChange={(e) => onOther(e.target.value)}
        />
      </label>
    </fieldset>
  );
}

function Bubble({ event }: { event: ChatEvent }) {
  const label =
    event.kind === "tool_use"
      ? `tool · ${event.tool_name ?? "?"}`
      : event.kind === "tool_result"
        ? "tool result"
        : event.kind;

  // Written by AIOps, not by an agent: the point in the transcript where the
  // conversation changed hands. Drawn as a break across the thread rather than
  // another grey system line, because everything below it was produced by a
  // different agent that never saw anything above it.
  if (event.kind === "provider_switch") {
    return (
      <div className="msg handoff">
        <div className="handoff-rule">
          <span>⇄ handed over</span>
        </div>
        <pre>{event.text}</pre>
      </div>
    );
  }

  if (event.kind === "system") {
    return <div className="msg system">{event.text}</div>;
  }

  // Only machine output folds away. A long *reply* is the thing the reader
  // opened the session for, and putting it behind a "…(3848 chars)" summary
  // made the answer the one part of the turn you had to go looking for.
  const foldable = event.kind === "tool_use" || event.kind === "tool_result" || event.kind === "user";
  const long = foldable && (event.text?.length ?? 0) > 1200;
  // What the agent's browser photographed, drawn where it took it. The result
  // line stays as well: it is what the agent was told, and a capture that was
  // never kept would otherwise leave nothing behind.
  const shots = shotsIn(event.kind, event.text);
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
      <ScreenshotStrip runId={event.run_id} shots={shots} />
    </div>
  );
}
