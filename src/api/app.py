"""FastAPI application factory."""

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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

    print("[Startup] Initializing Sidekick...")

    # Ensure data directory exists
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    # Initialize database
    print("[Startup] Initializing database...")
    await init_db(settings.database_url)

    # Initialize repository
    app.state.repository = Repository(settings.database_url)
    await app.state.repository.init_db()
    print("[Startup] Database initialized")

    # Initialize managers
    app.state.session_manager = SessionManager(app.state.repository)
    app.state.transcription_manager = TranscriptionManager(settings)
    app.state.summarization_manager = SummarizationManager(settings)

    # Transcription model loads on-demand when recording starts (no pre-load)

    # Try to restore last session
    await app.state.session_manager.restore_session()

    print("[Startup] Sidekick ready!")

    yield

    # Cleanup
    print("[Shutdown] Shutting down...")
    await app.state.transcription_manager.shutdown()
    await app.state.summarization_manager.shutdown()
    await app.state.repository.close()
    print("[Shutdown] Complete")


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
    from src.api.routes import sessions, modes, websocket, export

    app.include_router(sessions.router, prefix="/api", tags=["sessions"])
    app.include_router(modes.router, prefix="/api", tags=["modes"])
    app.include_router(export.router, prefix="/api", tags=["export"])
    app.include_router(websocket.router, tags=["websocket"])

    # Mount static files for web UI
    web_dir = Path("web")
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    def _read_cloudflare_public_url() -> str | None:
        path = Path("data/cloudflare.url")
        if not path.exists():
            return None

        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def _static_version() -> str:
        """Return a version string based on the most recently modified static file."""
        static_dirs = [web_dir / "css", web_dir / "js"]
        mtimes = [
            f.stat().st_mtime
            for d in static_dirs if d.exists()
            for f in d.iterdir() if f.is_file()
        ]
        return str(int(max(mtimes))) if mtimes else "0"

    def _serve_html(path: Path, request: Request) -> HTMLResponse:
        content = path.read_text(encoding="utf-8")
        content = re.sub(r"\?v=[^\"']+", f"?v={_static_version()}", content)

        fallback_ws_url = ""
        fallback_api_base = ""
        if request.url.hostname == "go.sidekickgo.app":
            cloudflare_url = _read_cloudflare_public_url()
            if cloudflare_url:
                fallback_api_base = cloudflare_url
                fallback_ws_url = f"{cloudflare_url.replace('https://', 'wss://', 1)}/ws/audio"

        content = content.replace(
            '"__SIDEKICK_WS_URL__"',
            json.dumps(fallback_ws_url),
        )
        content = content.replace(
            '"__SIDEKICK_API_BASE__"',
            json.dumps(fallback_api_base),
        )
        return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})

    # Health check endpoint
    @app.get("/health")
    async def health_check() -> dict:
        return {
            "status": "healthy",
            "version": "0.1.0",
        }

    # Root redirect to UI
    @app.get("/")
    async def root(request: Request):
        index_path = web_dir / "index.html"
        if index_path.exists():
            return _serve_html(index_path, request)
        return {"message": "Sidekick API", "docs": "/docs"}

    # Recordings page
    @app.get("/recordings")
    async def recordings_page(request: Request):
        recordings_path = web_dir / "recordings.html"
        if recordings_path.exists():
            return _serve_html(recordings_path, request)
        return {"message": "Page not found"}, 404

    return app
