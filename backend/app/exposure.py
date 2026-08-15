"""Who will read what your stored credentials produce, said before it happens.

A turn reaches the systems the person who *asked for it* may reach —
`runner._attempt` scopes `visible_targets` to `run.requested_by_id`, not to the
session's owner. That is the right rule and it is not changed here: somebody
should be able to bring their own systems into any conversation they are part
of, and an intersection rule ("only systems every viewer can reach") would
quietly make a shared session less capable than a private one for no gain in
safety, since the transcript is shared either way.

What is missing without this module is not a restriction, it is a *disclosure*.
Three things happen when Bob prompts a session Alice can read, using a system
Alice cannot reach:

1. Everything the agent does on that host lands in a transcript Alice reads —
   command output, file contents, whatever was on the far end.
2. The decrypted private key is a file on disk, readable by the agent, for the
   life of the run (see ssh_targets.prepare). One `cat` of it and the key
   material itself is in the transcript.
3. Alice's earlier messages are conversation context for Bob's turn. She can
   leave an instruction in the thread that the agent carries out on Bob's next
   prompt, holding Bob's credentials. That is a confused deputy, not merely an
   information leak, and it is the part nobody thinks of: it means a viewer does
   not have to wait for the key to be printed, she can ask for the host to be
   used.

So the facts are computed here, server-side, from the same visibility rules
everything else uses, and the person whose credential it is is told and asked.
Nothing in this module can refuse a run — the only thing it gates is the *first*
prompt into a session where this applies, and the gate opens as soon as the
person says they understand.

Nothing here discloses anything the caller could not already learn: the
usernames come from the same population `/api/users/directory` hands to any
signed-in user, and the systems are the caller's own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .access import session_viewer_ids
from .models import Event, Run, Session, SessionExposureAck, Target, User, utcnow
from .names import display_name
from .ssh_targets import visible_targets

#: What the transcript note is tagged with, so it can be found after the fact
#: without matching on prose. The question this exists to answer is "who agreed
#: to expose that credential, and to whom", asked months later by somebody
#: looking at a host that was touched from a session they are reading.
EVENT_MARKER = "aiops_credential_exposure"


@dataclass
class Exposure:
    """What one person's stored systems are exposed to in one session.

    Always from the point of view of a particular caller: `viewers` never
    includes them, and `systems` is only ever their own.
    """

    session_id: str
    #: Everyone else who can read this session's transcript.
    viewers: list[User] = field(default_factory=list)
    #: The caller's own stored systems, which a turn of theirs here would reach.
    systems: list[Target] = field(default_factory=list)
    #: Their standing acknowledgement for this session, if they have given one.
    ack: SessionExposureAck | None = None

    @property
    def shared(self) -> bool:
        return bool(self.viewers)

    @property
    def at_stake(self) -> bool:
        """True when there is actually something to warn about.

        Both halves are required. A private session exposes nothing to anyone,
        and a user with no stored systems has nothing of their own to expose —
        warning either of them trains people to click past the warning that
        matters.
        """
        return bool(self.viewers) and bool(self.systems)

    @property
    def new_viewers(self) -> list[User]:
        """Viewers the standing acknowledgement does not cover.

        Everyone, when there is no acknowledgement at all. This is what re-arms
        the prompt: agreeing that Bob may read what your key produces is not
        agreeing that Carol may.
        """
        if self.ack is None:
            return list(self.viewers)
        known = set(self.ack.viewer_ids or ())
        return [v for v in self.viewers if v.id not in known]

    @property
    def acknowledged(self) -> bool:
        """Whether the caller has agreed to the audience as it stands now."""
        return self.ack is not None and not self.new_viewers

    @property
    def needs_acknowledgement(self) -> bool:
        return self.at_stake and not self.acknowledged

    @property
    def viewer_ids(self) -> list[int]:
        return sorted(v.id for v in self.viewers)

    @property
    def acknowledged_at(self) -> datetime | None:
        return self.ack.updated_at if self.ack else None


async def describe(db: AsyncSession, sess: Session, user: User) -> Exposure:
    """The exposure facts for this caller in this session.

    Callers must have already checked that `user` may see `sess` — this does not
    re-derive that, exactly like every other helper reached through the session
    router's `_get`.
    """
    viewer_ids = await session_viewer_ids(db, sess) - {user.id}
    viewers = (
        list(
            await db.scalars(
                select(User).where(User.id.in_(viewer_ids)).order_by(User.username)
            )
        )
        if viewer_ids
        else []
    )
    return Exposure(
        session_id=sess.id,
        viewers=viewers,
        systems=await visible_targets(db, user),
        ack=await _ack(db, sess.id, user.id),
    )


async def _ack(db: AsyncSession, session_id: str, user_id: int) -> SessionExposureAck | None:
    return await db.scalar(
        select(SessionExposureAck).where(
            SessionExposureAck.session_id == session_id,
            SessionExposureAck.user_id == user_id,
        )
    )


async def acknowledge(
    db: AsyncSession, sess: Session, user: User, viewer_ids: list[int]
) -> None:
    """Record that this user agreed to this audience. Does not commit.

    The stored set is whatever the server just computed, never what a client
    sent: a client that under-reported the audience would otherwise be able to
    buy itself a broader consent than the user was shown.
    """
    row = await _ack(db, sess.id, user.id)
    if row is None:
        db.add(
            SessionExposureAck(
                session_id=sess.id, user_id=user.id, viewer_ids=sorted(viewer_ids)
            )
        )
        return
    # Replaced rather than unioned. The set is a record of what was shown and
    # agreed to at one moment; carrying forward somebody who has since been
    # removed would silently pre-approve them if they were ever added back.
    row.viewer_ids = sorted(viewer_ids)
    # Stamped rather than left to `onupdate`, which fires only when SQLAlchemy
    # sees a column change: re-agreeing to the audience already on the row would
    # otherwise leave the transcript note citing the older date.
    row.updated_at = utcnow()


#: Refused with this when a turn would put the caller's systems in front of
#: people who have not been disclosed to them yet. Deliberately not a 403: the
#: caller is entitled to do this, they have simply not been told what it means.
REFUSAL = (
    "Your stored systems would be reachable from this turn, and this session is "
    "readable by {who}. Confirm that you understand what that exposes before the "
    "first such turn — the chat view asks, or POST to this session's "
    "/exposure/ack. Nothing about what you can reach changes either way; this is "
    "a disclosure, not a restriction."
)


def refusal(exposure: Exposure) -> str:
    # Named the way the reader sees them named everywhere else — this is a
    # sentence a person has to make a decision from, and a login name they have
    # never seen is a worse prompt than the name on their screen. The transcript
    # record written by `record_use` below deliberately does *not* follow suit:
    # that one is the audit trail, and an audit trail wants the identifier that
    # is unique.
    return REFUSAL.format(who=_join(display_name(v) for v in exposure.new_viewers))


async def record_use(
    db: AsyncSession,
    run: Run,
    sess: Session,
    targets: list[Target],
    *,
    asker: User | None,
) -> Event | None:
    """Note in the transcript that stored systems were used in front of others.

    Written as an ordinary `system` event so it reads inline where it happened,
    rather than as a new kind needing its own rendering — the fact is prose, not
    a structural break in the conversation the way a provider switch is.

    Returns None when there is nothing to record: no systems, or nobody but the
    requester can read this session. An after-the-fact record is the point —
    if a credential is later found to have been misused, "whose was it, who
    agreed to expose it, and who could read the result" has to be answerable
    from the session itself, not reconstructed from the fact that a shared
    session once existed.

    Idempotent per run, because a failover retry runs the same turn again and
    the transcript should say this once per turn, not once per attempt.
    """
    if not targets or asker is None:
        return None
    viewer_ids = await session_viewer_ids(db, sess) - {asker.id}
    if not viewer_ids:
        return None
    if await _already_recorded(db, run.id):
        return None

    viewers = list(
        await db.scalars(select(User).where(User.id.in_(viewer_ids)).order_by(User.username))
    )
    ack = await _ack(db, sess.id, asker.id)
    slugs = [t.slug for t in targets]
    readers = _join(v.username for v in viewers)

    seq = (await db.scalar(select(func.max(Event.seq)).where(Event.run_id == run.id))) or 0
    event = Event(
        run_id=run.id,
        session_id=sess.id,
        seq=seq + 1,
        kind="system",
        text=(
            f"This turn ran with {asker.username}'s stored systems available to the "
            f"agent: {', '.join(slugs)}. Anything they produce here — command output, "
            f"file contents, and the key itself if the agent is asked to print it — is "
            f"readable by everyone who can see this session: {readers}."
            + (
                f" {asker.username} acknowledged that on "
                f"{ack.updated_at:%Y-%m-%d %H:%M} UTC."
                if ack is not None and ack.updated_at is not None
                else ""
            )
        ),
        raw={
            EVENT_MARKER: {
                "used_by": asker.username,
                "used_by_id": asker.id,
                "systems": [
                    {"id": t.id, "slug": t.slug, "name": t.name} for t in targets
                ],
                "readable_by": [
                    {"id": v.id, "username": v.username} for v in viewers
                ],
                "acknowledged_at": (
                    ack.updated_at.isoformat() if ack and ack.updated_at else None
                ),
                "acknowledged_viewer_ids": sorted(ack.viewer_ids or ()) if ack else None,
            }
        },
    )
    db.add(event)
    return event


async def _already_recorded(db: AsyncSession, run_id: int) -> bool:
    rows = await db.scalars(
        select(Event.raw).where(Event.run_id == run_id, Event.kind == "system")
    )
    return any(isinstance(raw, dict) and EVENT_MARKER in raw for raw in rows)


def _join(names) -> str:
    names = sorted(names)
    if not names:
        return "nobody else"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"
