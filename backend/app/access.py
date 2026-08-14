from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RelayNode, Session, SessionShare, Target, TeamMember, User

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
