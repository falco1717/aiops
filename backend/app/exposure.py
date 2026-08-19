"""Who will read what your stored credentials produce, said before it happens.

A turn reaches the systems the person who *asked for it* may reach —
`runner._attempt` scopes `visible_targets` to `run.requested_by_id`, not to the
session's owner. That is the right rule and it is not changed here: somebody
should be able to bring their own systems into any conversation they are part
of, and an intersection rule ("only systems every viewer can reach") would
quietly make a shared session less capable than a private one for no gain in
safety, since the transcript is shared either way.

`runner._attempt` resolves a second kind of credential the same way: a
workspace may name a GitHub account for `git push`/`pull` and pull requests,
and the turn only gets to use it when the *requester* — not the workspace's
owner — has at least `use` on that account (`access.github_account_level_for`).
That is the same rule as the one above, reached the same way, and it is folded
into this module rather than given one of its own for exactly the reason this
module exists at all: a GitHub token is a bearer credential, at least as
sensitive as a stored system's password, and the three risks below apply to it
without needing to be restated.

What is missing without this module is not a restriction, it is a *disclosure*.
Three things happen when Bob prompts a session Alice can read, using a
credential — a stored system, a linked GitHub account, or both — Alice cannot
reach:

1. Everything the agent does with it lands in a transcript Alice reads —
   command output, file contents, whatever was on the far end, or a pull
   request opened in Bob's name.
2. The decrypted secret — a private key or a GitHub token — is a file on disk,
   readable by the agent, for the life of the run (see ssh_targets.prepare and
   github_creds.prepare). One `cat` of it and the credential material itself is
   in the transcript.
3. Alice's earlier messages are conversation context for Bob's turn. She can
   leave an instruction in the thread that the agent carries out on Bob's next
   prompt, holding Bob's credentials. That is a confused deputy, not merely an
   information leak, and it is the part nobody thinks of: it means a viewer does
   not have to wait for the secret to be printed, she can ask for the host to be
   used, or for a pull request to be opened. This is sharper for a GitHub
   account than for a stored system: opening a pull request is one ordinary
   sentence for the agent to act on, where reaching a stored host at least
   needs the viewer to know it exists.

So the facts are computed here, server-side, from the same visibility rules
everything else uses, and the person whose credential it is is told and asked
— once, for everything of theirs this turn could reach, not once per kind of
credential. Splitting the disclosure in two would let somebody acknowledge
their SSH key being exposed while never being asked about their GitHub token
reaching the same audience, which is the exact half-fix this module exists to
avoid. Nothing in this module can refuse a run — the only thing it gates is
the *first* prompt into a session where this applies, and the gate opens as
soon as the person says they understand.

Nothing here discloses anything the caller could not already learn: the
usernames come from the same population `/api/users/directory` hands to any
signed-in user, and the systems and GitHub accounts are the caller's own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .access import github_account_level_for, session_viewer_ids, workspace_level_for
from .models import (
    Event,
    GithubAccount,
    Run,
    Session,
    SessionExposureAck,
    Target,
    User,
    utcnow,
)
from .names import display_name
from .ssh_targets import visible_targets

#: What the transcript note is tagged with, so it can be found after the fact
#: without matching on prose. The question this exists to answer is "who agreed
#: to expose that credential, and to whom", asked months later by somebody
#: looking at a host that was touched from a session they are reading.
EVENT_MARKER = "aiops_credential_exposure"


@dataclass
class Exposure:
    """What one person's stored credentials are exposed to in one session.

    Always from the point of view of a particular caller: `viewers` never
    includes them, and `systems` and `github_accounts` are only ever their own.
    One acknowledgement covers both — see the module docstring for why this is
    a single combined `Exposure` rather than one per credential kind.
    """

    session_id: str
    #: Everyone else who can read this session's transcript.
    viewers: list[User] = field(default_factory=list)
    #: The caller's own stored systems, which a turn of theirs here would reach.
    systems: list[Target] = field(default_factory=list)
    #: The caller's own GitHub account, when this session's workspace names one
    #: they may use. At most one — a workspace links to a single account — but
    #: kept as a list to stay parallel with `systems` and to leave room for a
    #: session gaining more than one credential-bearing resource later.
    github_accounts: list[GithubAccount] = field(default_factory=list)
    #: Their standing acknowledgement for this session, if they have given one.
    ack: SessionExposureAck | None = None

    @property
    def shared(self) -> bool:
        return bool(self.viewers)

    @property
    def at_stake(self) -> bool:
        """True when there is actually something to warn about.

        Both halves are required. A private session exposes nothing to anyone,
        and a user with no stored systems and no reachable GitHub account has
        nothing of their own to expose — warning either of them trains people
        to click past the warning that matters.
        """
        return bool(self.viewers) and bool(self.systems or self.github_accounts)

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
        github_accounts=await _reachable_github_accounts(db, sess, user),
        ack=await _ack(db, sess.id, user.id),
    )


async def _reachable_github_accounts(
    db: AsyncSession, sess: Session, user: User
) -> list[GithubAccount]:
    """The GitHub account a turn of this caller's here would authenticate as.

    Mirrors `runner._attempt`'s own resolution exactly, both of its steps: a
    workspace names at most one GitHub account for `git push`/`pull` and pull
    requests, and a turn only reaches it when *this caller* — the person this
    exposure is being described for, who is the requester the moment they next
    prompt — has at least `use` on the *workspace itself* (checked first in
    `runner.py`, so a turn that cannot even reach the workspace never reaches
    the credential check) and at least `use` on the account. Skipping the
    workspace check here would disclose an account to somebody whose actual
    next prompt would fail on the workspace step before the credential was ever
    touched — a false alarm the same way warning a user with no stored systems
    would be.

    Reached through `sess.workspace`, the same `lazy="joined"` relationship
    `runner.py` reads, so this never issues a query the runner itself would
    not.

    Returns a list of zero or one rather than an `Optional`, to stay parallel
    with `systems` above and because "the caller's reachable credentials here"
    is naturally a collection, not a single special-cased field.
    """
    workspace = sess.workspace
    if workspace is None or workspace.github_account_id is None:
        return []
    if workspace_level_for(workspace, user) is None:
        return []
    account = await db.get(GithubAccount, workspace.github_account_id)
    if account is None or github_account_level_for(account, user) is None:
        return []
    return [account]


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


#: Refused with this when a turn would put the caller's credentials in front of
#: people who have not been disclosed to them yet. Deliberately not a 403: the
#: caller is entitled to do this, they have simply not been told what it means.
#: `{what}` names the kind(s) of credential actually at stake for this caller —
#: see `_what_phrase` — so somebody with only a linked GitHub account is not
#: told their "stored systems" are exposed when they have none.
REFUSAL = (
    "Your {what} would be reachable from this turn, and this session is "
    "readable by {who}. Confirm that you understand what that exposes before the "
    "first such turn — the chat view asks, or POST to this session's "
    "/exposure/ack. Nothing about what you can reach changes either way; this is "
    "a disclosure, not a restriction."
)


def _what_phrase(exposure: Exposure) -> str:
    """Names, in English, which kinds of credential this caller has at stake.

    Kept separate from `REFUSAL` so it can be reasoned about (and tested) on
    its own: the combined-disclosure design means a caller can have either
    kind, or both, and the refusal has to say the one(s) that are actually
    true rather than always naming "stored systems" the way it did before a
    GitHub account could be the only thing at stake.
    """
    bits = []
    if exposure.systems:
        bits.append("stored systems")
    if exposure.github_accounts:
        bits.append(
            "GitHub account" if len(exposure.github_accounts) == 1 else "GitHub accounts"
        )
    if not bits:
        # Only reached if `refusal` is ever called on an exposure that is not
        # `at_stake` — not a real path, but an honest fallback beats a KeyError.
        return "stored credentials"
    if len(bits) == 1:
        return bits[0]
    return " and ".join(bits)


def refusal(exposure: Exposure) -> str:
    # Named the way the reader sees them named everywhere else — this is a
    # sentence a person has to make a decision from, and a login name they have
    # never seen is a worse prompt than the name on their screen. The transcript
    # record written by `record_use` below deliberately does *not* follow suit:
    # that one is the audit trail, and an audit trail wants the identifier that
    # is unique.
    return REFUSAL.format(
        what=_what_phrase(exposure),
        who=_join(display_name(v) for v in exposure.new_viewers),
    )


async def record_use(
    db: AsyncSession,
    run: Run,
    sess: Session,
    targets: list[Target],
    *,
    asker: User | None,
    github_account: GithubAccount | None = None,
) -> Event | None:
    """Note in the transcript that stored credentials were used in front of others.

    Written as an ordinary `system` event so it reads inline where it happened,
    rather than as a new kind needing its own rendering — the fact is prose, not
    a structural break in the conversation the way a provider switch is.

    `github_account` is the workspace's linked GitHub account, passed only when
    this run actually got a materialised credential for it (`runner.py` passes
    it exactly when `github_creds.prepare` succeeded) — the same "only what was
    actually usable" rule `targets` already followed via `usable` in the caller.

    Returns None when there is nothing to record: no targets and no GitHub
    account, or nobody but the requester can read this session. An
    after-the-fact record is the point — if a credential is later found to have
    been misused, "whose was it, who agreed to expose it, and who could read
    the result" has to be answerable from the session itself, not reconstructed
    from the fact that a shared session once existed.

    Idempotent per run, because a failover retry runs the same turn again and
    the transcript should say this once per turn, not once per attempt.
    """
    if not targets and github_account is None:
        return None
    if asker is None:
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

    # Built up rather than one fixed sentence, because either credential kind
    # may be absent — a run may have only stored systems, only a linked GitHub
    # account, or both, and the note should say exactly what was actually
    # reachable, not a sentence written for one case and stretched over both.
    credential_bits = []
    if targets:
        credential_bits.append(f"stored systems available to the agent: {', '.join(slugs)}")
    if github_account is not None:
        credential_bits.append(
            f"the GitHub account {github_account.label!r} available to the agent "
            "for git and pull requests"
        )
    credentials_clause = " and ".join(credential_bits)

    seq = (await db.scalar(select(func.max(Event.seq)).where(Event.run_id == run.id))) or 0
    event = Event(
        run_id=run.id,
        session_id=sess.id,
        seq=seq + 1,
        kind="system",
        text=(
            f"This turn ran with {asker.username}'s {credentials_clause}. Anything "
            f"they produce here — command output, file contents, and the credential "
            f"material itself if the agent is asked to print it — is "
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
                "github_account": (
                    {"id": github_account.id, "label": github_account.label}
                    if github_account is not None
                    else None
                ),
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
