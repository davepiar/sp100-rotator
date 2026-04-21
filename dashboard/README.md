# SP100 Paper Dashboard

Local-only web dashboard that shows three live numbers from the Alpaca paper
account: cash available, money in open positions, and total equity. Refreshes
every 15 seconds.

## One-time setup

```bash
cd dashboard/server && npm install
cd ../web && npm install
```

## Run (two terminals)

Terminal 1 — backend:
```bash
cd dashboard/server && npm start
```
It reads `~/.alpaca/credentials`, refuses to start unless `ALPACA_BASE_URL`
points at `paper-api`, and listens on `127.0.0.1:8787`.

Terminal 2 — frontend:
```bash
cd dashboard/web && npm run dev
```
Open http://localhost:5173. Vite proxies `/api/*` to the backend.

## Endpoint

`GET /api/portfolio` → `{ cash, positionsValue, equity, asOf }` (numbers, ISO
timestamp). Backend fetches `/v2/account` and `/v2/positions` in parallel from
Alpaca and never exposes credentials to the browser.
