from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    GithubAccount,
    RelayNode,
    Session,
    SessionShare,
    Target,
    TeamMember,
    User,
    Workspace,
)

#: use  — agents in this user's sessions may connect through the system.
#: manage — additionally edit it, replace the credential, and grant others.
LEVELS = ("use", "manage")


def level_for(target: Target, user: User | None) -> str | None:
    """What this user may do with a stored system, or None if it is not theirs to see.

    Administrators are deliberately not privileged here. Everywhere else in
    AIOps an admin sees everything, but a stored credential belongs to the
    person who put it in: administering the app is not the same as being
    entitled to reach someone else's servers.

    This lives apart from the router because the runner needs the same rule
    when deciding which systems a turn may reach, and importing it from the
    router package would drag every router in through its __init__.
    """
    if user is None:
        return None
    if target.owner_id is not None and target.owner_id == user.id:
        return "owner"
    for grant in target.grants:
        if grant.user_id == user.id:
            return grant.level if grant.level in LEVELS else "use"
    return None


def node_level_for(node: RelayNode, user: User | None) -> str | None:
    """The same rule again, for a relay node.

    A node is a way into somebody's network, so it is owned and shared exactly
    as a stored credential is — including administrators getting nothing they
    were not given. Approving a node *is* an administrator's job, but that is a
    separate question from being allowed to send traffic through it, and the
    router keeps the two apart.

    Written out rather than sharing a generic helper with `level_for`: the two
    happen to agree today, and if the rule for one ever changes it must be
    possible to change it without silently moving the other.
    """
    if user is None:
        return None
    if node.owner_id is not None and node.owner_id == user.id:
        return "owner"
    for grant in node.grants:
        if grant.user_id == user.id:
            return grant.level if grant.level in LEVELS else "use"
    return None


def workspace_level_for(workspace: Workspace, user: User | None) -> str | None:
    """The same rule a third time, for a workspace.

    A workspace is a directory an agent runs *inside*: it reads the files there,
    edits them, and runs its commands from them. So a workspace is its contents,
    and lending one out is lending out a checkout — often a private repository
    with its own history and its own secrets in it. That is the same kind of
    thing as a stored credential, and it is owned the same way: by whoever
    registered it, private until they name someone, and **not** reachable by an
    administrator who was not named. Administering AIOps is not entitlement to
    read somebody's code.

    Written out rather than sharing a generic helper with the two above, for
    exactly the reason given there: the three happen to agree today, and if the
    rule for one ever changes it must be possible to change it without silently
    moving the others.

    Used at run time as well as at the API, and against the turn's *requester*
    rather than the session's owner — see runner.py. A shared session must not
    lend its owner's workspace to everyone who can type into it, for the same
    reason it does not lend out their stored keys.
    """
    if user is None:
        return None
    if workspace.owner_id is not None and workspace.owner_id == user.id:
        return "owner"
    for grant in workspace.grants:
        if grant.user_id == user.id:
            return grant.level if grant.level in LEVELS else "use"
    return None


def github_account_level_for(account: GithubAccount, user: User | None) -> str | None:
    """The same rule a fourth time, for a stored GitHub personal access token.

    A GitHub account here *is* a bearer credential — whoever can use it can
    clone, push to, pull from and open pull requests against anything the
    token can reach on the owner's behalf. That is at least as sensitive as a
    stored system's password, so it is owned and shared the same way:
    private to whoever added it, and **administrators get nothing they were
    not given**. That has been true of `Target`, `RelayNode` and `Workspace`
    at every turn this project has taken, and this is not the turn it stops
    being true.

    Written out rather than sharing a generic helper with the three functions
    above, for exactly the reason given in theirs: they happen to agree today,
    and if the rule for one is ever changed it must be possible to change it
    without silently moving the others.
    """
    if user is None:
        return None
    if account.owner_id is not None and account.owner_id == user.id:
        return "owner"
    for grant in account.grants:
        if grant.user_id == user.id:
            return grant.level if grant.level in LEVELS else "use"
    return None


def sessions_visible_to(user: User) -> ColumnElement[bool]:
    """The clause that narrows a session query to what this user may see.

    Its owner, anyone it was shared with by name, and everyone in the team that
    owns it. **Administrators get nothing extra**, the same as for a stored
    system: a session is somebody's work, and being able to administer AIOps is
    not the same as being entitled to read it.

    Admins did once see everything, so that a session abandoned by someone who
    had left could still be unstuck. That is a real problem but this was the
    wrong answer to it — on an instance where everyone is an admin it made every
    session public. It is handled at the other end instead: deleting a user
    hands their shared sessions to somebody who can see them and removes the
    ones nobody else could (see routers/users.py), so there is nothing left
    stranded for an admin to need to reach.
    """
    return or_(
        Session.owner_id == user.id,
        Session.id.in_(select(SessionShare.session_id).where(SessionShare.user_id == user.id)),
        Session.team_id.in_(select(TeamMember.team_id).where(TeamMember.user_id == user.id)),
    )


async def session_viewer_ids(db: AsyncSession, session: Session) -> set[int]:
    """Everyone who can read this session's transcript, by user id.

    The same three routes as `sessions_visible_to`, resolved forwards instead of
    used as a filter: its owner, everyone it was shared with by name, and every
    member of the team that owns it. Administrators are not in it, because being
    an admin is not a way into a session (see the docstring above) — so this is
    the real audience for anything the agent prints here, not an approximation
    of it.

    Written as a set of ids rather than of users so the caller decides what to
    do about names: the exposure endpoint resolves them, the runner's transcript
    note resolves them, and neither wants the other's ordering.

    The shares are queried rather than read off `session.shares`, unlike
    `can_see_session` below. That relationship is only populated for a session
    that was loaded by a query, and this also runs against one just built in
    memory — creating a session straight into a team is shared before its first
    turn, so the check happens before the row is committed. Touching the
    collection there is an implicit lazy load, which under asyncio is not a slow
    path but a hard MissingGreenlet.
    """
    ids: set[int] = set()
    if session.owner_id is not None:
        ids.add(session.owner_id)
    ids.update(
        await db.scalars(
            select(SessionShare.user_id).where(SessionShare.session_id == session.id)
        )
    )
    if session.team_id is not None:
        ids.update(
            await db.scalars(
                select(TeamMember.user_id).where(TeamMember.team_id == session.team_id)
            )
        )
    return ids


async def can_see_session(db: AsyncSession, session: Session, user: User) -> bool:
    """The same rule for one already-loaded session."""
    if session.owner_id is not None and session.owner_id == user.id:
        return True
    if any(share.user_id == user.id for share in session.shares):
        return True
    if session.team_id is None:
        return False
    membership = await db.scalar(
        select(TeamMember.id).where(
            TeamMember.team_id == session.team_id, TeamMember.user_id == user.id
        )
    )
    return membership is not None
