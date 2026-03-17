"""
api.didibaba.ai — Main FastAPI entrypoint
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from routers import chart
import logging
import os

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start browser pool on startup, stop on shutdown."""
    from services.chart_service import get_pool
    pool = get_pool()
    logger.info("Starting browser pool...")
    await pool.start()
    yield
    logger.info("Stopping browser pool...")
    await pool.stop()


app = FastAPI(
    title="didibaba API",
    description="API services for didibaba.ai",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://didibaba.ai", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chart.router, prefix="/chart", tags=["chart"])

# Serve static docs at root
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(_static_dir, "index.html"))


@app.get("/health")
def health():
    return {"status": "ok"}
