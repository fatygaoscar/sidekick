"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config.settings import get_settings
from src.sessions.models import init_db
from src.sessions.repository import Repository
from src.sessions.manager import SessionManager
from src.transcription.manager import TranscriptionManager
from src.summarization.manager import SummarizationManager


class AppState:
    """Application state container."""

    repository: Repository
    session_manager: SessionManager
    transcription_manager: TranscriptionManager
    summarization_manager: SummarizationManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager."""
    settings = get_settings()

    # Ensure data directory exists
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    # Initialize database
    await init_db(settings.database_url)

    # Initialize repository
    app.state.repository = Repository(settings.database_url)
    await app.state.repository.init_db()

    # Initialize managers
    app.state.session_manager = SessionManager(app.state.repository)
    app.state.transcription_manager = TranscriptionManager(settings)
    app.state.summarization_manager = SummarizationManager(settings)

    # Try to restore last session
    await app.state.session_manager.restore_session()

    yield

    # Cleanup
    await app.state.transcription_manager.shutdown()
    await app.state.summarization_manager.shutdown()
    await app.state.repository.close()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Sidekick",
        description="Personal Audio Transcription Assistant",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, specify allowed origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    from src.api.routes import sessions, modes, websocket

    app.include_router(sessions.router, prefix="/api", tags=["sessions"])
    app.include_router(modes.router, prefix="/api", tags=["modes"])
    app.include_router(websocket.router, tags=["websocket"])

    # Mount static files for web UI
    web_dir = Path("web")
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    # Health check endpoint
    @app.get("/health")
    async def health_check() -> dict:
        return {
            "status": "healthy",
            "version": "0.1.0",
        }

    # Root redirect to UI
    @app.get("/")
    async def root() -> dict:
        from fastapi.responses import FileResponse

        index_path = web_dir / "index.html"
        if index_path.exists():
            return FileResponse(str(index_path))
        return {"message": "Sidekick API", "docs": "/docs"}

    return app
