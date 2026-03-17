"""
Symbol normalization utilities.
Converts user-supplied ticker strings to TradingView format.
"""

import requests

# Hard-coded overrides
_SYMBOL_OVERRIDES = {
    "NBIS": "NASDAQ:NBIS",
    "BTC": "BINANCE:BTCUSDT",
    "PLTR": "NASDAQ:PLTR",
}

# Top-N cryptos cache (populated lazily)
_CRYPTO_SYMBOLS: set[str] = set()


def _load_crypto_symbols(limit: int = 100) -> set[str]:
    global _CRYPTO_SYMBOLS
    if _CRYPTO_SYMBOLS:
        return _CRYPTO_SYMBOLS
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": limit,
                "page": 1,
                "sparkline": False,
                "api_key": "CG-bXjpukn5qtSFA5Q77EDerE3v",
            },
            timeout=10,
        )
        resp.raise_for_status()
        _CRYPTO_SYMBOLS = {c["symbol"].upper() for c in resp.json()}
    except Exception:
        pass
    return _CRYPTO_SYMBOLS


def normalize(symbol: str) -> str:
    """
    Normalize a raw symbol to a TradingView-compatible exchange:symbol string.

    Supported inputs:
      - Hard-coded overrides (BTC, PLTR, NBIS)
      - Already-qualified (e.g. NASDAQ:AAPL)
      - Chinese A-share codes (6xxxxx → SSE, 0xxxxx/3xxxxx → SZSE)
      - HK codes (≤5 digits)
      - US crypto symbols (checked against CoinGecko top-100)
      - US stocks (returned as-is for TradingView to resolve)
    """
    upper = symbol.upper().strip()

    # Overrides
    if upper in _SYMBOL_OVERRIDES:
        return _SYMBOL_OVERRIDES[upper]

    # Already qualified
    if ":" in upper:
        return upper

    # Strip common suffixes
    clean = upper.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")

    # HK (≤5 digits)
    if len(clean) <= 5 and clean.isdigit():
        return f"HKEX:{clean}"

    # Chinese A-shares (6 digits)
    if len(clean) == 6 and clean.isdigit():
        if clean.startswith("6"):
            return f"SSE:{clean}"
        if clean.startswith(("0", "3")):
            return f"SZSE:{clean}"

    # US / crypto (alpha only)
    if clean.isalpha():
        cryptos = _load_crypto_symbols()
        if clean in cryptos:
            return f"BINANCE:{clean}USD"
        # US stock — query TradingView symbol search to resolve the correct exchange
        exchange = _resolve_us_exchange(clean)
        return f"{exchange}:{clean}"

    # Return as-is and let TradingView decide
    return upper


def _resolve_us_exchange(symbol: str) -> str:
    """Look up exchange for a US stock symbol via TradingView symbol search."""
    try:
        resp = requests.get(
            "https://symbol-search.tradingview.com/symbol_search/",
            params={"text": symbol, "hl": "1", "exchange": "", "lang": "en", "type": "stock", "domain": "production"},
            timeout=5,
        )
        data = resp.json()
        for item in data:
            if item.get("symbol", "").upper() == symbol.upper():
                return item.get("exchange", "NASDAQ")
    except Exception:
        pass
    return "NASDAQ"
