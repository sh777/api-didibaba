"""
api.didibaba.ai — Main FastAPI entrypoint
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routers import chart

app = FastAPI(
    title="didibaba API",
    description="API services for didibaba.ai",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://didibaba.ai", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chart.router, prefix="/chart", tags=["chart"])


@app.get("/health")
def health():
    return {"status": "ok"}
