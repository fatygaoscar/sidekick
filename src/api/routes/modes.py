"""Mode management endpoints."""

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.sessions.manager import SessionManager


router = APIRouter()


def get_session_manager(request: Request) -> SessionManager:
    """Dependency to get session manager."""
    return request.app.state.session_manager


class ChangeModeRequest(BaseModel):
    mode: str
    submode: str | None = None


def load_modes_config() -> dict[str, Any]:
    """Load modes configuration from YAML."""
    config_path = Path("config/modes.yaml")
    if not config_path.exists():
        return {"modes": {}, "default_mode": "work", "default_submode": None}

    with open(config_path) as f:
        return yaml.safe_load(f)


@router.get("/modes")
async def get_modes():
    """Get all available modes and submodes."""
    config = load_modes_config()
    return {
        "modes": config.get("modes", {}),
        "default_mode": config.get("default_mode", "work"),
        "default_submode": config.get("default_submode"),
    }


@router.get("/modes/current")
async def get_current_mode(
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Get the current mode and submode."""
    session = session_manager.current_session
    if not session:
        config = load_modes_config()
        return {
            "mode": config.get("default_mode", "work"),
            "submode": config.get("default_submode"),
            "has_session": False,
        }

    return {
        "mode": session.mode,
        "submode": session.submode,
        "has_session": True,
        "session_id": session.id,
    }


@router.post("/modes/change")
async def change_mode(
    request: ChangeModeRequest,
    session_manager: SessionManager = Depends(get_session_manager),
):
    """Change the current mode/submode."""
    config = load_modes_config()
    modes = config.get("modes", {})

    # Validate mode
    if request.mode not in modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {request.mode}. Available: {list(modes.keys())}",
        )

    # Validate submode if provided
    if request.submode:
        submodes = modes[request.mode].get("submodes", {})
        if request.submode not in submodes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid submode: {request.submode}. Available: {list(submodes.keys())}",
            )

    # If no session, start one
    if not session_manager.current_session:
        session = await session_manager.start_session(
            mode=request.mode,
            submode=request.submode,
        )
    else:
        session = await session_manager.change_mode(
            mode=request.mode,
            submode=request.submode,
        )

    return {
        "mode": session.mode,
        "submode": session.submode,
        "session_id": session.id,
    }


@router.get("/modes/{mode_name}")
async def get_mode_details(mode_name: str):
    """Get details for a specific mode."""
    config = load_modes_config()
    modes = config.get("modes", {})

    if mode_name not in modes:
        raise HTTPException(status_code=404, detail=f"Mode not found: {mode_name}")

    return {"name": mode_name, **modes[mode_name]}
