import { useEffect, useRef, useState } from "react";

const REFRESH_MS = 15_000;
const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});
const qtyFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 });
const pctFmt = new Intl.NumberFormat("en-US", {
  style: "percent",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "always",
});
const signedUsd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
  signDisplay: "always",
});
const weightFmt = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const driftFmt = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "exceptZero",
});

function formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString();
}

function plClass(n) {
  if (n == null || Number.isNaN(n) || n === 0) return "pl-flat";
  return n > 0 ? "pl-up" : "pl-down";
}

function driftClass(n) {
  if (n == null || Number.isNaN(n)) return "pl-flat";
  if (Math.abs(n) < 0.25) return "pl-flat";
  return n > 0 ? "drift-over" : "drift-under";
}

function StatusBadge({ status }) {
  if (status === "hold") {
    return <span className="status status-hold">Hold</span>;
  }
  if (status === "exit-next") {
    return <span className="status status-exit">Exit next</span>;
  }
  return <span className="status status-unknown">—</span>;
}

const MCAP_LABELS = {
  mega: "Mega cap",
  large: "Large cap",
  mid: "Mid cap",
  small: "Small cap",
};

function SymbolCell({ row }) {
  const { symbol, name, sector, mcapTier, exchange } = row;
  return (
    <div className="sym-cell">
      <span className="sym">{symbol}</span>
      {row.side && row.side !== "long" && row.side !== "buy" && row.side !== "sell" && (
        <span className="side">{row.side}</span>
      )}
      <div className="sym-popover" role="tooltip">
        <div className="pop-header">
          <span className="pop-sym">{symbol}</span>
          {exchange && <span className="pop-exchange">{exchange}</span>}
        </div>
        <div className="pop-name">{name || "—"}</div>
        <dl className="pop-meta">
          <div>
            <dt>Sector</dt>
            <dd>{sector || "—"}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd>{MCAP_LABELS[mcapTier] || mcapTier || "—"}</dd>
          </div>
        </dl>
      </div>
    </div>
  );
}

function SideBadge({ side }) {
  const kind = side === "buy" ? "buy" : side === "sell" ? "sell" : "other";
  return (
    <span className={`side-badge side-${kind}`}>{side?.toUpperCase() || "—"}</span>
  );
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function OrdersTable({ orders }) {
  if (!orders) return null;
  if (orders.length === 0) {
    return <div className="empty">No pending orders.</div>;
  }
  return (
    <div className="table-wrap">
      <table className="positions">
        <thead>
          <tr>
            <th>Side</th>
            <th className="col-sym">Symbol</th>
            <th className="num">Qty</th>
            <th>Type</th>
            <th className="num">Limit / Stop</th>
            <th className="num" title="Qty × limit (or stop) price">
              Est. cost
            </th>
            <th>TIF</th>
            <th>Placed</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => {
            const price = o.limitPrice ?? o.stopPrice;
            return (
              <tr key={o.id}>
                <td>
                  <SideBadge side={o.side} />
                </td>
                <td className="col-sym">
                  <SymbolCell row={o} />
                </td>
                <td className="num">
                  {o.remainingQty != null ? qtyFmt.format(o.remainingQty) : "—"}
                </td>
                <td className="type-cell">{o.type}</td>
                <td className="num">{price == null ? "—" : usd.format(price)}</td>
                <td className="num">
                  {o.estCost == null ? "—" : usd.format(o.estCost)}
                </td>
                <td className="tif-cell">{o.timeInForce}</td>
                <td className="muted">{formatDateTime(o.submittedAt)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Card({ label, value, loading }) {
  return (
    <div className="card">
      <div className="card-label">{label}</div>
      <div className={`card-value ${loading ? "loading" : ""}`}>
        {value == null ? "—" : usd.format(value)}
      </div>
    </div>
  );
}

function PositionsTable({ positions }) {
  if (!positions) return null;
  if (positions.length === 0) {
    return <div className="empty">No open positions.</div>;
  }
  return (
    <div className="table-wrap">
      <table className="positions">
        <thead>
          <tr>
            <th className="col-sym">Symbol</th>
            <th className="num">Qty</th>
            <th className="num">Avg entry</th>
            <th className="num">Price</th>
            <th className="num">Market value</th>
            <th className="num">Unrealized P/L</th>
            <th className="num">%</th>
            <th className="num">Today</th>
            <th>Status</th>
            <th className="num" title="Planned weight from target_weights.json">
              Target
            </th>
            <th className="num" title="Current market_value / equity">
              Actual
            </th>
            <th className="num" title="Actual − Target (pp). Closer to 0 = on plan.">
              Drift
            </th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.symbol} className={p.status === "exit-next" ? "row-exit" : ""}>
              <td className="col-sym">
                <SymbolCell row={p} />
              </td>
              <td className="num">{qtyFmt.format(p.qty)}</td>
              <td className="num">{usd.format(p.avgEntryPrice)}</td>
              <td className="num">{usd.format(p.currentPrice)}</td>
              <td className="num">{usd.format(p.marketValue)}</td>
              <td className={`num ${plClass(p.unrealizedPl)}`}>
                {signedUsd.format(p.unrealizedPl)}
              </td>
              <td className={`num ${plClass(p.unrealizedPlpc)}`}>
                {pctFmt.format(p.unrealizedPlpc)}
              </td>
              <td className={`num ${plClass(p.changeToday)}`}>
                {pctFmt.format(p.changeToday)}
              </td>
              <td>
                <StatusBadge status={p.status} />
              </td>
              <td className="num">
                {p.targetWeightPct == null
                  ? "—"
                  : `${weightFmt.format(p.targetWeightPct)}%`}
              </td>
              <td className="num">{`${weightFmt.format(p.actualWeightPct)}%`}</td>
              <td className={`num ${driftClass(p.driftPct)}`}>
                {p.driftPct == null ? "—" : `${driftFmt.format(p.driftPct)}pp`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const abortRef = useRef(null);

  async function fetchPortfolio() {
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      const res = await fetch("/api/portfolio", { signal: ctrl.signal });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
      setData(body);
      setError(null);
    } catch (err) {
      if (err.name === "AbortError") return;
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchPortfolio();
    const id = setInterval(fetchPortfolio, REFRESH_MS);
    return () => {
      clearInterval(id);
      abortRef.current?.abort();
    };
  }, []);

  return (
    <div className="app">
      <header>
        <h1>SP100 Paper Account</h1>
        <span className="badge">paper</span>
      </header>

      <section className="cards">
        <Card label="Cash available" value={data?.cash} loading={loading} />
        <Card
          label="In operations"
          value={data?.positionsValue}
          loading={loading}
        />
        <Card label="Total equity" value={data?.equity} loading={loading} />
      </section>

      <section className="positions-section">
        <div className="section-header">
          <h2>Open positions</h2>
          <span className="count">
            {data?.positions ? `${data.positions.length} open` : "—"}
            {data?.plan?.sessionDate && (
              <>
                {" · plan "}
                {data.plan.sessionDate}
                {data.plan.signedOff ? " (signed off)" : " (draft)"}
              </>
            )}
          </span>
        </div>
        <PositionsTable positions={data?.positions} />
        <div className="legend">
          <span className="status status-hold">Hold</span> = in next plan ·{" "}
          <span className="status status-exit">Exit next</span> = dropped from
          plan, will be sold next session · <b>Drift</b> = actual − target
          weight (pp); near zero means on plan.
        </div>
      </section>

      <section className="positions-section">
        <div className="section-header">
          <h2>Pending operations</h2>
          <span className="count">
            {data?.openOrders
              ? `${data.openOrders.length} pending`
              : "—"}
            {data?.pendingBuyCommitment != null && data.pendingBuyCommitment > 0 && (
              <>
                {" · est. buy commitment "}
                {usd.format(data.pendingBuyCommitment)}
              </>
            )}
          </span>
        </div>
        <OrdersTable orders={data?.openOrders} />
        <div className="legend">
          Orders already accepted by the broker but not yet filled. Limit orders
          rest in the book and execute when the market reaches the price (or
          expire per TIF).
        </div>
      </section>

      {error && (
        <div className="error">Last refresh failed: {error}</div>
      )}

      <footer>
        Updated {formatTime(data?.asOf)} · refresh every {REFRESH_MS / 1000}s
      </footer>
    </div>
  );
}
