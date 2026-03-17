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
POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "1"))


def get_sessions() -> list[dict]:
    """
    Load TradingView session list from env vars.

    Priority:
    1. TRADINGVIEW_SESSIONS=id1:sign1,id2:sign2,...  (multi-session)
    2. TRADINGVIEW_SESSION_ID + TRADINGVIEW_SESSION_ID_SIGN (single session)
    3. TRADINGVIEW_COOKIES_FILE (legacy JSON file)

    Pool size is automatically set to the number of sessions.
    """
    multi = os.getenv("TRADINGVIEW_SESSIONS", "").strip()
    if multi:
        sessions = []
        for entry in multi.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":", 1)
            if len(parts) != 2:
                logger.warning(f"Skipping malformed session entry: {entry!r}")
                continue
            sessions.append({"sessionid": parts[0], "sessionid_sign": parts[1]})
        if sessions:
            logger.info(f"Loaded {len(sessions)} TradingView sessions from TRADINGVIEW_SESSIONS")
            return sessions

    sessionid = os.getenv("TRADINGVIEW_SESSION_ID")
    sessionid_sign = os.getenv("TRADINGVIEW_SESSION_ID_SIGN")
    if sessionid and sessionid_sign:
        return [{"sessionid": sessionid, "sessionid_sign": sessionid_sign}]

    try:
        with open(TRADINGVIEW_COOKIES_FILE, "r") as f:
            return [json.load(f)]
    except FileNotFoundError:
        raise RuntimeError(
            "No TradingView session found. "
            "Set TRADINGVIEW_SESSIONS or TRADINGVIEW_SESSION_ID env vars."
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
        """
        Initialize the pool. Called once at app startup.
        Pool size = number of sessions in TRADINGVIEW_SESSIONS (or BROWSER_POOL_SIZE fallback).
        Pages are seeded immediately (bare); warm-up runs in background.
        """
        from playwright.async_api import async_playwright

        sessions = get_sessions()
        # If multi-session, pool size = session count; otherwise use BROWSER_POOL_SIZE
        effective_size = len(sessions) if len(sessions) > 1 else self._size
        self._sessions = sessions

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        self._queue = asyncio.Queue()
        for i in range(effective_size):
            # Round-robin assign session to each page
            cookies = sessions[i % len(sessions)]
            page = await self._make_page(cookies=cookies, warm=False)
            await self._queue.put(page)

        logger.info(f"BrowserPool: {effective_size} pages ready ({len(sessions)} session(s)), warming up...")
        asyncio.create_task(self._warm_all())

    async def _warm_all(self):
        """Navigate all pooled pages to TradingView in background."""
        pages = []
        while not self._queue.empty():
            pages.append(await self._queue.get())

        for i, page in enumerate(pages):
            try:
                await page.goto(
                    f"{TV_BASE}/{CHART_ID_DEFAULT}/",
                    wait_until="load",
                    timeout=60000,
                )
                await page.wait_for_selector("canvas", timeout=30000)
                await page.wait_for_timeout(3000)
                logger.info(f"BrowserPool: page {i + 1}/{len(pages)} warmed")
            except Exception as e:
                logger.warning(f"BrowserPool: warm page {i + 1} failed (non-fatal): {e}")
            finally:
                await self._queue.put(page)

        logger.info(f"BrowserPool: all {len(pages)} pages warmed")

    async def stop(self):
        """Cleanup on app shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("BrowserPool stopped")

    async def _make_page(self, cookies: dict, warm: bool = False):
        """Create a new page with given TradingView cookies. Optionally pre-warm."""
        context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        await context.add_cookies([
            {"name": k, "value": v, "domain": ".tradingview.com", "path": "/"}
            for k, v in cookies.items()
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
                    new_page = await self._make_page(cookies=self._sessions[0], warm=False)
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
