from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import AgentPreset, User
from ..providers import PROVIDERS
from ..schemas import PresetIn, PresetOut
from ..security import current_user
from ..services import ValidationError, validate_effort

router = APIRouter(prefix="/api/presets", tags=["presets"])


def _validate(payload: PresetIn) -> None:
    provider = PROVIDERS.get(payload.provider)
    if provider is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown provider {payload.provider!r}. Known: {', '.join(PROVIDERS)}",
        )
    if payload.permission_mode and payload.permission_mode not in provider.permission_modes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{payload.provider} accepts permission modes: "
            f"{', '.join(provider.permission_modes)}",
        )
    if payload.allowed_tools and payload.provider != "claude":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "allowed_tools is only supported for the claude provider"
        )
    try:
        validate_effort(payload.provider, payload.model, payload.effort)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("", response_model=list[PresetOut])
async def list_presets(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(AgentPreset).order_by(AgentPreset.provider, AgentPreset.name))
    return list(rows)


@router.post("", response_model=PresetOut, status_code=status.HTTP_201_CREATED)
async def create_preset(
    payload: PresetIn, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    _validate(payload)
    if await db.scalar(select(AgentPreset).where(AgentPreset.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A preset with that name already exists")
    preset = AgentPreset(**payload.model_dump())
    db.add(preset)
    await db.commit()
    await db.refresh(preset)
    return preset


@router.put("/{preset_id}", response_model=PresetOut)
async def update_preset(
    preset_id: int,
    payload: PresetIn,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate(payload)
    preset = await db.get(AgentPreset, preset_id)
    if preset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    for key, value in payload.model_dump().items():
        setattr(preset, key, value)
    await db.commit()
    await db.refresh(preset)
    return preset


@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: int, _: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    preset = await db.get(AgentPreset, preset_id)
    if preset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Preset not found")
    await db.delete(preset)
    await db.commit()
