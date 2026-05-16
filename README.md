# kalshi-agent

Standalone, isolated sidecar service for agentic trading on Kalshi prediction markets.
Runs 24/7 as a `systemd` unit, talks to Kalshi over REST+WS with RSA-signed requests,
and is gated by independent kill-switch paths so a runaway strategy or model cannot
exceed pre-configured risk limits.

This repository contains the **platform skeleton** plus a placeholder strategy that
proves the pipeline end-to-end. Real strategies (CPI nowcast, KPI nowcast, etc.)
plug into the `Strategy` ABC and will be added in follow-up work.

---

## Architecture at a glance

```
┌────────────────────────────────────────────────────────────────┐
│ kalshi-agent (single asyncio event loop)                       │
│                                                                │
│  ┌──────────────┐  signal   ┌─────────┐ size+check ┌────────┐  │
│  │  Strategy    │──────────▶│ Risk    │───────────▶│Exec    │──┐
│  │ (60s tick)   │           │ Monitor │            │        │  │
│  └──────────────┘           └──┬──────┘            └────────┘  │ │
│                                │ trips                          │ │
│                                ▼                                │ │
│  ┌──────────────┐         ┌─────────────┐                       │ │
│  │  Reconciler  │◀───lock─│ Kill Switch │   /var/lib/HALT       │ │
│  │ (hourly,     │         │             │◀──┐                  │ │
│  │ on startup)  │         └─────────────┘   │ touch / curl     │ │
│  └────┬─────────┘                            │                  │ │
│       │                                       │                  │ │
│       ▼               ┌─────────┐             │                  ▼ │
│  ┌─────────┐          │ FastAPI │─/api/control/halt──────────────┘ │
│  │ SQLite  │          │  :8787  │─/api/observer/*  (read-only)     │
│  │ (WAL)   │◀────────│         │─/   dashboard (HTML)              │
│  └─────────┘          └─────────┘                                  │
│                            │                                       │
└────────────────────────────┼───────────────────────────────────────┘
                             │ Discord webhook
                             ▼
                     [trades / kill / EOD summaries]
```

**Key safety properties**

* **Paper mode is default.** `--mode live` must be passed explicitly to talk to production Kalshi.
* **Three independent halt paths**: HALT file presence, HTTP control endpoint, automatic on risk breach.
* **Risk checks are pure Python** — no LLM in the loop between signal and order.
* **Idempotent orders** via `client_order_id` persisted before send — restart-safe.
* **Kalshi is the source of truth** for positions; the local DB is a mirror, reconciled hourly.
* **Conservative defaults**: ≤$50/contract size, ≤$200 total exposure, ≤$50 daily loss, ≤10 orders/minute.

---

## Install (dev)

```bash
git clone https://github.com/clotho2/kalshi-agent
cd kalshi-agent
uv venv
uv pip install -e ".[dev]"
```

(If `uv` is not installed: `pip install uv`, or fall back to `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`.)

## Configure (local)

```bash
cp .env.example .env
# Edit .env — fill in KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
# DISCORD_WEBHOOK_URL, CONTROL_BEARER_TOKEN.
# Generate the bearer token with:  openssl rand -hex 32

cp config/config.example.yaml config/config.yaml
# Adjust paths (db_path, log_dir, halt_file_path) if running outside /var/lib.
```

## Run tests

```bash
.venv/bin/pytest -v
```

36 tests should pass: risk monitor, kill switch, reconciliation, kalshi client (auth + retries + rate limits), strategy base + execution.

## Run in paper mode

```bash
.venv/bin/python -m kalshi_agent --config config/config.yaml --mode paper
```

The service:

1. Initializes the SQLite DB in WAL mode.
2. Runs a startup reconciliation (best-effort).
3. Starts the placeholder strategy on a 60s tick, which emits one signal per hour.
4. Starts the FastAPI server on `127.0.0.1:8787`.
5. Schedules hourly reconciliation, EOD summary (23:59 ET), and weekly summary (Sun 23:59 ET).

Open `http://127.0.0.1:8787/` for the dashboard.

## Engage / disengage the kill switch

Three independent paths:

```bash
# (1) File-based (primary)
./scripts/halt.sh "reason"
./scripts/resume.sh

# (2) HTTP
curl -X POST -H "Authorization: Bearer $CONTROL_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"manual"}' \
  http://127.0.0.1:8787/api/control/halt

curl -X POST -H "Authorization: Bearer $CONTROL_BEARER_TOKEN" \
  http://127.0.0.1:8787/api/control/resume

# (3) Automatic — fires on any of:
#   * daily realized PnL <= -max_daily_loss_usd
#   * post-hoc total exposure > max_total_exposure_usd
#   * > error_spike_threshold API errors in error_spike_window_seconds
#   * > max_orders_per_minute order attempts in 60s
```

When automatic engagement fires, the HALT file carries a JSON payload with
`reason`, `source`, `timestamp`, and `payload` keys — `cat /var/lib/kalshi-agent/HALT`
to inspect.

## Read the journal

Two parallel sources of truth:

**SQLite** (`config.storage.db_path`, default `/var/lib/kalshi-agent/kalshi-agent.db`):

```bash
sqlite3 /var/lib/kalshi-agent/kalshi-agent.db
> SELECT created_at, market_ticker, side, accepted, rejection_reason, rationale FROM decisions ORDER BY created_at DESC LIMIT 20;
> SELECT created_at, market_ticker, side, count, price_dollars, status FROM orders ORDER BY created_at DESC LIMIT 20;
> SELECT created_at, kind, level, message FROM events ORDER BY created_at DESC LIMIT 20;
```

**JSON Lines** (`config.journal.log_dir`, default `/var/log/kalshi-agent/agent.jsonl`):

```bash
tail -f /var/log/kalshi-agent/agent.jsonl | jq .
```

Daily-rotated, retained 90 days. Secrets are redacted by the structlog processor
(`Authorization`, `KALSHI-ACCESS-SIGNATURE`, Discord webhook URLs, etc.).

## Observer API

Read-only. No auth from localhost; bearer token required from elsewhere
(e.g. through the Cloudflare tunnel).

```bash
curl http://127.0.0.1:8787/api/observer/health
curl http://127.0.0.1:8787/api/observer/positions
curl 'http://127.0.0.1:8787/api/observer/trades?limit=20'
curl 'http://127.0.0.1:8787/api/observer/pnl?period=today'
curl http://127.0.0.1:8787/api/observer/market/INXD-26MAY16
```

## Production install

See **[MANUAL_SETUP.md](./MANUAL_SETUP.md)** for `systemd` installation, environment
file setup, RSA key generation, Cloudflare tunnel ingress, and bearer-token
provisioning.

---

## Key design choices (with rationale)

| Choice | Why |
|---|---|
| Decimal-string prices on the wire | Required since Kalshi's March 2026 fixed-point migration. |
| Real fractional Kelly (`edge / (1-price)`) clamped at `max_kelly_size_pct_bankroll` | Brief's `kelly * confidence * capital` heuristic ignores edge magnitude. |
| Fees computed per-contract via `ceil(0.07·P·(1-P)·100)/100` | Real Kalshi formula; flat fee floor is too crude. |
| EOD boundary in `America/New_York` | Aligns with Kalshi settlement; the Hetzner server's local Berlin time is irrelevant to trading. |
| HALT file in `/var/lib` not `/var/run` | Persists across reboots — automatic engagement stays engaged. |
| Reconciliation behind an asyncio lock | Prevents racing in-flight orders against position pulls. |
| `client_order_id` set before HTTP send | Recoverable on crash mid-flight. |
| SQLite WAL + busy_timeout | Dashboard + writer share the DB without locking. |
| Bearer token for control + remote observer | Brief's auth model; localhost observer is no-auth to keep curl-debug easy. |
| Independent of any LLM/orchestrator process | This service is the trade-authority root; LLM signals come in via Strategy implementations only. |

## What's stubbed in this skeleton

* `bankroll_provider` in `__main__.py` returns a fixed `$1000`. Real wiring fetches `/portfolio/balance` from Kalshi on each tick.
* WebSocket price feed (`KalshiClient` has REST only; WS reconnect logic + watchdog is queued for next pass).
* Fill ingestion: `Fill` rows are written by future WS handler / reconciliation; right now the executor only writes `Order` rows.
* Real strategies — see `strategies/placeholder.py` for the only concrete `Strategy`.

## Repo layout

See the [project brief](./README.md#) for the full tree. Key entry points:

* `src/kalshi_agent/__main__.py` — service bootstrap
* `src/kalshi_agent/execution.py` — single chokepoint between strategy and Kalshi
* `src/kalshi_agent/safety/risk_monitor.py` — pre-trade + post-hoc checks
* `src/kalshi_agent/safety/kill_switch.py` — the HALT file
* `src/kalshi_agent/strategies/base.py` — `Strategy` ABC and `Signal` model
