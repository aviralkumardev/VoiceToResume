from pydantic import BaseModel, Field


class StartSessionResponse(BaseModel):
    roomUrl: str = Field(..., description="Daily room URL the browser should join")
    token: str = Field(..., description="Daily meeting token for the browser (non-owner)")
    roomName: str = Field(..., description="Daily room name — the session handle used by /stop")


class StopSessionResponse(BaseModel):
    ok: bool = True
