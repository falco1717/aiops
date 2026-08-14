from __future__ import annotations

from .models import Target, User

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
