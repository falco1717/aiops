import type * as React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api, openSocket } from "../api";
import { EFFORT_HINT, effortChoices } from "../effort";
import { displayName, fullName, nameById } from "../names";
import type { Suggestion, TokenMatch } from "../mentions";
import { activeToken, applySuggestion, emptyHint, suggestionsFor } from "../mentions";
import { composerKeyAction } from "../composerKeys";
import type {
  Account,
  AgentEvent,
  Approval,
  ApprovalMode,
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
          // Whose answer it was. In a shared session the card can vanish under
          // somebody who was reading it, and "it disappeared" is not an answer
          // to "who let the agent run that". Skipped for your own decisions,
          // which you just made, and for expiries, which nobody made.
          if (msg.decided_by && msg.decided_by_id !== me.id) {
            setDecisions((prev) => [
              ...prev.filter((d) => d.id !== msg.approval_id),
              { id: msg.approval_id, text: `${msg.decided_by} ${msg.status} this tool call.` },
            ]);
            window.setTimeout(
              () => setDecisions((prev) => prev.filter((d) => d.id !== msg.approval_id)),
              12000,
            );
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

  const activeRun = useMemo(
    () => runs.find((r) => r.status === "running" || r.status === "queued"),
    [runs],
  );

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
    if (!text || activeRun || uploading) return;
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
  const token = useMemo(
    () => (activeRun ? null : activeToken(prompt, caret)),
    [prompt, caret, activeRun],
  );
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
        {activeRun && (
          <button className="danger" onClick={cancel}>
            Stop
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
            {/* Which agent runs this conversation. Dead while a turn is in
                flight: the prompt has already gone to one CLI, and switching
                underneath it would leave the reply attributed to the other. */}
            {session && providers.length > 0 && (
              <select
                className="approval-select"
                value={session.provider}
                disabled={!!activeRun}
                title={
                  activeRun
                    ? `${providerLabel(session.provider)} is in the middle of a turn. ` +
                      "Switching agents now would leave that turn attributed to the wrong " +
                      "one — stop it or let it finish first."
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
            <div className="msg prompt">
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
        <textarea
          ref={promptRef}
          rows={3}
          value={prompt}
          placeholder={
            activeRun
              ? "Agent is working — stop it or wait…"
              : isDesktop
                ? "Describe the task…  (Enter to send, Shift+Enter for a new line, / for skills, @ for systems, paste or drop files)"
                : "Describe the task…  (Ctrl+Enter to send, / for skills, @ for systems, paste or drop files)"
          }
          disabled={!!activeRun}
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
          <button
            className="primary"
            type="submit"
            disabled={!!activeRun || sending || uploading || (!prompt.trim() && staged.length === 0)}
          >
            {sending ? "Sending…" : "Send"}
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
            disabled={!!activeRun}
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
            disabled={!!activeRun}
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
            disabled={!!activeRun}
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
