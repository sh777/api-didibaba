"""
/chart router — chart image generation endpoints.
"""

import os
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from services.chart_service import capture_chart, CHART_ID_DEFAULT, CHART_ID_BTC, CHART_ID_FABIO
from utils.symbol import normalize

router = APIRouter()

VALID_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "2h", "4h", "1D", "1W", "1M"}


@router.get(
    "/image",
    summary="Generate a TradingView chart image",
    response_class=FileResponse,
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG chart image"},
        400: {"description": "Invalid parameters"},
        500: {"description": "Chart generation failed"},
    },
)
def get_chart_image(
    symbol: str = Query(..., description="Ticker symbol, e.g. AAPL, 000001, BTC"),
    interval: str = Query("1D", description="Chart interval: 1m 5m 15m 30m 1h 4h 1D 1W 1M"),
    chart_type: str = Query("default", description="Chart type: default | btc | fabio"),
    width: int = Query(1920, ge=400, le=3840, description="Image width in pixels"),
    height: int = Query(1080, ge=300, le=2160, description="Image height in pixels"),
):
    if interval not in VALID_INTERVALS:
        raise HTTPException(status_code=400, detail=f"Invalid interval. Choose from: {', '.join(sorted(VALID_INTERVALS))}")

    chart_id_map = {
        "default": CHART_ID_DEFAULT,
        "btc": CHART_ID_BTC,
        "fabio": CHART_ID_FABIO,
    }
    chart_id = chart_id_map.get(chart_type, CHART_ID_DEFAULT)

    # Auto-select BTC chart if symbol looks like BTC
    tv_symbol = normalize(symbol)
    if "BTC" in tv_symbol.upper() and chart_type == "default":
        chart_id = CHART_ID_BTC

    try:
        path = capture_chart(
            symbol=tv_symbol,
            chart_id=chart_id,
            interval=interval,
            width=width,
            height=height,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return FileResponse(
        path,
        media_type="image/png",
        filename=f"{symbol.upper()}-{interval}.png",
        background=_cleanup(path),
    )


class _cleanup:
    """Background task to delete the temp file after response is sent."""

    def __init__(self, path: str):
        self._path = path

    async def __call__(self):
        try:
            os.remove(self._path)
        except Exception:
            pass
