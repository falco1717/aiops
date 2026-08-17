from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import attachments as store
from ..db import get_db
from ..models import (
    Approval,
    Attachment,
    Event,
    RelayNode,
    RelayNodeAccess,
    Run,
    Schedule,
    Session,
    SessionExposureAck,
    SessionShare,
    Target,
    TargetAccess,
    TeamMember,
    User,
    Workspace,
    WorkspaceAccess,
)
from ..names import clean_display_name, summarise
from ..runner import runner
from ..schemas import (
    ProfilePatch,
    UserCreate,
    UserOut,
    UserPasswordReset,
    UserPatch,
    UserSummary,
)
from ..security import current_admin, current_user, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


async def _admin_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True))) or 0


@router.get("", response_model=list[UserOut])
async def list_users(_: User = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(select(User).order_by(User.id)))


@router.get("/directory", response_model=list[UserSummary])
async def directory(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Names to share things with, for anyone signed in.

    Sharing is not an admin action — whoever stores a system decides who else
    may reach it — so it cannot depend on the admin-only listing above. This
    exposes names and nothing else: no roles, no timestamps, no password state.

    Ordered by username rather than display name: the sort has to be stable and
    display names are optional, so ordering by one would shuffle the list every
    time somebody set or cleared theirs.
    """
    rows = await db.scalars(select(User).order_by(User.username))
    return [summarise(u) for u in rows]


@router.patch("/me", response_model=UserOut)
async def patch_me(
    payload: ProfilePatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set your own display name.

    Declared above `/{user_id}`, because FastAPI matches in order and the path
    parameter is an int — "me" would otherwise be rejected as a bad integer
    before this route was ever considered.

    Separate from the admin route below rather than a special case inside it:
    an administrator editing somebody is deciding what everyone else calls that
    person, and this is a person deciding what they are called. Those are the
    same field but not the same permission, and folding them together is how a
    non-admin ends up able to PATCH a route that also carries `is_admin`.
    """
    data = payload.model_dump(exclude_unset=True)
    if "display_name" in data:
        user.display_name = clean_display_name(data["display_name"])
    await db.commit()
    await db.refresh(user)
    return user


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    _: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username must not be empty")
    if await db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken")

    user = User(
        username=username,
        # No uniqueness check: display names are not unique by design, so two
        # Walts are allowed and the usernames beside them do the disambiguating.
        display_name=clean_display_name(payload.display_name),
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
        must_change_password=payload.must_change_password,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: int,
    payload: UserPatch,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    data = payload.model_dump(exclude_unset=True)
    # Removing the last admin would leave nobody able to manage users or sign
    # the agent CLIs in.
    if data.get("is_admin") is False and user.is_admin and await _admin_count(db) <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This is the only admin — promote someone else first"
        )
    if data.get("is_admin") is False and user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot remove your own admin rights")

    # Normalised rather than stored raw: an admin sending "  " would otherwise
    # leave a blank name that renders as a gap instead of falling back.
    if "display_name" in data:
        data["display_name"] = clean_display_name(data["display_name"])
    for key, value in data.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: int,
    payload: UserPasswordReset,
    _: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin reset. The target must change it at next sign-in unless told otherwise."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = payload.must_change_password
    await db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot delete your own account")
    if user.is_admin and await _admin_count(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot delete the only admin")
    await _hand_on_systems(db, user)
    await _release_schedules(db, user)
    destroyed = await _release_sessions(db, user)
    await db.delete(user)
    await db.commit()
    # Uploads on disk, once the rows that named them are actually gone. After the
    # commit rather than before: a failure between the two would otherwise leave
    # a session whose attachments the UI still lists and cannot fetch.
    for session_id in destroyed:
        store.discard_session(session_id)


async def _release_sessions(db: AsyncSession, leaving: User) -> list[str]:
    """Take a departing user out of everything that grants session access, and
    leave none of their own sessions visible to nobody.

    Returns the ids of the sessions destroyed, whose uploads the caller should
    remove from disk once the transaction has landed.

    The membership and share rows go first. The foreign keys say as much, but
    SQLite does not enforce ON DELETE and reuses integer ids, so a leftover
    share is a grant lying in wait for whoever is created next.

    Then their own sessions, which used to be left ownerless. That worked only
    because administrators saw every session; now that nobody does, an ownerless
    session is visible to no one for ever — including one holding a parked agent.
    Handing them to an administrator instead would make deleting a user a way to
    read their work, so each session goes to whoever already had a claim on it:

    * shared with someone by name → to them, by the owner's own earlier decision
    * in a team → the team keeps it, and a remaining member takes ownership
    * neither → destroyed, because the departing user was the only person who
      could ever see it, so there is nothing anyone else loses

    A team session outlives an empty team: `team_id` is kept and the owner left
    null, so it comes back as soon as the team has members again. That is an
    administrator adding someone to a team, which is a deliberate and visible
    act — not the silent read that an admin bypass would be.
    """
    await db.execute(delete(SessionShare).where(SessionShare.user_id == leaving.id))
    await db.execute(delete(TeamMember).where(TeamMember.user_id == leaving.id))
    await _forget_exposure_acks(db, leaving)
    await db.flush()

    destroyed: list[str] = []
    owned = list(await db.scalars(select(Session).where(Session.owner_id == leaving.id)))
    for sess in owned:
        heir = await _session_heir(db, sess, leaving)
        if heir is not None:
            sess.owner_id = heir
            # The new owner must not also hold a share of their own session; it
            # would sit in the sharing list with nothing to switch it off.
            for share in list(sess.shares):
                if share.user_id == heir:
                    sess.shares.remove(share)
                    await db.delete(share)
        elif sess.team_id is not None:
            sess.owner_id = None
        else:
            await _destroy_session(db, sess)
            destroyed.append(sess.id)
    await db.flush()
    return destroyed


async def _forget_exposure_acks(db: AsyncSession, leaving: User) -> None:
    """Erase a departing user from the credential-exposure consents.

    Two directions, both for the reason the share rows above are deleted rather
    than left to a foreign key: SQLite does not enforce ON DELETE and it reuses
    integer ids, so anything still naming this id is a decision lying in wait
    for whoever is created next.

    * Their own acknowledgements go, or the next user to be given this id would
      inherit an agreement they never made and never be asked.
    * Their id comes out of everyone else's, or that same next user would be
      pre-approved as a reader of other people's systems — the exact thing the
      re-arming rule exists to prevent.
    """
    await db.execute(delete(SessionExposureAck).where(SessionExposureAck.user_id == leaving.id))
    rows = list(await db.scalars(select(SessionExposureAck)))
    for row in rows:
        viewers = list(row.viewer_ids or ())
        if leaving.id in viewers:
            row.viewer_ids = [uid for uid in viewers if uid != leaving.id]


async def _session_heir(db: AsyncSession, sess: Session, leaving: User) -> int | None:
    """Who should inherit this session, or None if nobody has a claim on it.

    A named sharee first: the owner decided to give that person this exact
    conversation. Failing that any remaining member of its team, who can see it
    through the team whoever holds it. The lowest user id in both cases, so the
    choice is deterministic rather than whatever order the database happens to
    return, which is what makes it testable.
    """
    shared = sorted(share.user_id for share in sess.shares if share.user_id != leaving.id)
    if shared:
        return shared[0]
    if sess.team_id is None:
        return None
    return await db.scalar(
        select(func.min(TeamMember.user_id)).where(
            TeamMember.team_id == sess.team_id, TeamMember.user_id != leaving.id
        )
    )


async def _destroy_session(db: AsyncSession, sess: Session) -> None:
    """Delete a session nobody else could have seen, and everything under it.

    The same shape as DELETE /api/sessions/{id}: stop the agent first, because
    deleting the row out from under a running process leaves it writing events
    against a session that no longer exists, which fails on the foreign key and
    orphans the subprocess.

    The children are deleted explicitly rather than left to the cascade. Every
    one of those foreign keys is declared ON DELETE CASCADE and Postgres honours
    it, but SQLite does not enforce foreign keys at all unless asked to, and
    nothing here asks — so on a development database the rows would simply stay,
    pointing at a session id that will never resolve.
    """
    active = list(
        await db.scalars(
            select(Run.id).where(
                Run.session_id == sess.id, Run.status.in_(("queued", "running"))
            )
        )
    )
    for run_id in active:
        await runner.cancel(run_id)
    for model in (Event, Approval, Attachment, SessionExposureAck, Run):
        await db.execute(delete(model).where(model.session_id == sess.id))
    await db.delete(sess)


async def _release_schedules(db: AsyncSession, leaving: User) -> None:
    """Delete a departing user's schedules.

    A schedule has no sharing of any kind and the list is owner-scoped, so
    nobody but its owner ever had a claim on one. Left ownerless it would be
    worse than an invisible session: the cron loop keeps firing it, spending an
    account and running commands on this server under a prompt no user can read,
    edit or switch off. Same rule as a session nobody else could see, and for
    the same reason.

    Runs are unpinned first. `runs.schedule_id` is ON DELETE SET NULL, which
    SQLite does not enforce, so a run belonging to a session that gets handed on
    would otherwise keep reporting a schedule id that no longer resolves.
    """
    ids = list(await db.scalars(select(Schedule.id).where(Schedule.owner_id == leaving.id)))
    if not ids:
        return
    await db.execute(update(Run).where(Run.schedule_id.in_(ids)).values(schedule_id=None))
    await db.execute(delete(Schedule).where(Schedule.id.in_(ids)))
    await db.flush()


async def _hand_on_systems(db: AsyncSession, leaving: User) -> None:
    """Pass this user's stored systems, relay nodes and workspaces to someone
    who can manage them.

    Admins get no implicit access to a stored credential, so a system left
    without an owner would be reachable by nobody and removable by nobody. The
    delete is refused rather than orphaning one — and refusing is also what
    stops deleting a user becoming a way to inherit their credentials.
    """
    # Grants this user held on other people's systems, nodes and workspaces. The
    # foreign keys say cascade, and Postgres honours them, but SQLite enforces
    # neither that nor unique ids over time — so a leftover row is access lying
    # in wait for whoever is created next, and access to a stored credential, a
    # route into a network or somebody's checkout is the worst thing to inherit
    # by accident.
    await db.execute(delete(TargetAccess).where(TargetAccess.user_id == leaving.id))
    await db.execute(delete(RelayNodeAccess).where(RelayNodeAccess.user_id == leaving.id))
    await db.execute(delete(WorkspaceAccess).where(WorkspaceAccess.user_id == leaving.id))
    # Relay nodes and workspaces are owned and shared on exactly the same terms,
    # so an ownerless one strands a route into a network, or a directory full of
    # somebody's work, the same way a stored system would. A stranded workspace
    # is worse than invisible: sessions still point at it, and every turn in one
    # would fail with nobody able to grant the access that would fix it.
    owned = (
        list(await db.scalars(select(Target).where(Target.owner_id == leaving.id)))
        + list(await db.scalars(select(RelayNode).where(RelayNode.owner_id == leaving.id)))
        + list(await db.scalars(select(Workspace).where(Workspace.owner_id == leaving.id)))
    )
    stranded: list[str] = []
    for target in owned:
        heir = next((g for g in target.grants if g.level == "manage"), None)
        if heir is None:
            stranded.append(target.name)
            continue
        target.owner_id = heir.user_id
        await db.delete(heir)
    # Nothing above is committed if this fires, so the refusal leaves the user
    # and every grant exactly as they were.
    if stranded:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{leaving.username} owns {len(stranded)} system(s), relay node(s) or "
            f"workspace(s) nobody else can manage: "
            f"{', '.join(sorted(stranded)[:5])}"
            + (" …" if len(stranded) > 5 else "")
            + ". Give someone manage access, or delete them, before removing this user.",
        )
