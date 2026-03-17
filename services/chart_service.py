"""
TradingView chart service — persistent browser pool.

Each page stays alive between requests; only the symbol URL changes.
Eliminates cold-start overhead (~24s → ~5s per request after warm-up).
Pages are recycled every PAGE_RECYCLE_AFTER requests to prevent memory leaks.
"""

import os
import json
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

TRADINGVIEW_COOKIES_FILE = os.getenv("TRADINGVIEW_COOKIES_FILE", "/tmp/cookies.json")
CHART_ID_DEFAULT = "Pmtyn6fy"
TV_BASE = "https://www.tradingview.com/chart"
POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "1"))
PAGE_RECYCLE_AFTER = int(os.getenv("PAGE_RECYCLE_AFTER", "100"))


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


@dataclass
class PooledPage:
    """Wraps a Playwright page with metadata for lifecycle management."""
    page: object
    cookies: dict
    request_count: int = field(default=0)


class BrowserPool:
    """
    Pool of persistent Playwright browser pages.
    Each page is recycled after PAGE_RECYCLE_AFTER requests to prevent memory growth.
    Pool size = number of sessions (fully parallel) or BROWSER_POOL_SIZE.
    """

    def __init__(self, size: int = 1):
        self._size = size
        self._playwright = None
        self._browser = None
        self._queue: asyncio.Queue | None = None
        self._sessions: list[dict] = []

    async def start(self):
        """
        Initialize the pool. Called once at app startup.
        Pages seeded immediately (bare); warm-up runs in background.
        """
        from playwright.async_api import async_playwright

        sessions = get_sessions()
        effective_size = len(sessions) if len(sessions) > 1 else self._size
        self._sessions = sessions

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        self._queue = asyncio.Queue()
        for i in range(effective_size):
            cookies = sessions[i % len(sessions)]
            entry = await self._make_entry(cookies=cookies)
            await self._queue.put(entry)

        logger.info(f"BrowserPool: {effective_size} pages ready ({len(sessions)} session(s)), warming up...")
        asyncio.create_task(self._warm_all())

    async def _warm_all(self):
        """Navigate all pooled pages to TradingView in background."""
        entries = []
        while not self._queue.empty():
            entries.append(await self._queue.get())

        for i, entry in enumerate(entries):
            try:
                await entry.page.goto(
                    f"{TV_BASE}/{CHART_ID_DEFAULT}/",
                    wait_until="load",
                    timeout=60000,
                )
                await entry.page.wait_for_selector("canvas", timeout=30000)
                await entry.page.wait_for_timeout(3000)
                logger.info(f"BrowserPool: page {i + 1}/{len(entries)} warmed")
            except Exception as e:
                logger.warning(f"BrowserPool: warm page {i + 1} failed (non-fatal): {e}")
            finally:
                await self._queue.put(entry)

        logger.info(f"BrowserPool: all {len(entries)} pages warmed")

    async def stop(self):
        """Cleanup on app shutdown."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("BrowserPool stopped")

    async def _make_entry(self, cookies: dict) -> PooledPage:
        """Create a fresh PooledPage with its own browser context."""
        context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        await context.add_cookies([
            {"name": k, "value": v, "domain": ".tradingview.com", "path": "/"}
            for k, v in cookies.items()
        ])
        page = await context.new_page()
        return PooledPage(page=page, cookies=cookies)

    async def _recycle(self, entry: PooledPage) -> PooledPage:
        """Close old context and create a fresh one with the same session."""
        logger.info(f"BrowserPool: recycling page after {entry.request_count} requests")
        try:
            await entry.page.context.close()
        except Exception as e:
            logger.warning(f"BrowserPool: error closing old context: {e}")
        return await self._make_entry(cookies=entry.cookies)

    @asynccontextmanager
    async def _acquire(self):
        """Acquire a PooledPage, yield its page, then return/recycle it."""
        entry = await self._queue.get()
        try:
            yield entry.page
            entry.request_count += 1
        finally:
            if entry.page.is_closed():
                logger.warning("BrowserPool: page crashed, replacing...")
                try:
                    new_entry = await self._make_entry(cookies=entry.cookies)
                    await self._queue.put(new_entry)
                except Exception as e:
                    logger.error(f"BrowserPool: failed to replace page: {e}")
            elif entry.request_count >= PAGE_RECYCLE_AFTER:
                # Recycle in background so we don't block returning the slot
                asyncio.create_task(self._recycle_and_return(entry))
            else:
                await self._queue.put(entry)

    async def _recycle_and_return(self, entry: PooledPage):
        """Recycle a page context and put fresh entry back in queue."""
        new_entry = await self._recycle(entry)
        await self._queue.put(new_entry)

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
            current = page.viewport_size
            if not current or current["width"] != width or current["height"] != height:
                await page.set_viewport_size({"width": width, "height": height})

            await page.goto(url, wait_until="load", timeout=60000)
            await page.wait_for_selector("canvas", timeout=30000)

            try:
                await page.wait_for_function(
                    "() => document.querySelectorAll('canvas').length >= 2",
                    timeout=15000,
                )
            except Exception:
                pass

            reconnect = page.locator("button:has-text('Reconnect')")
            if await reconnect.count() > 0:
                await reconnect.first.click()
                await page.wait_for_timeout(3000)

            # Settle wait — also lets transient overlays (login status toasts) disappear
            await page.wait_for_timeout(5000)

            # Measure toolbar height BEFORE hiding (display:none returns 0)
            top_offset = await page.evaluate("""
                () => {
                    const el = document.querySelector('.layout__area--top');
                    return el ? Math.ceil(el.getBoundingClientRect().height) : 38;
                }
            """) or 38

            # Now hide it
            await page.evaluate("""
                () => {
                    const el = document.querySelector('.layout__area--top');
                    if (el) el.style.setProperty('display', 'none', 'important');
                }
            """)

            await page.screenshot(
                path=path,
                clip={"x": 0, "y": top_offset, "width": width, "height": height - top_offset},
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

