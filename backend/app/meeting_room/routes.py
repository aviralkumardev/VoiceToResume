from fastapi import APIRouter, Depends, HTTPException

from app.meeting_room.models import StartSessionResponse, StopSessionResponse
from app.meeting_room.room_orchestrator import ResumeRoomOrchestrator, get_orchestrator_instance


router = APIRouter(
    prefix="/resume-room",
    tags=["resume-room"],
    responses={404: {"description": "Not found"}},
)


@router.post("/start", response_model=StartSessionResponse)
async def start_session(
    orchestrator: ResumeRoomOrchestrator = Depends(get_orchestrator_instance)
):
    try:
        return await orchestrator.start_session()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="RESUME ROOM: Failed to start the session.") from exc


@router.post("/stop/{room_name}", response_model=StopSessionResponse)
async def stop_session(
    room_name: str,
    orchestrator: ResumeRoomOrchestrator = Depends(get_orchestrator_instance)
):
    try:
        return await orchestrator.stop_session(room_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="RESUME ROOM: Failed to stop the session.") from exc
