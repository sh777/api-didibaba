"""
TradingView chart service — via chart-img.com API.

Uses chart-img.com /v2/tradingview/layout-chart/{chart_id} to render
a clean chart image with user-defined shared layout (indicators etc.)
without any TradingView UI chrome.
"""

import os
import uuid
import logging
import httpx

logger = logging.getLogger(__name__)

CHART_IMG_API_KEY = os.getenv("CHART_IMG_API_KEY", "AeM9YEI5qF8J7R7cTNkqw9eKr3ifSFti8KyXr4Ee")
CHART_IMG_BASE = "https://api.chart-img.com"
CHART_ID_DEFAULT = "Pmtyn6fy"

# chart-img free plan: 60 req/min
_TIMEOUT = 60.0


async def capture_chart_async(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1200,
    height: int = 700,
) -> str:
    """
    Capture a chart screenshot via chart-img.com layout-chart API.
    Returns path to a temporary PNG file.
    """
    url = f"{CHART_IMG_BASE}/v2/tradingview/layout-chart/{chart_id}"
    payload = {
        "symbol": symbol,
        "interval": interval,
        "resetZoom": True,
        "width": width,
        "height": height,
    }
    headers = {
        "Authorization": f"Bearer {CHART_IMG_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        raise RuntimeError(
            f"chart-img API error {resp.status_code}: {resp.text[:200]}"
        )

    path = f"/tmp/chart_{uuid.uuid4().hex[:8]}.png"
    with open(path, "wb") as f:
        f.write(resp.content)

    logger.info(f"Chart saved: {path} ({len(resp.content)} bytes)")
    return path
