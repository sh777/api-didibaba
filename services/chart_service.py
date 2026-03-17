"""
TradingView chart service.
Uses TradingView session cookies + Playwright to capture chart screenshots.
"""

import os
import json
import uuid
import threading

TRADINGVIEW_COOKIES_FILE = os.getenv("TRADINGVIEW_COOKIES_FILE", "/tmp/cookies.json")

# Default chart layout ID (TradingView saved layout)
CHART_ID_DEFAULT = "Pmtyn6fy"

TV_BASE = "https://www.tradingview.com/chart"

# Serialize requests within this process
_chart_lock = threading.Lock()


def get_session() -> dict:
    """Load TradingView session cookies from env vars or cached file."""
    sessionid = os.getenv("TRADINGVIEW_SESSION_ID")
    sessionid_sign = os.getenv("TRADINGVIEW_SESSION_ID_SIGN")

    if sessionid and sessionid_sign:
        return {"sessionid": sessionid, "sessionid_sign": sessionid_sign}

    try:
        with open(TRADINGVIEW_COOKIES_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        raise RuntimeError(
            "No TradingView session found. "
            "Set TRADINGVIEW_SESSION_ID and TRADINGVIEW_SESSION_ID_SIGN env vars."
        )


def _load_chart(page, url: str, width: int, height: int) -> None:
    """Load TradingView chart and wait for full render, retrying on disconnect errors."""
    for attempt in range(3):
        if attempt == 0:
            page.goto(url, wait_until="load", timeout=60000)
        else:
            page.reload(wait_until="load", timeout=60000)

        # Wait for canvas
        page.wait_for_selector("canvas", timeout=30000)

        # Wait for multiple canvases (main chart + price axis)
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('canvas').length >= 2",
                timeout=15000,
            )
        except Exception:
            pass

        # Check for "Something went wrong" error dialog
        page.wait_for_timeout(4000)
        reconnect = page.locator("button:has-text('Reconnect')")
        if reconnect.count() > 0:
            reconnect.first.click()
            page.wait_for_timeout(3000)
            # Will retry on next iteration
            continue

        # No error — wait for canvas text to fully render then done
        page.wait_for_timeout(8000)
        return

    # After 3 attempts still erroring — take screenshot anyway (may be partial)
    page.wait_for_timeout(5000)


def capture_chart(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """
    Capture a TradingView chart screenshot using Playwright + session cookies.

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

            context.add_cookies([
                {"name": k, "value": v, "domain": ".tradingview.com", "path": "/"}
                for k, v in cookies.items()
            ])

            page = context.new_page()
            _load_chart(page, url, width, height)

            page.screenshot(path=path, clip={"x": 0, "y": 0, "width": width, "height": height})
            browser.close()

    return path
