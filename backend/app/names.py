"""How a person is named on screen, decided in exactly one place.

A user has a `username`, which is their unique login and never changes shape,
and an optional `display_name`, which is what they would rather be called.
Everywhere a person is named the second wins when it is set — but it is
nullable, it is not unique, and two people really can both be "Walt", so the
username stays the thing that disambiguates and the thing you sign in with.

The rule lives here rather than being spelled `display_name or username` at
each call site, because a call site that forgets it does not fail: it silently
shows the login name to somebody who set a display name, and the bug is
invisible until a user complains. One import is easier to grep for than a
scattered idiom, and the frontend keeps the same single resolver in
`src/names.ts` for the same reason.
"""
from __future__ import annotations

from .models import User
from .schemas import UserSummary

#: Longest display name we store. Wider than a username (64) because a real
#: name with a team in brackets is a reasonable thing to want.
MAX_DISPLAY_NAME = 128


def display_name(user: User | None) -> str:
    """What to call this person. Never empty, never None."""
    if user is None:
        return "someone"
    chosen = (user.display_name or "").strip()
    return chosen or user.username


def clean_display_name(raw: str | None) -> str | None:
    """Normalise an incoming display name, or None to clear it.

    Whitespace-only is stored as NULL rather than as a blank string: a blank
    would satisfy `display_name or username` in every naive check and render as
    an anonymous gap on screen.
    """
    if raw is None:
        return None
    trimmed = raw.strip()
    return trimmed[:MAX_DISPLAY_NAME] or None


def summarise(user: User) -> UserSummary:
    """The shape every screen that names other people reads.

    Carries both names: the resolver on the client needs the fallback, and the
    username is what tells two people called "Walt" apart.
    """
    return UserSummary(id=user.id, username=user.username, display_name=user.display_name)
