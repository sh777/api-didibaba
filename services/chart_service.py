"""
TradingView chart image service.
Wraps chart-img.com API to capture TradingView chart screenshots.
"""

import os
import uuid
import json
import platform
import requests
from urllib3 import encode_multipart_formdata

CHART_IMG_API_KEY = os.getenv("CHART_IMG_API_KEY", "AeM9YEI5qF8J7R7cTNkqw9eKr3ifSFti8KyXr4Ee")
TRADINGVIEW_COOKIES_FILE = os.getenv("TRADINGVIEW_COOKIES_FILE", "/tmp/cookies.json")

# Default chart IDs
CHART_ID_DEFAULT = "Pmtyn6fy"
CHART_ID_BTC = "T1SI4Xaq"
CHART_ID_FABIO = "kZfQme6x"


def get_session(force: bool = False) -> dict:
    """Load TradingView session cookies, refreshing if needed."""
    if not force:
        try:
            with open(TRADINGVIEW_COOKIES_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            pass
    return _login()


def _login() -> dict:
    username = os.environ["TRADINGVIEW_USERNAME"]
    password = os.environ["TRADINGVIEW_PASSWORD"]

    payload = {"username": username, "password": password, "remember": "on"}
    body, content_type = encode_multipart_formdata(payload)
    user_agent = f"TWAPI/3.0 ({platform.system()}; {platform.version()}; {platform.release()})"

    resp = requests.post(
        "https://www.tradingview.com/accounts/signin/",
        data=body,
        headers={
            "origin": "https://www.tradingview.com",
            "User-Agent": user_agent,
            "Content-Type": content_type,
            "referer": "https://www.tradingview.com",
        },
    )
    if "error" in resp.text:
        raise RuntimeError(f"TradingView login failed: {resp.text}")

    cookies = resp.cookies.get_dict()
    with open(TRADINGVIEW_COOKIES_FILE, "w") as f:
        json.dump(cookies, f)
    return cookies


def capture_chart(
    symbol: str,
    chart_id: str = CHART_ID_DEFAULT,
    interval: str = "1D",
    width: int = 1920,
    height: int = 1080,
) -> str:
    """
    Capture a TradingView chart screenshot via chart-img.com.

    Returns:
        Path to the saved PNG file in /tmp.
    """
    cookies = get_session()
    sessionid = cookies.get("sessionid", "")
    sessionid_sign = cookies.get("sessionid_sign", "")

    body = {
        "symbol": symbol,
        "interval": interval,
        "resetZoom": True,
        "width": width,
        "height": height,
    }

    resp = requests.post(
        f"https://api.chart-img.com/v2/tradingview/layout-chart/{chart_id}",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHART_IMG_API_KEY}",
            "tradingview-session-id": sessionid,
            "tradingview-session-id-sign": sessionid_sign,
        },
        json=body,
        timeout=30,
    )

    if resp.status_code != 200:
        # Retry once with fresh login
        cookies = get_session(force=True)
        sessionid = cookies.get("sessionid", "")
        sessionid_sign = cookies.get("sessionid_sign", "")
        resp = requests.post(
            f"https://api.chart-img.com/v2/tradingview/layout-chart/{chart_id}",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {CHART_IMG_API_KEY}",
                "tradingview-session-id": sessionid,
                "tradingview-session-id-sign": sessionid_sign,
            },
            json=body,
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"chart-img API error {resp.status_code}: {resp.text}")

    filename = f"chart_{uuid.uuid4().hex[:8]}.png"
    path = f"/tmp/{filename}"
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


def is_exchange_symbol(exchange_id: str, symbol: str) -> bool:
    """Check if a symbol exists on the given exchange via chart-img.com."""
    url = f"https://api.chart-img.com/v3/tradingview/exchange/{exchange_id}?symbol={symbol}"
    try:
        resp = requests.get(url, headers={"x-api-key": CHART_IMG_API_KEY}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "payload" in data:
            return len(data["payload"]) > 0
        return False
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return False
        raise
    except Exception:
        return False
