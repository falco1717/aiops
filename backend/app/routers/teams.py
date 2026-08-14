from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Session, Team, TeamMember, User
from ..schemas import TeamIn, TeamOut, TeamPatch
from ..security import current_admin, current_user

router = APIRouter(prefix="/api/teams", tags=["teams"])


async def _session_counts(db: AsyncSession) -> dict[int, int]:
    rows = await db.execute(
        select(Session.team_id, func.count(Session.id))
        .where(Session.team_id.isnot(None))
        .group_by(Session.team_id)
    )
    return {team_id: count for team_id, count in rows}


def _out(team: Team, counts: dict[int, int]) -> TeamOut:
    return TeamOut(
        id=team.id,
        name=team.name,
        description=team.description,
        member_ids=sorted(m.user_id for m in team.members),
        session_count=counts.get(team.id, 0),
        created_at=team.created_at,
    )


async def _get(db: AsyncSession, team_id: int) -> Team:
    team = await db.get(Team, team_id)
    if team is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    return team


async def _apply_members(db: AsyncSession, team: Team, member_ids: list[int] | None) -> None:
    if member_ids is None:
        return
    for existing in list(team.members):
        await db.delete(existing)
    team.members = []
    await db.flush()
    for user_id in dict.fromkeys(member_ids):
        if await db.get(User, user_id) is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"There is no user with id {user_id}"
            )
        db.add(TeamMember(team_id=team.id, user_id=user_id))


@router.get("", response_model=list[TeamOut])
async def list_teams(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Everyone's teams for an administrator; your own otherwise.

    Not admin-only, because putting a session into a team is an ordinary user's
    decision and they need to know which teams they are in to make it.
    """
    stmt = select(Team).order_by(Team.name)
    if not user.is_admin:
        stmt = stmt.where(
            Team.id.in_(select(TeamMember.team_id).where(TeamMember.user_id == user.id))
        )
    counts = await _session_counts(db)
    return [_out(team, counts) for team in await db.scalars(stmt)]


@router.post("", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamIn, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    name = payload.name.strip()
    if await db.scalar(select(Team).where(Team.name == name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A team with that name already exists")
    team = Team(name=name, description=payload.description)
    db.add(team)
    await db.commit()
    await db.refresh(team)
    await _apply_members(db, team, payload.member_ids)
    await db.commit()
    await db.refresh(team)
    return _out(team, await _session_counts(db))


@router.patch("/{team_id}", response_model=TeamOut)
async def update_team(
    team_id: int,
    payload: TeamPatch,
    _: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    team = await _get(db, team_id)
    data = payload.model_dump(exclude_unset=True)
    members = data.pop("member_ids", None)
    if data.get("name"):
        clash = await db.scalar(
            select(Team.id).where(Team.name == data["name"].strip(), Team.id != team_id)
        )
        if clash:
            raise HTTPException(status.HTTP_409_CONFLICT, "A team with that name already exists")
        team.name = data["name"].strip()
    if "description" in data:
        team.description = data["description"]
    await _apply_members(db, team, members)
    await db.commit()
    await db.refresh(team)
    return _out(team, await _session_counts(db))


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: int, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    """Delete a team. Its sessions survive, visible to their owners alone."""
    team = await _get(db, team_id)
    # Done here rather than left to the foreign key: SQLite does not enforce ON
    # DELETE, so a session would keep pointing at a team that no longer exists
    # and the members who could see it would neither lose nor keep access
    # predictably.
    await db.execute(update(Session).where(Session.team_id == team_id).values(team_id=None))
    await db.delete(team)
    await db.commit()
