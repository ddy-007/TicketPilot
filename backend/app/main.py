from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.db import check_checkpoint_database, check_database, create_app_engine
from app.execution.api import router


def create_app(settings: Settings | None = None, *, frontend_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.settings = settings or get_settings()
        engine = create_app_engine(application.state.settings)
        application.state.engine = engine
        try:
            yield
        finally:
            engine.dispose()

    application = FastAPI(title="TicketPilot", version="0.1.0", lifespan=lifespan)
    application.include_router(router)

    bundled = Path(__file__).resolve().parents[1] / "static"
    frontend = frontend_dir or (
        bundled if bundled.is_dir() else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    )
    if (frontend / "index.html").is_file():
        application.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @application.get("/", include_in_schema=False)
        def workbench():
            return FileResponse(
                frontend / "index.html",
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                    "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                },
            )

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse(status_code=422, content={"error": "INVALID_REQUEST"})

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"error": "DATABASE_UNAVAILABLE"})

    @application.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready() -> JSONResponse:
        database_ok = check_database(application.state.engine)
        checkpoint_ok = check_checkpoint_database(application.state.settings)
        return JSONResponse(
            status_code=200 if database_ok and checkpoint_ok else 503,
            content={
                "status": "ready" if database_ok and checkpoint_ok else "not_ready",
                "database": "ok" if database_ok else "error",
                "checkpoint": "ok" if checkpoint_ok else "error",
            },
        )

    return application


app = create_app()
