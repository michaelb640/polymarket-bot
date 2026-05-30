#!/usr/bin/env python3
"""Live dashboard for the Polymarket BTC bot."""

import sqlite3
import time
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template_string
import requests

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _pacific_day_range() -> tuple[str, str]:
    now = datetime.now(_PACIFIC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return (
        day_start.astimezone(timezone.utc).strftime(fmt),
        day_end.astimezone(timezone.utc).strftime(fmt),
    )

DB_PATH = "bot.db"
BINANCE_URL = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSD"

app = Flask(__name__)

_price_cache: dict = {"price": None, "ts": 0.0}


def _get_btc_price() -> float | None:
    now = time.time()
    if now - _price_cache["ts"] < 10:
        return _price_cache["price"]
    try:
        r = requests.get(BINANCE_URL, timeout=5)
        r.raise_for_status()
        _price_cache["price"] = float(r.json()["price"])
        _price_cache["ts"] = now
    except Exception:
        pass
    return _price_cache["price"]


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _age(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int(time.time() - dt.timestamp())
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return "?"


STARTING_BALANCE = 1000.0

@app.get("/api/stats")
def api_stats():
    start_utc, end_utc = _pacific_day_range()
    btc = _get_btc_price()

    with _db() as conn:
        open_rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY entry_time DESC"
        ).fetchall()

        closed_today = conn.execute(
            "SELECT * FROM positions WHERE status='closed' AND exit_time >= ? AND exit_time < ? ORDER BY exit_time DESC",
            (start_utc, end_utc),
        ).fetchall()

        all_closed = conn.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY exit_time DESC LIMIT 100"
        ).fetchall()

        all_closed_for_breakdown = conn.execute(
            "SELECT exit_time, pnl, side FROM positions WHERE status='closed' ORDER BY exit_time ASC"
        ).fetchall()

        daily_pnl_row = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) as total FROM positions WHERE status='closed' AND exit_time >= ? AND exit_time < ?",
            (start_utc, end_utc),
        ).fetchone()

        total_pnl_row = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) as total FROM positions WHERE status='closed'"
        ).fetchone()

    # Arb monitor stats (separate table, may not exist yet)
    arb_stats = {"detected": 0, "executed": 0, "dry_run": 0, "est_pnl": 0.0}
    arb_events_list = []
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM arb_events WHERE event_type IN ('detected','dry_run','executed')"
            ).fetchone()
            arb_stats["detected"] = row["cnt"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM arb_events WHERE event_type='dry_run'"
            ).fetchone()
            arb_stats["dry_run"] = row["cnt"] if row else 0

            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM arb_events WHERE event_type='executed'"
            ).fetchone()
            arb_stats["executed"] = row["cnt"] if row else 0

            # Est. P&L: sum profit from executed (live) + dry_run (would-have).
            # In DRY_RUN mode this shows the simulated P&L; in live it adds real fills.
            row = conn.execute(
                "SELECT COALESCE(SUM(est_pnl),0) as pnl FROM arb_events WHERE event_type IN ('executed','dry_run')"
            ).fetchone()
            arb_stats["est_pnl"] = round(float(row["pnl"]), 4) if row else 0.0

            rows = conn.execute(
                "SELECT * FROM arb_events ORDER BY event_time DESC LIMIT 30"
            ).fetchall()
            arb_events_list = [dict(r) for r in rows]
    except Exception:
        pass  # table doesn't exist yet on older DB

    # Roll arb P&L into the main balance cards
    arb_pnl_total = 0.0
    arb_pnl_today = 0.0
    arb_trades_today = 0
    arb_wins_today = 0
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(est_pnl),0) as t FROM arb_events "
                "WHERE event_type IN ('executed','dry_run')"
            ).fetchone()
            arb_pnl_total = float(row["t"]) if row else 0.0

            row = conn.execute(
                "SELECT COALESCE(SUM(est_pnl),0) as t, COUNT(*) as cnt FROM arb_events "
                "WHERE event_type IN ('executed','dry_run') AND event_time >= ? AND event_time < ?",
                (start_utc, end_utc),
            ).fetchone()
            if row:
                arb_pnl_today = float(row["t"])
                arb_trades_today = int(row["cnt"])
                arb_wins_today = arb_trades_today  # arbs are deterministic — every fill is a win
    except Exception:
        pass

    open_positions = [dict(r) for r in open_rows]
    for p in open_positions:
        p["age"] = _age(p["entry_time"])

    closed_list = [dict(r) for r in closed_today]
    all_closed_list = [dict(r) for r in all_closed]

    signal_trades_today = len(closed_list)
    signal_winners_today = sum(1 for t in closed_list if (t["pnl"] or 0) > 0)
    total_today = signal_trades_today + arb_trades_today
    winners_today = signal_winners_today + arb_wins_today
    win_rate = round(winners_today / total_today * 100, 1) if total_today else 0.0

    signal_daily_pnl = round(daily_pnl_row["total"], 4) if daily_pnl_row else 0.0
    daily_pnl = round(signal_daily_pnl + arb_pnl_today, 4)

    signal_total_pnl = round(total_pnl_row["total"], 4) if total_pnl_row else 0.0
    total_pnl = round(signal_total_pnl + arb_pnl_total, 4)
    account_balance = round(STARTING_BALANCE + total_pnl, 2)

    # P&L curve + intraday drawdown for today
    pnl_curve: list[dict] = []
    cumulative = 0.0
    today_min_pnl = 0.0
    for t in reversed(closed_list):
        cumulative += t["pnl"] or 0
        if cumulative < today_min_pnl:
            today_min_pnl = cumulative
        pnl_curve.append({"t": t["exit_time"], "pnl": round(cumulative, 4)})
    intraday_drawdown = round(today_min_pnl, 2)

    # P&L by Pacific day (all history, no limit)
    all_for_breakdown = [dict(r) for r in all_closed_for_breakdown]
    day_map: dict = {}
    for t in all_for_breakdown:
        try:
            dt = datetime.fromisoformat(t["exit_time"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            pacific_date = dt.astimezone(_PACIFIC).date().isoformat()
        except Exception:
            continue
        if pacific_date not in day_map:
            day_map[pacific_date] = {"date": pacific_date, "trades": 0, "wins": 0, "pnl": 0.0, "running": 0.0, "max_drawdown": 0.0}
        day_map[pacific_date]["trades"] += 1
        day_map[pacific_date]["wins"] += 1 if (t["pnl"] or 0) > 0 else 0
        day_map[pacific_date]["pnl"] += t["pnl"] or 0
        day_map[pacific_date]["running"] += t["pnl"] or 0
        if day_map[pacific_date]["running"] < day_map[pacific_date]["max_drawdown"]:
            day_map[pacific_date]["max_drawdown"] = day_map[pacific_date]["running"]
    daily_breakdown = sorted(day_map.values(), key=lambda x: x["date"], reverse=True)
    for d in daily_breakdown:
        d["pnl"] = round(d["pnl"], 2)
        d["max_drawdown"] = round(d["max_drawdown"], 2)
        d["win_rate"] = round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0.0
        del d["running"]

    return jsonify(
        btc_price=btc,
        daily_pnl=daily_pnl,
        account_balance=account_balance,
        starting_balance=STARTING_BALANCE,
        trades_today=total_today,
        winners_today=winners_today,
        win_rate=win_rate,
        open_positions=open_positions,
        closed_today=closed_list,
        all_closed=all_closed_list,
        pnl_curve=pnl_curve,
        daily_breakdown=daily_breakdown,
        arb_stats=arb_stats,
        arb_events=arb_events_list,
        intraday_drawdown=intraday_drawdown,
        server_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket BTC Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0d0f14;
    --surface: #161b22;
    --border: #21262d;
    --text: #e6edf3;
    --muted: #7d8590;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --blue: #388bfd;
    --accent: #1f6feb;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace;
    font-size: 14px;
    min-height: 100vh;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 24px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 10;
  }

  header h1 {
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.5px;
  }

  .badge {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 12px;
    background: #1a3a5c;
    color: var(--blue);
    border: 1px solid #388bfd44;
    margin-left: 8px;
    letter-spacing: 0.8px;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 13px;
    color: var(--muted);
  }

  .btc-price {
    font-size: 20px;
    font-weight: 700;
    color: var(--text);
    font-variant-numeric: tabular-nums;
  }

  .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
    display: inline-block;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  main {
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px 20px;
  }

  .cards {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin-bottom: 28px;
  }

  @media (max-width: 800px) {
    .cards { grid-template-columns: repeat(2, 1fr); }
  }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px 20px;
  }

  .card-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    margin-bottom: 8px;
  }

  .card-value {
    font-size: 28px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  .card-sub {
    font-size: 12px;
    color: var(--muted);
    margin-top: 6px;
  }

  .positive { color: var(--green); }
  .negative { color: var(--red); }
  .neutral  { color: var(--text); }

  .section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    margin-bottom: 20px;
    overflow: hidden;
  }

  .section-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    font-weight: 600;
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .section-header .count {
    font-size: 11px;
    color: var(--muted);
    font-weight: 400;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  th {
    text-align: left;
    padding: 10px 20px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--muted);
    font-weight: 500;
    border-bottom: 1px solid var(--border);
  }

  td {
    padding: 11px 20px;
    border-bottom: 1px solid var(--border);
    font-variant-numeric: tabular-nums;
  }

  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }

  .side-yes {
    color: var(--green);
    font-weight: 600;
  }
  .side-no {
    color: var(--red);
    font-weight: 600;
  }

  .empty {
    padding: 32px 20px;
    text-align: center;
    color: var(--muted);
    font-size: 13px;
  }

  .chart-wrap {
    padding: 20px;
    height: 200px;
    position: relative;
  }

  #last-updated {
    font-size: 11px;
    color: var(--muted);
  }

  .market-id {
    font-family: monospace;
    font-size: 12px;
    color: var(--muted);
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
</head>
<body>

<header>
  <div style="display:flex;align-items:center;gap:8px;">
    <h1>Polymarket BTC Bot</h1>
    <span class="badge">DRY RUN</span>
  </div>
  <div class="header-right">
    <span id="last-updated"></span>
    <span id="btc-price" class="btc-price">—</span>
    <span class="dot" title="Live"></span>
  </div>
</header>

<main>
  <div class="cards">
    <div class="card">
      <div class="card-label">Total Balance</div>
      <div id="account-balance" class="card-value neutral">—</div>
      <div id="account-balance-started" class="card-sub">Started: —</div>
      <div id="account-balance-profit" class="card-sub" style="margin-top:2px">Profit: —</div>
      <div id="account-balance-sub" class="card-sub" style="margin-top:2px">—</div>
    </div>
    <div class="card">
      <div class="card-label">Daily P&amp;L</div>
      <div id="daily-pnl" class="card-value neutral">—</div>
      <div id="daily-pnl-pct" class="card-sub">of total balance</div>
      <div id="daily-drawdown" class="card-sub" style="margin-top:2px">Max dip: —</div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div id="win-rate" class="card-value neutral">—</div>
      <div id="win-sub" class="card-sub">—</div>
    </div>
    <div class="card">
      <div class="card-label">Trades Today</div>
      <div id="trades-today" class="card-value neutral">—</div>
      <div id="open-count-card" class="card-sub">— open now</div>
    </div>
  </div>

  <div class="section" id="chart-section" style="display:none;">
    <div class="section-header">P&amp;L Curve (Today)</div>
    <div class="chart-wrap">
      <canvas id="pnl-chart"></canvas>
    </div>
  </div>

  <div class="section">
    <div class="section-header">
      Open Positions
      <span class="count" id="open-count-label"></span>
    </div>
    <div id="open-body">
      <div class="empty">No open positions.</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">
      Trade History (Today)
      <span class="count" id="closed-count-label"></span>
    </div>
    <div id="closed-body">
      <div class="empty">No trades today yet.</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">
      Arb Monitor
      <span class="count" id="arb-count-label"></span>
    </div>
    <div id="arb-summary" style="display:flex;gap:32px;padding:16px 20px;border-bottom:1px solid var(--border)">
      <div><div class="card-label" style="margin-bottom:4px">Detected</div><span id="arb-detected" style="font-size:22px;font-weight:700">—</span></div>
      <div><div class="card-label" style="margin-bottom:4px">Would Execute</div><span id="arb-dry-run" style="font-size:22px;font-weight:700">—</span></div>
      <div><div class="card-label" style="margin-bottom:4px">Executed (Live)</div><span id="arb-executed" style="font-size:22px;font-weight:700">—</span></div>
      <div><div class="card-label" style="margin-bottom:4px">Est. P&amp;L</div><span id="arb-pnl" style="font-size:22px;font-weight:700">—</span></div>
    </div>
    <div id="arb-body">
      <div class="empty">No arb events yet — scanner running in background.</div>
    </div>
  </div>

  <div class="section">
    <div class="section-header">P&amp;L by Day (PST)</div>
    <div id="daily-breakdown-body">
      <div class="empty">No completed days yet.</div>
    </div>
  </div>
</main>

<script>
let pnlChart = null;

function fmt(n, prefix='$') {
  if (n === null || n === undefined) return '—';
  const s = Math.abs(n).toFixed(2);
  return (n < 0 ? '-' : '') + prefix + s;
}

function colorClass(n) {
  if (n > 0) return 'positive';
  if (n < 0) return 'negative';
  return 'neutral';
}

function renderOpen(positions) {
  const el = document.getElementById('open-body');
  document.getElementById('open-count-label').textContent =
    positions.length ? positions.length + ' position' + (positions.length > 1 ? 's' : '') : '';
  if (!positions.length) {
    el.innerHTML = '<div class="empty">No open positions.</div>';
    return;
  }
  let html = '<table><thead><tr>'
    + '<th>Market</th><th>Side</th><th>BTC Entry</th><th>Token Paid</th><th>Size</th><th>Age</th>'
    + '</tr></thead><tbody>';
  for (const p of positions) {
    const btc = p.btc_entry_price ? '$' + p.btc_entry_price.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
    const paid = (p.entry_price * 100).toFixed(1) + '¢';
    const name = p.market_name || p.market_id.substring(0, 16) + '…';
    html += `<tr>
      <td style="font-size:12px">${name}</td>
      <td><span class="side-${p.side.toLowerCase()}">${p.side}</span></td>
      <td>${btc}</td>
      <td style="color:var(--muted)">${paid}</td>
      <td>$${p.size.toFixed(2)}</td>
      <td>${p.age}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderClosed(trades) {
  const el = document.getElementById('closed-body');
  document.getElementById('closed-count-label').textContent =
    trades.length ? trades.length + ' trade' + (trades.length > 1 ? 's' : '') : '';
  if (!trades.length) {
    el.innerHTML = '<div class="empty">No trades today yet.</div>';
    return;
  }

  function resultLabel(exitPrice, pnl) {
    if (exitPrice === null || exitPrice === undefined) return '<span style="color:var(--muted)">—</span>';
    if (exitPrice >= 0.99) return '<span class="positive">WIN</span>';
    if (exitPrice <= 0.01) return '<span class="negative">LOSS</span>';
    return '<span style="color:var(--muted)">PUSH</span>';
  }

  let html = '<table><thead><tr>'
    + '<th>Market</th><th>Side</th><th>BTC Entry</th><th>Paid</th><th>Result</th><th>P&amp;L</th><th>Time (UTC)</th>'
    + '</tr></thead><tbody>';
  for (const t of trades) {
    const pnlClass = colorClass(t.pnl);
    const timeStr = t.exit_time ? t.exit_time.substring(0, 19).replace('T', ' ') : '—';
    const btc = t.btc_entry_price ? '$' + t.btc_entry_price.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
    const paid = (t.entry_price * 100).toFixed(1) + '¢';
    const name = t.market_name || t.market_id.substring(0, 16) + '…';
    html += `<tr>
      <td style="font-size:12px">${name}</td>
      <td><span class="side-${t.side.toLowerCase()}">${t.side}</span></td>
      <td>${btc}</td>
      <td style="color:var(--muted)">${paid}</td>
      <td>${resultLabel(t.exit_price, t.pnl)}</td>
      <td class="${pnlClass}">${fmt(t.pnl)}</td>
      <td style="color:var(--muted)">${timeStr}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderChart(curve) {
  const section = document.getElementById('chart-section');
  if (!curve || !curve.length) { section.style.display = 'none'; return; }
  section.style.display = '';

  const labels = curve.map(p => p.t.substring(11, 16));
  const data   = curve.map(p => p.pnl);

  const color = data[data.length - 1] >= 0 ? '#3fb950' : '#f85149';

  if (pnlChart) {
    pnlChart.data.labels = labels;
    pnlChart.data.datasets[0].data = data;
    pnlChart.data.datasets[0].borderColor = color;
    pnlChart.data.datasets[0].backgroundColor = color + '22';
    pnlChart.update('none');
    return;
  }

  const ctx = document.getElementById('pnl-chart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data,
        borderColor: color,
        backgroundColor: color + '22',
        borderWidth: 2,
        pointRadius: 2,
        tension: 0.3,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#7d8590', maxTicksLimit: 8 }, grid: { color: '#21262d' } },
        y: {
          ticks: { color: '#7d8590', callback: v => '$' + v.toFixed(2) },
          grid: { color: '#21262d' },
        }
      }
    }
  });
}

function renderArb(stats, events) {
  // Summary row
  document.getElementById('arb-detected').textContent = stats ? stats.detected : '—';
  document.getElementById('arb-dry-run').textContent = stats ? stats.dry_run : '—';

  const execEl = document.getElementById('arb-executed');
  execEl.textContent = stats ? stats.executed : '—';

  const pnlEl = document.getElementById('arb-pnl');
  if (stats) {
    const p = stats.est_pnl || 0;
    pnlEl.textContent = (p >= 0 ? '+' : '') + '$' + Math.abs(p).toFixed(2);
    pnlEl.style.color = p > 0 ? 'var(--green)' : p < 0 ? 'var(--red)' : 'var(--muted)';
  }

  const countEl = document.getElementById('arb-count-label');
  if (stats) countEl.textContent = stats.detected + ' total detected';

  // Events table
  const el = document.getElementById('arb-body');
  if (!events || !events.length) {
    el.innerHTML = '<div class="empty">No arb events yet — scanner running in background.</div>';
    return;
  }

  const typeColor = { detected: 'var(--muted)', dry_run: 'var(--yellow)', executed: 'var(--green)', aborted: 'var(--red)' };
  const typeLabel = { detected: 'THIN', dry_run: 'DRY RUN', executed: 'LIVE', aborted: 'ABORTED' };

  let html = '<table><thead><tr>'
    + '<th>Time (UTC)</th><th>Type</th><th>YES Ask</th><th>NO Ask</th><th>Total</th><th>Gross</th><th>Est. Profit</th>'
    + '</tr></thead><tbody>';
  for (const e of events) {
    const timeStr = e.event_time ? e.event_time.substring(0, 19).replace('T', ' ') : '—';
    const color = typeColor[e.event_type] || 'var(--muted)';
    const label = typeLabel[e.event_type] || e.event_type.toUpperCase();
    const gross = e.gross_pct != null ? e.gross_pct.toFixed(2) + '%' : '—';
    const pnl = e.est_pnl != null ? '$' + e.est_pnl.toFixed(3) : '—';
    html += `<tr>
      <td style="color:var(--muted)">${timeStr}</td>
      <td style="color:${color};font-weight:600">${label}</td>
      <td>${e.yes_ask != null ? e.yes_ask.toFixed(3) : '—'}</td>
      <td>${e.no_ask  != null ? e.no_ask.toFixed(3)  : '—'}</td>
      <td>${e.total   != null ? e.total.toFixed(4)   : '—'}</td>
      <td>${gross}</td>
      <td>${pnl}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderDailyBreakdown(days) {
  const el = document.getElementById('daily-breakdown-body');
  if (!days || !days.length) {
    el.innerHTML = '<div class="empty">No completed days yet.</div>';
    return;
  }
  let html = '<table><thead><tr>'
    + '<th>Date (PST)</th><th>Trades</th><th>Win Rate</th><th>P&amp;L</th><th>Max Dip</th>'
    + '</tr></thead><tbody>';
  for (const d of days) {
    const pnlClass = d.pnl > 0 ? 'positive' : d.pnl < 0 ? 'negative' : 'neutral';
    const wrClass = d.win_rate >= 50 ? 'positive' : 'negative';
    const dipStr = d.max_drawdown < 0 ? `-$${Math.abs(d.max_drawdown).toFixed(2)}` : '$0.00';
    html += `<tr>
      <td>${d.date}</td>
      <td>${d.trades}</td>
      <td class="${wrClass}">${d.win_rate}%&nbsp;<span style="color:var(--muted);font-weight:400">(${d.wins}W / ${d.trades - d.wins}L)</span></td>
      <td class="${pnlClass}">${d.pnl >= 0 ? '+' : ''}$${d.pnl.toFixed(2)}</td>
      <td style="color:var(--red)">${dipStr}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

async function refresh() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();

    document.getElementById('btc-price').textContent =
      d.btc_price ? '$' + d.btc_price.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';

    // Total balance card
    const balEl = document.getElementById('account-balance');
    balEl.textContent = d.account_balance ? '$' + d.account_balance.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';

    document.getElementById('account-balance-started').textContent =
      `Started: $${d.starting_balance.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})}`;

    const profit = d.account_balance - d.starting_balance;
    const profitEl = document.getElementById('account-balance-profit');
    profitEl.textContent = `Profit: ${profit >= 0 ? '+' : ''}$${profit.toFixed(2)}`;
    profitEl.style.color = profit > 0 ? 'var(--green)' : profit < 0 ? 'var(--red)' : 'var(--muted)';

    const totalPct = d.starting_balance ? (profit / d.starting_balance * 100) : 0;
    const totalPctStr = (totalPct >= 0 ? '+' : '') + totalPct.toFixed(1) + '%';
    const balSubEl = document.getElementById('account-balance-sub');
    balSubEl.textContent = `${totalPctStr} since start`;
    balSubEl.style.color = totalPct > 0 ? 'var(--green)' : totalPct < 0 ? 'var(--red)' : '';

    // Daily P&L card
    const pnlEl = document.getElementById('daily-pnl');
    pnlEl.textContent = fmt(d.daily_pnl);
    pnlEl.className = 'card-value ' + colorClass(d.daily_pnl);
    const pct = d.account_balance ? (d.daily_pnl / d.account_balance * 100) : 0;
    const pctStr = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
    const pctEl = document.getElementById('daily-pnl-pct');
    pctEl.textContent = `${pctStr} of total balance`;
    pctEl.style.color = pct > 0 ? 'var(--green)' : pct < 0 ? 'var(--red)' : '';

    const dipEl = document.getElementById('daily-drawdown');
    const dip = d.intraday_drawdown || 0;
    dipEl.textContent = dip < 0 ? `Max dip: -$${Math.abs(dip).toFixed(2)}` : 'Max dip: $0.00';
    dipEl.style.color = dip < 0 ? 'var(--red)' : 'var(--muted)';

    const wrEl = document.getElementById('win-rate');
    wrEl.textContent = d.trades_today ? d.win_rate + '%' : '—';
    wrEl.className = 'card-value ' + (d.win_rate >= 50 ? 'positive' : d.trades_today ? 'negative' : 'neutral');

    document.getElementById('win-sub').textContent =
      d.trades_today ? `${d.winners_today}W / ${d.trades_today - d.winners_today}L` : 'no trades yet';

    document.getElementById('trades-today').textContent = d.trades_today ?? '—';
    document.getElementById('open-count-card').textContent =
      d.open_positions.length === 1 ? '1 open now' : `${d.open_positions.length} open now`;

    document.getElementById('last-updated').textContent = d.server_time;

    renderOpen(d.open_positions);
    renderClosed(d.closed_today);
    renderChart(d.pnl_curve);
    renderArb(d.arb_stats, d.arb_events);
    renderDailyBreakdown(d.daily_breakdown);
  } catch(e) {
    console.error('refresh error', e);
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
