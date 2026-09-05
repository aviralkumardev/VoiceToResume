from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.meeting_room.room_orchestrator import get_orchestrator_instance
from app.meeting_room.routes import router as resume_room_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await get_orchestrator_instance().shutdown()


app = FastAPI(lifespan=lifespan)

allowed_origins = ["http://localhost:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(resume_room_router, tags=["resume-meeting-room"])
