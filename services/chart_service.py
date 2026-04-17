"""
TradingView chart service — persistent browser pool.

Uses window.TradingViewApi.takeClientScreenshot() (same as the camera button)
to produce a clean canvas-only PNG — no UI chrome, no toolbar, no watermarks.
"""

import os
import uuid
import base64
import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHART_ID_DEFAULT = "Pmtyn6fy"
TV_BASE = "https://www.tradingview.com/chart"
POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "1"))
PAGE_RECYCLE_AFTER = int(os.getenv("PAGE_RECYCLE_AFTER", "100"))


def get_sessions() -> list[dict]:
    """Load TradingView session credentials from env vars."""
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

    raise RuntimeError(
        "No TradingView session found. "
        "Set TRADINGVIEW_SESSIONS or TRADINGVIEW_SESSION_ID env vars."
    )


@dataclass
class PooledPage:
    page: object
    cookies: dict
    request_count: int = field(default=0)


class BrowserPool:
    def __init__(self, size: int = 1):
        self._size = size
        self._playwright = None
        self._browser = None
        self._queue: asyncio.Queue | None = None
        self._sessions: list[dict] = []

    async def start(self):
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
        logger.info(f"BrowserPool: {effective_size} pages ready, warming up...")
        asyncio.create_task(self._warm_all())

    async def _warm_all(self):
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
                await entry.page.wait_for_function(
                    "() => typeof window.TradingViewApi?.takeClientScreenshot === 'function'",
                    timeout=20000,
                )
                await entry.page.wait_for_timeout(3000)
                logger.info(f"BrowserPool: page {i + 1}/{len(entries)} warmed")
            except Exception as e:
                logger.warning(f"BrowserPool: warm page {i + 1} failed: {e}")
            finally:
                await self._queue.put(entry)
        logger.info(f"BrowserPool: all {len(entries)} pages warmed")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("BrowserPool stopped")

    async def _make_entry(self, cookies: dict) -> PooledPage:
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
        logger.info(f"BrowserPool: recycling page after {entry.request_count} requests")
        try:
            await entry.page.context.close()
        except Exception as e:
            logger.warning(f"BrowserPool: error closing old context: {e}")
        return await self._make_entry(cookies=entry.cookies)

    @asynccontextmanager
    async def _acquire(self):
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
                asyncio.create_task(self._recycle_and_return(entry))
            else:
                await self._queue.put(entry)

    async def _recycle_and_return(self, entry: PooledPage):
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
        # Load the chart WITHOUT symbol param first (use default), then switch via API.
        # This avoids the race where initial paint uses default symbol and studies
        # get stuck on "error in series" when a fast URL-param switch happens.
        url = f"{TV_BASE}/{chart_id}/"
        path = f"/tmp/chart_{uuid.uuid4().hex[:8]}.png"

        async with self._acquire() as page:
            current = page.viewport_size
            if not current or current["width"] != width or current["height"] != height:
                await page.set_viewport_size({"width": width, "height": height})

            await page.goto(url, wait_until="load", timeout=60000)
            await page.wait_for_selector("canvas", timeout=30000)

            await page.wait_for_function(
                "() => typeof window.TradingViewApi?.takeClientScreenshot === 'function' && typeof window.TradingViewApi?.activeChart === 'function'",
                timeout=20000,
            )
            # Let the default chart fully load first
            await page.wait_for_timeout(2500)

            # Now switch symbol + interval via the official widget API and wait for series.
            # onDataLoaded() fires once after the new series has been painted.
            await page.evaluate(
                """async ({symbol, interval}) => {
                    const chart = window.TradingViewApi.activeChart();
                    await new Promise((resolve) => {
                        let done = false;
                        const finish = () => { if (!done) { done = true; resolve(); } };
                        try {
                            chart.onDataLoaded().subscribe(null, finish);
                        } catch (_) {}
                        // Safety timeout so we always resolve
                        setTimeout(finish, 12000);
                        chart.setSymbol(symbol, interval, () => {});
                    });
                }""",
                {"symbol": symbol, "interval": interval},
            )

            # Extra settle for study recomputation after the new series arrives
            await page.wait_for_timeout(3500)

            # Native clean screenshot
            data_url: str = await page.evaluate(
                """async () => {
                    const canvas = await window.TradingViewApi.takeClientScreenshot();
                    return canvas.toDataURL('image/png');
                }"""
            )

        # Strip "data:image/png;base64," prefix and decode
        b64 = data_url.split(",", 1)[1]
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))

        logger.info(f"Chart saved via takeClientScreenshot: {path}")
        return path


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
    return await get_pool().capture(symbol, chart_id, interval, width, height)
