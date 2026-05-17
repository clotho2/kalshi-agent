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

69 tests should pass: risk monitor, kill switch, reconciliation, Kalshi client (auth + retries + rate limits + decimal pricing), positions/PnL math, fill ingestion + idempotency, startup recovery, halt actions (cancel-on-engage), anti-self-trade, bankroll caching, OpenRouter client, LLM strategy filters, plus the original strategy base + execution flow.

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

## What runs today

* **Real bankroll** — `Bankroll` polls `GET /portfolio/balance`, caches for 10s, falls back to last good value on transient failure.
* **Fill ingestion** — polls `GET /portfolio/fills` every 5s, applies to `Position`/`Fill` tables with full duplicate-detection. Fees computed via the Kalshi parabolic formula.
* **Realized PnL** — accumulated on every fill via paired-position math (buy YES + buy NO = $1 lock-in). Daily snapshots at 00:00 ET feed the equity-curve chart.
* **Anti-self-trade** — refuses to open new exposure on the opposite side of an existing position; the rational close is to buy the opposite, which is allowed and realizes PnL via pairing.
* **Cancel-on-HALT** — when the kill switch engages (any path), `HaltMonitor` calls `cancel_all_resting` and marks every local pending/resting order `cancelled_by_halt`.
* **Startup recovery** — on every restart, any local order with `status='pending'` is matched against Kalshi's order list by `client_order_id`. Matched orders adopt the server's true status; orphans are flagged `lost` and posted to Discord.
* **Reconciliation** — startup + hourly, Kalshi positions are source of truth. Fill catch-up runs after every reconcile so downtime fills get backfilled.
* **WebSocket client** (`kalshi/ws.py`) — RSA-signed handshake, exponential-backoff reconnect, message watchdog that trips the kill switch on silence. Coded; strategies can subscribe to live price feeds via `KalshiWebSocket.on(channel, handler)`.
* **OpenRouter LLM** — `OpenRouterClient` posts to `chat/completions` with JSON-mode, retries 429/5xx, parses content. Any OpenRouter model id works (`anthropic/claude-sonnet-4.6`, `openai/gpt-5`, `google/gemini-2.5-pro`, etc.).
* **LLM market assessor strategy** — for each whitelisted ticker, fetches the market, asks the LLM to estimate `P(YES)` given title/description/prices, emits a signal if `(LLM probability ± market price) > min_edge` and `LLM confidence > min_confidence`. The downstream risk monitor still re-checks edge after fees.
* **Placeholder strategy** — kept for paper-mode pipeline testing (one signal/hour, low-confidence so risk monitor rejects).
* **Equity curve dashboard** — Chart.js line chart of daily realized PnL with 30-day window; KPI strip shows realized total, today's PnL, open positions, open exposure.
* **EOD/weekly summaries** — full content: orders, fills, fees, realized PnL, list of open positions.
* **Liveness heartbeat** — outbound GET to `LIVENESS_HEARTBEAT_URL` every 60s; alerting is whatever endpoint you point it at (healthchecks.io, UptimeRobot, etc.).
* **SQLite backup** — `scripts/backup.sh` writes a gzipped `.backup` to `BACKUP_DIR`, retains 30 days. Wire into cron.

## What's intentionally not done

* **Auth verification against the real Kalshi demo** — couldn't be smoke-tested from the build sandbox (egress blocked). Run `scripts/verify_auth.sh` on the Hetzner box with real credentials before flipping to live.
* **WebSocket-driven price feed in `__main__.py`** — the client is coded and tested at module level, but the LLM strategy makes one REST call per signal so live price streaming wasn't required. To enable it, add a `KalshiWebSocket` instance in `__main__.amain` and subscribe to `ticker` channels for your whitelist.

## Repo layout

See the [project brief](./README.md#) for the full tree. Key entry points:

* `src/kalshi_agent/__main__.py` — service bootstrap
* `src/kalshi_agent/execution.py` — single chokepoint between strategy and Kalshi
* `src/kalshi_agent/safety/risk_monitor.py` — pre-trade + post-hoc checks
* `src/kalshi_agent/safety/kill_switch.py` — the HALT file
* `src/kalshi_agent/strategies/base.py` — `Strategy` ABC and `Signal` model
