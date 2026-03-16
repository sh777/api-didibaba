# api.didibaba.ai

Backend API service for [didibaba.ai](https://didibaba.ai).

## Endpoints

### `GET /health`
Health check.

### `GET /chart/image`
Generate a TradingView chart screenshot via [chart-img.com](https://chart-img.com).

**Query parameters:**

| Parameter    | Default   | Description                                      |
|-------------|-----------|--------------------------------------------------|
| `symbol`    | required  | Ticker: `AAPL`, `000001`, `BTC`, `NASDAQ:TSLA`  |
| `interval`  | `1D`      | `1m` `5m` `15m` `30m` `1h` `4h` `1D` `1W` `1M` |
| `chart_type`| `default` | `default` \| `btc` \| `fabio`                   |
| `width`     | `1920`    | Image width (400–3840)                           |
| `height`    | `1080`    | Image height (300–2160)                          |

**Example:**
```
GET /chart/image?symbol=AAPL&interval=1D
GET /chart/image?symbol=000001&interval=1W
GET /chart/image?symbol=BTC&chart_type=btc
```

## Setup

```bash
cp .env.example .env
# Fill in TRADINGVIEW_USERNAME and TRADINGVIEW_PASSWORD

pip install -r requirements.txt
uvicorn main:app --reload
```

## Docker

```bash
docker build -t api-didibaba .
docker run -p 8000:8000 --env-file .env api-didibaba
```

## API Docs

Visit `http://localhost:8000/docs` for the interactive Swagger UI.

## Structure

```
├── main.py                  # FastAPI app + CORS
├── routers/
│   └── chart.py             # /chart endpoints
├── services/
│   └── chart_service.py     # chart-img.com + TradingView session
└── utils/
    └── symbol.py            # Symbol normalization
```
