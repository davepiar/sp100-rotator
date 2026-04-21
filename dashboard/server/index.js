const fs = require("fs");
const os = require("os");
const path = require("path");
const express = require("express");

const CREDS_PATH = path.join(os.homedir(), ".alpaca", "credentials");
const REQUIRED = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL"];

function loadCredentials(file) {
  const text = fs.readFileSync(file, "utf8");
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq === -1) continue;
    const key = line.slice(0, eq).trim();
    const val = line.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
    if (!(key in process.env)) process.env[key] = val;
  }
}

try {
  loadCredentials(CREDS_PATH);
} catch (err) {
  console.error(`[fatal] cannot read credentials at ${CREDS_PATH}: ${err.message}`);
  process.exit(1);
}

for (const k of REQUIRED) {
  if (!process.env[k]) {
    console.error(`[fatal] missing required credential: ${k}`);
    process.exit(1);
  }
}

if (!process.env.ALPACA_BASE_URL.includes("paper-api")) {
  console.error(
    `[fatal] ALPACA_BASE_URL must point at paper-api.alpaca.markets; got ${process.env.ALPACA_BASE_URL}`,
  );
  process.exit(1);
}

const BASE = process.env.ALPACA_BASE_URL.replace(/\/+$/, "");
const HEADERS = {
  "APCA-API-KEY-ID": process.env.ALPACA_API_KEY,
  "APCA-API-SECRET-KEY": process.env.ALPACA_SECRET_KEY,
};

const TARGET_WEIGHTS_PATH = path.resolve(
  __dirname,
  "..",
  "..",
  "state",
  "target_weights.json",
);

function readTargetPlan() {
  try {
    const text = fs.readFileSync(TARGET_WEIGHTS_PATH, "utf8");
    const parsed = JSON.parse(text);
    return {
      asOf: parsed.as_of ?? null,
      sessionDate: parsed.session_date ?? null,
      signedOff: Boolean(parsed.signed_off),
      weights: parsed.positions ?? {},
    };
  } catch (err) {
    return { asOf: null, sessionDate: null, signedOff: false, weights: {}, error: err.code || err.message };
  }
}

async function alpacaGet(pathname) {
  const res = await fetch(`${BASE}${pathname}`, { headers: HEADERS });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`alpaca ${pathname} ${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json();
}

const app = express();

app.use((req, res, next) => {
  const t0 = Date.now();
  res.on("finish", () => {
    console.log(`${req.method} ${req.path} ${res.statusCode} ${Date.now() - t0}ms`);
  });
  next();
});

function normalizePosition(p) {
  return {
    symbol: p.symbol,
    side: p.side,
    qty: Number(p.qty),
    avgEntryPrice: Number(p.avg_entry_price),
    currentPrice: Number(p.current_price),
    marketValue: Number(p.market_value),
    costBasis: Number(p.cost_basis),
    unrealizedPl: Number(p.unrealized_pl),
    unrealizedPlpc: Number(p.unrealized_plpc),
    changeToday: Number(p.change_today),
  };
}

function annotateWithPlan(positions, equity, plan) {
  const weights = plan.weights || {};
  return positions.map((p) => {
    const targetWeightPct = weights[p.symbol];
    const actualWeightPct = equity > 0 ? (p.marketValue / equity) * 100 : 0;
    let status;
    if (Object.keys(weights).length === 0) status = "unknown";
    else if (targetWeightPct == null) status = "exit-next";
    else status = "hold";
    const driftPct =
      targetWeightPct == null ? null : actualWeightPct - targetWeightPct;
    return {
      ...p,
      status,
      targetWeightPct: targetWeightPct ?? null,
      actualWeightPct,
      driftPct,
    };
  });
}

app.get("/api/portfolio", async (_req, res) => {
  try {
    const [account, positions] = await Promise.all([
      alpacaGet("/v2/account"),
      alpacaGet("/v2/positions"),
    ]);
    const cash = Number(account.cash);
    const equity = Number(account.equity);
    const normalized = positions.map(normalizePosition);
    const positionsValue = normalized.reduce(
      (sum, p) => sum + p.marketValue,
      0,
    );
    const plan = readTargetPlan();
    const annotated = annotateWithPlan(normalized, equity, plan);
    annotated.sort((a, b) => b.marketValue - a.marketValue);
    res.json({
      cash,
      positionsValue,
      equity,
      positions: annotated,
      plan: {
        sessionDate: plan.sessionDate,
        asOf: plan.asOf,
        signedOff: plan.signedOff,
        available: Object.keys(plan.weights).length > 0,
      },
      asOf: new Date().toISOString(),
    });
  } catch (err) {
    console.error(`[err] /api/portfolio: ${err.message}`);
    res.status(502).json({ error: err.message });
  }
});

app.use((_req, res) => res.status(404).json({ error: "not found" }));

app.use((err, _req, res, _next) => {
  console.error(`[err] ${err.message}`);
  res.status(500).json({ error: "internal" });
});

const PORT = 8787;
app.listen(PORT, "127.0.0.1", () => {
  const host = new URL(BASE).host;
  console.log(`listening on 127.0.0.1:${PORT} (${host})`);
});
