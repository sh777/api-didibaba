"""
TradingView chart service — persistent browser pool.

Each page stays alive between requests; only the symbol URL changes.
Eliminates cold-start overhead (~24s → ~5s per request after warm-up).
"""

import os
import json
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

TRADINGVIEW_COOKIES_FILE = os.getenv("TRADINGVIEW_COOKIES_FILE", "/tmp/cookies.json")
CHART_ID_DEFAULT = "Pmtyn6fy"
TV_BASE = "https://www.tradingview.com/chart"
POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "3"))


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


class BrowserPool:
    """
    Pool of persistent Playwright browser pages.
    Pages are pre-warmed with TradingView and kept alive between requests.
    Pool size = max concurrent requests (default 3).
    """

    def __init__(self, size: int = 3):
        self._size = size
        self._playwright = None
        self._browser = None
        self._queue: asyncio.Queue | None = None
        self._cookies: dict = {}

    async def start(self):
        """Initialize the pool. Called once at app startup."""
        from playwright.async_api import async_playwright

        self._cookies = get_session()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        self._queue = asyncio.Queue()
        for i in range(self._size):
            page = await self._make_page(warm=True)
            await self._queue.put(page)
            logger.info(f"BrowserPool: page {i + 1}/{self._size} ready")

        logger.info(f"BrowserPool started with {self._size} pages")

    async def stop(self):
        """Cleanup on app shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("BrowserPool stopped")

    async def _make_page(self, warm: bool = False):
        """Create a new page with TradingView cookies. Optionally pre-warm."""
        context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        await context.add_cookies([
            {"name": k, "value": v, "domain": ".tradingview.com", "path": "/"}
            for k, v in self._cookies.items()
        ])
        page = await context.new_page()

        if warm:
            try:
                await page.goto(
                    f"{TV_BASE}/{CHART_ID_DEFAULT}/",
                    wait_until="load",
                    timeout=60000,
                )
                await page.wait_for_selector("canvas", timeout=30000)
                await page.wait_for_timeout(4000)
                logger.info("BrowserPool: page pre-warmed")
            except Exception as e:
                logger.warning(f"BrowserPool: pre-warm failed (non-fatal): {e}")

        return page

    @asynccontextmanager
    async def _acquire(self):
        """Acquire a page, yield it, return it (or replace if crashed)."""
        page = await self._queue.get()
        try:
            yield page
        finally:
            if page.is_closed():
                logger.warning("BrowserPool: page crashed, replacing...")
                try:
                    new_page = await self._make_page(warm=False)
                    await self._queue.put(new_page)
                except Exception as e:
                    logger.error(f"BrowserPool: failed to replace page: {e}")
            else:
                await self._queue.put(page)

    async def capture(
        self,
        symbol: str,
        chart_id: str = CHART_ID_DEFAULT,
        interval: str = "1D",
        width: int = 1920,
        height: int = 1080,
    ) -> str:
        """Capture a chart screenshot using a pooled page."""
        url = f"{TV_BASE}/{chart_id}/?symbol={symbol}&interval={interval}"
        path = f"/tmp/chart_{uuid.uuid4().hex[:8]}.png"

        async with self._acquire() as page:
            # Resize viewport if dimensions changed
            current = page.viewport_size
            if not current or current["width"] != width or current["height"] != height:
                await page.set_viewport_size({"width": width, "height": height})

            # Navigate to the new symbol URL
            await page.goto(url, wait_until="load", timeout=60000)
            await page.wait_for_selector("canvas", timeout=30000)

            # Wait for multiple canvases (price + volume)
            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('canvas').length >= 2",
                    timeout=15000,
                )
            except Exception:
                pass

            # Handle reconnect dialog if TradingView lost connection
            reconnect = page.locator("button:has-text('Reconnect')")
            if await reconnect.count() > 0:
                await reconnect.first.click()
                await page.wait_for_timeout(3000)

            # Short settle wait for rendering
            await page.wait_for_timeout(3000)

            await page.screenshot(
                path=path,
                clip={"x": 0, "y": 0, "width": width, "height": height},
            )

        return path


# Global pool — initialized by app lifespan
_pool: BrowserPool | None = None


def get_pool() -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool(size=POOL_SIZE)
    return _pool


async def capture_chart_async(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """Public entry point: capture chart via persistent browser pool."""
    return await get_pool().capture(symbol, chart_id, interval, width, height)
