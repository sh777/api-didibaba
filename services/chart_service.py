"""
TradingView chart service.
Uses TradingView session cookies + Playwright to capture chart screenshots.
"""

import os
import json
import uuid
import threading

TRADINGVIEW_COOKIES_FILE = os.getenv("TRADINGVIEW_COOKIES_FILE", "/tmp/cookies.json")

# Default chart layout IDs (TradingView saved layouts)
CHART_ID_DEFAULT = "Pmtyn6fy"

TV_BASE = "https://www.tradingview.com/chart"

# Global lock: TradingView rejects concurrent sessions with the same cookie
_chart_lock = threading.Lock()


def get_session() -> dict:
    """Load TradingView session cookies from env vars or cached file."""
    sessionid = os.getenv("TRADINGVIEW_SESSION_ID")
    sessionid_sign = os.getenv("TRADINGVIEW_SESSION_ID_SIGN")

    if sessionid and sessionid_sign:
        return {"sessionid": sessionid, "sessionid_sign": sessionid_sign}

    # Fallback: load from cached file
    try:
        with open(TRADINGVIEW_COOKIES_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            "No TradingView session found. "
            "Set TRADINGVIEW_SESSION_ID and TRADINGVIEW_SESSION_ID_SIGN env vars."
        )


def capture_chart(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """
    Capture a TradingView chart screenshot using Playwright + session cookies.
    Serialized via a global lock to prevent TradingView session conflicts.

    Returns:
        Path to the saved PNG file in /tmp.
    """
    from playwright.sync_api import sync_playwright

    with _chart_lock:
        cookies = get_session()
        url = f"{TV_BASE}/{chart_id}/?symbol={symbol}&interval={interval}"
        path = f"/tmp/chart_{uuid.uuid4().hex[:8]}.png"

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(viewport={"width": width, "height": height})

            # Inject TradingView session cookies
            context.add_cookies([
                {"name": k, "value": v, "domain": ".tradingview.com", "path": "/"}
                for k, v in cookies.items()
            ])

            page = context.new_page()
            page.goto(url, wait_until="load", timeout=60000)

            # Wait for chart canvas to appear
            page.wait_for_selector("canvas", timeout=30000)

            # Wait for multiple canvases (price axis + chart area)
            try:
                page.wait_for_function(
                    """() => document.querySelectorAll('canvas').length >= 2""",
                    timeout=15000,
                )
            except Exception:
                pass

            page.wait_for_timeout(10000)  # let all canvas text render

            page.screenshot(path=path, clip={"x": 0, "y": 0, "width": width, "height": height})
            browser.close()

    return path
