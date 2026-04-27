---
name: alpaca-trading
description: Interact with the Alpaca brokerage account via REST API for paper or live trading. Use whenever the user asks to check account balance/equity/buying power, list positions or orders, place/cancel orders (market, limit, stop), get quotes/bars, or perform any Alpaca trading operation. Triggers on mentions of "alpaca", "my brokerage", "paper account", "place a trade", "buy/sell shares".
---

# Alpaca Trading Skill

Use the Alpaca REST API directly via `curl` (the alpaca-mcp-server has a Windows Unicode bug, so do NOT use it).

## Credentials

Load credentials by sourcing `C:\Users\dpineda\.alpaca\credentials` (a key=value file, user-only ACL). It defines `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_BASE_URL`, `ALPACA_DATA_URL`.

In bash:
```bash
set -a; . /c/Users/dpineda/.alpaca/credentials; set +a
```

Never echo `ALPACA_SECRET_KEY` to the user or include it in any output.

Default to **paper trading**:
- Trading base: `https://paper-api.alpaca.markets/v2`
- Data base: `https://data.alpaca.markets/v2`

Switch to live (`https://api.alpaca.markets/v2`) ONLY if the user explicitly says "live account".

## Required headers

```
APCA-API-KEY-ID: $ALPACA_API_KEY
APCA-API-SECRET-KEY: $ALPACA_SECRET_KEY
```

For POST/PATCH also add `Content-Type: application/json`.

## Safety rules

1. **Always confirm before placing, replacing, or canceling orders.** Show symbol, side, qty/notional, type, limit/stop price, and TIF, then wait for explicit "yes".
2. Never place an order on a live account without a second confirmation.
3. Never expose the secret key in output.
4. Default `time_in_force` to `day` for stocks, `gtc` for crypto, unless specified.

## Common endpoints

| Action | Method | Path |
|---|---|---|
| Account info / balance | GET | `/account` |
| List positions | GET | `/positions` |
| Position for symbol | GET | `/positions/{symbol}` |
| Close all positions | DELETE | `/positions?cancel_orders=true` |
| Close one position | DELETE | `/positions/{symbol}` |
| List orders | GET | `/orders?status=all&limit=50` |
| Get order | GET | `/orders/{id}` |
| Place order | POST | `/orders` |
| Replace order | PATCH | `/orders/{id}` |
| Cancel order | DELETE | `/orders/{id}` |
| Cancel all orders | DELETE | `/orders` |
| Assets list | GET | `/assets?status=active` |
| Clock | GET | `/clock` |
| Calendar | GET | `/calendar` |
| Latest quote (stock) | GET | `data/v2/stocks/{symbol}/quotes/latest` |
| Latest trade (stock) | GET | `data/v2/stocks/{symbol}/trades/latest` |
| Historical bars | GET | `data/v2/stocks/{symbol}/bars?timeframe=1Day&start=...` |
| Latest crypto quote | GET | `data/v1beta3/crypto/us/latest/quotes?symbols=BTC/USD` |

## Order body (POST /orders)

```json
{
  "symbol": "AAPL",
  "qty": "10",                  // OR "notional": "1000"
  "side": "buy",                 // buy | sell
  "type": "market",              // market | limit | stop | stop_limit | trailing_stop
  "time_in_force": "day",        // day | gtc | opg | cls | ioc | fok
  "limit_price": "150.00",       // required for limit / stop_limit
  "stop_price": "145.00",        // required for stop / stop_limit
  "trail_price": "1.50",         // OR "trail_percent" for trailing_stop
  "extended_hours": false,
  "client_order_id": "optional-uuid",
  "order_class": "simple",       // simple | bracket | oco | oto
  "take_profit": { "limit_price": "160.00" },
  "stop_loss":   { "stop_price": "140.00", "limit_price": "139.50" }
}
```

## Curl recipes

Get account:
```bash
curl -s -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" \
  https://paper-api.alpaca.markets/v2/account
```

Place market buy:
```bash
curl -s -X POST -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","qty":"1","side":"buy","type":"market","time_in_force":"day"}' \
  https://paper-api.alpaca.markets/v2/orders
```

Place limit sell:
```bash
curl -s -X POST -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","qty":"1","side":"sell","type":"limit","limit_price":"200","time_in_force":"gtc"}' \
  https://paper-api.alpaca.markets/v2/orders
```

Cancel order:
```bash
curl -s -X DELETE -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" \
  https://paper-api.alpaca.markets/v2/orders/<ORDER_ID>
```

Latest AAPL quote:
```bash
curl -s -H "APCA-API-KEY-ID: $K" -H "APCA-API-SECRET-KEY: $S" \
  https://data.alpaca.markets/v2/stocks/AAPL/quotes/latest
```

## Output style

When showing account info or orders, format as a clean Markdown table. Always state whether the account is **paper** or **live** at the top.

If a request fails (4xx/5xx), surface the JSON `message` field and suggest a fix (bad symbol, market closed, insufficient buying power, etc.).
