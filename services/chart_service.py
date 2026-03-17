"""
TradingView chart service.
Uses TradingView session cookies + Playwright to capture chart screenshots.
"""

import os
import json
import uuid
import asyncio

TRADINGVIEW_COOKIES_FILE = os.getenv("TRADINGVIEW_COOKIES_FILE", "/tmp/cookies.json")

# Default chart layout ID (TradingView saved layout)
CHART_ID_DEFAULT = "Pmtyn6fy"

TV_BASE = "https://www.tradingview.com/chart"

# Semaphore: only 1 Playwright session at a time (same TradingView cookie)
_chart_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _chart_semaphore
    if _chart_semaphore is None:
        _chart_semaphore = asyncio.Semaphore(1)
    return _chart_semaphore


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


def _run_playwright(cookies: dict, url: str, width: int, height: int, path: str) -> None:
    """Synchronous Playwright capture. Called via asyncio.to_thread."""
    from playwright.sync_api import sync_playwright

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

        for attempt in range(3):
            if attempt == 0:
                page.goto(url, wait_until="load", timeout=60000)
            else:
                page.reload(wait_until="load", timeout=60000)

            page.wait_for_selector("canvas", timeout=30000)

            try:
                page.wait_for_function(
                    "() => document.querySelectorAll('canvas').length >= 2",
                    timeout=15000,
                )
            except Exception:
                pass

            page.wait_for_timeout(4000)

            reconnect = page.locator("button:has-text('Reconnect')")
            if reconnect.count() > 0:
                reconnect.first.click()
                page.wait_for_timeout(3000)
                continue

            # No error dialog — let canvas text finish rendering
            page.wait_for_timeout(8000)
            break
        else:
            page.wait_for_timeout(5000)

        page.screenshot(path=path, clip={"x": 0, "y": 0, "width": width, "height": height})
        browser.close()


async def capture_chart_async(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """
    Async wrapper: acquire semaphore then run Playwright in a thread.
    Ensures only 1 active TradingView session at a time.
    """
    cookies = get_session()
    url = f"{TV_BASE}/{chart_id}/?symbol={symbol}&interval={interval}"
    path = f"/tmp/chart_{uuid.uuid4().hex[:8]}.png"

    async with _get_semaphore():
        await asyncio.to_thread(_run_playwright, cookies, url, width, height, path)

    return path


def capture_chart(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Sync entry point (runs event loop if needed). Use capture_chart_async when possible."""
    import asyncio as _asyncio
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context — should not happen for sync endpoints
        # but handle it gracefully
        future = _asyncio.ensure_future(capture_chart_async(symbol, chart_id, interval, width, height))
        return future  # type: ignore
    else:
        return _asyncio.run(capture_chart_async(symbol, chart_id, interval, width, height))
