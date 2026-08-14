"""Briefing an incoming agent on a conversation it did not have.

Switching a session's provider mid-conversation cannot be a state transfer.
Each CLI owns its own conversation state and can only resume its own — Claude by
`--resume <session-id>`, Codex by `thread/resume` — and neither can be pointed at
the other's. There is no format to convert between and no file to hand over.

So what a switch actually produces is a *handoff*: AIOps writes a briefing out
of the transcript it kept for itself, and the incoming agent starts a brand new
conversation whose first message happens to begin with a report of what has
already been agreed. It is a colleague reading somebody else's notes, not
somebody remembering their own work, and the wording below says so — an agent
that mistakes a summary for its own memory will state things it did not verify
with the confidence of something it saw itself.

Three properties this file exists to guarantee:

* it is **capped** by character budget, and when content is dropped it drops the
  *oldest* turns and says in the briefing that it did. Silently losing the
  middle of a conversation is worse than admitting the gap: an agent told
  nothing is missing will contradict a decision it never saw.
* it **replays no secrets**. Everything goes through redaction.redact, and tool
  output is left out of the briefing altogether (see _turn_block).
* it goes in the prompt handed to the CLI and never into `run.prompt`, which is
  what the transcript shows the user — the same split the runner already uses
  for attachment paths.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Event, Run, Session
from .redaction import dumps_environment, redact

#: How each provider is named to the agent reading this. Its own name is one of
#: them, which is the point: "you are Codex" has to be recognisable as itself.
LABELS = {"claude": "Claude", "codex": "Codex"}

#: Per-item ceiling, so one enormous reply cannot consume the whole budget
#: before the turn-level cap gets a chance to drop older turns instead.
_ITEM_LIMIT = 2000
#: Tool calls are listed as one short line each; the interesting part is which
#: files and commands were touched, not the arguments in full.
_ACTION_LIMIT = 200
_ACTIONS_PER_TURN = 12

#: Room kept for conversation on top of the briefing's own framing. A budget too
#: small to hold the framing plus this is raised until it is — a briefing that
#: tells an agent it is inheriting somebody's conversation and then says nothing
#: about the conversation is worse than no briefing at all, because it invites
#: the agent to guess. The cap is honoured above this floor and reported below
#: it; the setting's default leaves two orders of magnitude of headroom.
_MIN_TURN_ROOM = 900

_TRUNCATED = "\n[briefing cut off here to stay within its size limit]"


def label(provider: str | None) -> str:
    return LABELS.get(provider or "", (provider or "another agent").title())


#: What the transcript records at the point a session changed hands. A kind of
#: its own, not a "system" line, so the UI can draw the break rather than let it
#: scroll past among the CLI's own notices.
SWITCH_KIND = "provider_switch"


async def record_switch(
    db: AsyncSession,
    sess: Session,
    *,
    outgoing: str,
    incoming: str,
    username: str | None = None,
) -> bool:
    """Write the switch into the transcript. True when there was history to brief.

    Hung off the last turn, because that is where it happened: events belong to
    runs, and the marker has to sort after the output of the turn it follows for
    the transcript to read in order. A session with no turns yet has nothing to
    mark and nothing to summarise — changing the provider before the first
    message is just choosing one, so this reports False and the caller skips the
    briefing entirely.
    """
    last = await db.scalar(
        select(Run).where(Run.session_id == sess.id).order_by(Run.id.desc()).limit(1)
    )
    if last is None:
        return False
    seq = (await db.scalar(select(func.max(Event.seq)).where(Event.run_id == last.id))) or 0
    by = f" by {username}" if username else ""
    db.add(
        Event(
            run_id=last.id,
            session_id=sess.id,
            seq=seq + 1,
            kind=SWITCH_KIND,
            text=(
                f"Switched from {label(outgoing)} to {label(incoming)}{by}. "
                f"{label(incoming)} cannot read {label(outgoing)}'s session, so it is not "
                f"continuing this conversation — it starts a new one. The next message "
                f"sent from here is prefixed with a written summary of everything above, "
                f"assembled by AIOps. Anything {label(outgoing)} knew but never said out "
                f"loud in this transcript is gone."
            ),
            raw={"aiops_provider_switch": {"from": outgoing, "to": incoming, "by": username}},
        )
    )
    return True


async def build_digest(
    db: AsyncSession,
    sess: Session,
    *,
    before_run_id: int,
    budget: int | None = None,
) -> str:
    """The briefing to prefix onto the first turn after a switch.

    Empty when there is nothing to hand over, so the caller can prefix
    unconditionally. `before_run_id` excludes the turn being briefed — its
    prompt is what the CLI is being sent, and repeating it above itself would
    read as the user having said the same thing twice.
    """
    budget = budget if budget is not None else settings.handoff_digest_max_chars

    runs = list(
        await db.scalars(
            select(Run)
            .where(Run.session_id == sess.id, Run.id < before_run_id)
            .order_by(Run.id)
        )
    )
    if not runs:
        return ""

    events = list(
        await db.scalars(
            select(Event)
            .where(Event.run_id.in_([r.id for r in runs]))
            .order_by(Event.run_id, Event.seq)
        )
    )
    by_run: dict[int, list[Event]] = {}
    for event in events:
        by_run.setdefault(event.run_id, []).append(event)

    blocks = [
        _turn_block(number, run, by_run.get(run.id, []))
        for number, run in enumerate(runs, start=1)
    ]
    # Whoever was actually answering, read off the turns rather than off the
    # session: the session's provider is already the *new* one by the time this
    # runs, and a mixed session may have been through more than one.
    outgoing = next(
        (r.provider for r in reversed(runs) if r.provider and r.provider != sess.provider),
        None,
    )
    if outgoing is None:
        # Switched away and back before the other provider ever answered. The
        # briefing is still owed — the round trip threw the resumable session id
        # away — but it is being handed to the same agent as before, and telling
        # it that it is taking over from itself would be nonsense.
        outgoing = next((r.provider for r in reversed(runs) if r.provider), None)
    return _fit(_header(outgoing, sess), blocks, _FOOTER, budget)


def _header(outgoing: str | None, sess: Session) -> str:
    who = label(sess.provider)
    them = label(outgoing)
    where = f" using {sess.model}" if sess.model else ""
    # Same agent, different session: the honest framing is "you, earlier, in a
    # conversation you cannot reopen" rather than "somebody else".
    whose = (
        f"an earlier session of {them} that you cannot reopen"
        if outgoing == sess.provider
        else f"{them}, in a separate session you have no access to"
    )
    return (
        f"--- HANDOFF BRIEFING: you are taking over a conversation in progress ---\n"
        f"You are {who}{where}, and this is your first turn in this conversation. "
        f"Until now it was being handled by {whose} — you were switched in by the "
        f"operator partway through.\n\n"
        f"What follows is not your own history and not a log you produced. It is a "
        f"written record assembled by AIOps, the tool running both of you, from the "
        f"transcript it kept: the operator's messages, {them}'s replies, and the "
        f"actions {them} took. Read it the way you would read handover notes from a "
        f"colleague who has left for the day. Where something in it is load-bearing "
        f"and you cannot see it for yourself, check it rather than assuming it — the "
        f"state of the files and systems described here is whatever {them} left "
        f"behind, and this summary is the only thing telling you about it.\n\n"
        f"Tool output is deliberately not reproduced below, only the fact that a "
        f"tool was used. If you need what a command printed, run it again."
    )

_FOOTER = (
    "--- END OF BRIEFING ---\n"
    "Everything above is context you were given, not work you did. The "
    "operator's new message follows."
)


def _notice(omitted: int) -> str:
    turns = "turn" if omitted == 1 else "turns"
    return (
        f"[!] THIS BRIEFING IS INCOMPLETE. The {omitted} earliest {turns} of this "
        f"conversation would not fit inside the briefing's size limit and are not "
        f"below. The conversation began before what you can see. Do not treat the "
        f"first turn shown as its opening, and do not conclude that something was "
        f"never discussed because it does not appear here — ask the operator if it "
        f"matters."
    )


def _turn_block(number: int, run: Run, events: list[Event]) -> str:
    """One turn, rendered for somebody who was not there.

    Tool *results* are left out entirely. They are the bulkiest thing in a
    transcript and the least summarisable, and they are also where a secret ends
    up when one leaks: the incident this file's redaction pass exists for was an
    agent dumping the control plane's own environment into one. A briefing that
    replays them is a new way for that to travel, and the cost of omitting them
    is one extra command for the incoming agent (see the header).
    """
    who = label(run.provider)
    lines = [f"--- Turn {number} — answered by {who}{f' ({run.model})' if run.model else ''} ---"]
    lines.append(f"Operator asked:\n{_clip(redact(run.prompt), _ITEM_LIMIT)}")

    replies = [
        _clip(redact(e.text), _ITEM_LIMIT)
        for e in events
        if e.kind == "assistant" and (e.text or "").strip()
    ]
    if replies:
        lines.append(f"{who} replied:\n" + "\n\n".join(replies))

    actions = _actions(events)
    if actions:
        lines.append(f"{who} did:\n" + "\n".join(f"- {a}" for a in actions))

    if run.status not in ("succeeded", "queued", "running"):
        detail = _clip(redact(run.error), 400) if run.error else ""
        lines.append(
            f"[This turn did not finish cleanly — it ended {run.status}."
            + (f" The error was: {detail}]" if detail else "]")
        )
    elif not replies and not actions:
        lines.append("[No reply was recorded for this turn.]")

    return "\n\n".join(lines)


def _actions(events: list[Event]) -> list[str]:
    out: list[str] = []
    extra = 0
    for event in events:
        if event.kind != "tool_use":
            continue
        name = event.tool_name or "tool"
        detail = (event.text or "").strip()
        if dumps_environment(detail):
            # The command line itself is not the secret — its output was. Naming
            # it in a briefing serves nothing and invites the incoming agent to
            # repeat it, so the fact is kept and the command is not.
            summary = f"{name} (a command that reads the environment; not repeated here)"
        else:
            summary = f"{name}: {_clip(redact(detail), _ACTION_LIMIT)}" if detail else name
        if len(out) >= _ACTIONS_PER_TURN:
            extra += 1
            continue
        out.append(summary.replace("\n", " "))
    if extra:
        out.append(f"… and {extra} further tool call(s), not listed")
    return out


def _fit(header: str, blocks: list[str], footer: str, budget: int) -> str:
    """Assemble the briefing, dropping whole turns from the oldest end.

    The notice's length is reserved whether or not it is used, so deciding to
    include it cannot itself push the result over the budget — which is the bug
    the obvious version of this has.
    """
    reserve = len(header) + len(footer) + len(_notice(len(blocks))) + 8
    budget = max(budget, reserve + _MIN_TURN_ROOM)
    room = budget - reserve

    chosen: list[str] = []
    used = 0
    for block in reversed(blocks):
        if not chosen and len(block) + 2 > room:
            # Never return a briefing with no turns in it at all: the most
            # recent one is the one the incoming agent most needs, so it is
            # clipped rather than dropped.
            chosen.append(_clip(block, max(room - 2, 100)))
            used = room
            continue
        if used + len(block) + 2 > room:
            break
        chosen.append(block)
        used += len(block) + 2

    omitted = len(blocks) - len(chosen)
    parts = [header]
    if omitted:
        parts.append(_notice(omitted))
    parts.extend(reversed(chosen))
    parts.append(footer)
    out = "\n\n".join(parts)

    if len(out) > budget:
        keep = budget - len(footer) - len(_TRUNCATED) - 2
        out = f"{out[:max(keep, 0)]}{_TRUNCATED}\n\n{footer}"
    return out[:budget]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… [{len(text) - limit} characters omitted]"
