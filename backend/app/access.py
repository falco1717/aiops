from __future__ import annotations

from sqlalchemy import ColumnElement, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Session, SessionShare, Target, TeamMember, User

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


def sessions_visible_to(user: User) -> ColumnElement[bool]:
    """The clause that narrows a session query to what this user may see.

    Its owner, anyone it was shared with by name, and everyone in the team that
    owns it. Administrators are included here — the reverse of the rule above,
    and for an operational reason rather than a symmetric one: a session left
    behind by somebody who has gone still has a stopped agent in it, and only an
    administrator can be relied on to still be here to unstick it.
    """
    if user.is_admin:
        return true()
    return or_(
        Session.owner_id == user.id,
        Session.id.in_(select(SessionShare.session_id).where(SessionShare.user_id == user.id)),
        Session.team_id.in_(select(TeamMember.team_id).where(TeamMember.user_id == user.id)),
    )


async def can_see_session(db: AsyncSession, session: Session, user: User) -> bool:
    """The same rule for one already-loaded session."""
    if user.is_admin:
        return True
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
