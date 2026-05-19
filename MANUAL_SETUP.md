# Production setup (Hetzner + systemd + Cloudflare tunnel)

Server timezone is `Europe/Berlin`; that's irrelevant to trading — the agent
stores all timestamps as UTC and uses `America/New_York` for any human-facing
display or scheduling cron. You do **not** need to change the system clock.

## 1. System user and directories

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin kalshi

sudo mkdir -p /opt/kalshi-agent /etc/kalshi-agent /var/lib/kalshi-agent /var/log/kalshi-agent
sudo chown -R kalshi:kalshi /var/lib/kalshi-agent /var/log/kalshi-agent
# Critical: the `kalshi` user must be able to traverse /etc/kalshi-agent to reach
# the config and env files. Owning the dir as root:kalshi (group-traversable) is
# the minimum permissive setup — files inside stay readable only by root and kalshi.
sudo chown root:kalshi /etc/kalshi-agent
sudo chmod 750 /etc/kalshi-agent
```

## 2. Deploy code

```bash
sudo -u kalshi git clone https://github.com/clotho2/kalshi-agent /opt/kalshi-agent
cd /opt/kalshi-agent
sudo -u kalshi python3.11 -m venv .venv
sudo -u kalshi .venv/bin/pip install --upgrade pip
sudo -u kalshi .venv/bin/pip install -e .
```

## 3. Kalshi API key

Generate locally, then upload the **public** key to your Kalshi account
(Settings → API Keys → Upload):

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out kalshi_private_key.pem
openssl rsa -in kalshi_private_key.pem -pubout -out kalshi_public_key.pem

# Upload kalshi_public_key.pem in the Kalshi web UI; copy the resulting Key ID.

sudo mv kalshi_private_key.pem /etc/kalshi-agent/
sudo chown root:kalshi /etc/kalshi-agent/kalshi_private_key.pem
sudo chmod 640 /etc/kalshi-agent/kalshi_private_key.pem
```

## 4. Control bearer token

```bash
openssl rand -hex 32
# Copy the hex string — you'll paste it into the env file in step 5 and use the same
# value when calling /api/control/* via curl or scripts/reconcile.sh.
```

## 5. Environment file

`/etc/kalshi-agent/env` (read by systemd at unit start):

```ini
KALSHI_API_KEY_ID=<paste Key ID from Kalshi>
KALSHI_PRIVATE_KEY_PATH=/etc/kalshi-agent/kalshi_private_key.pem
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXX/YYY
CONTROL_BEARER_TOKEN=<paste hex from step 4>
KALSHI_AGENT_CONFIG=/etc/kalshi-agent/config.yaml
```

```bash
sudo chown root:kalshi /etc/kalshi-agent/env
sudo chmod 640 /etc/kalshi-agent/env
```

## 6. Config file

```bash
sudo cp /opt/kalshi-agent/config/config.example.yaml /etc/kalshi-agent/config.yaml
sudo chown root:kalshi /etc/kalshi-agent/config.yaml
sudo chmod 644 /etc/kalshi-agent/config.yaml
# Review and edit risk limits, market whitelist, etc.
```

Defaults are conservative (`max_total_exposure: $200`, `max_daily_loss: $50`,
`per_order_max_contracts: 50`, `max_orders_per_minute: 10`). Tighten further if
desired; loosen only after observing the agent for a while.

## 7. systemd unit

```bash
sudo cp /opt/kalshi-agent/systemd/kalshi-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kalshi-agent
sudo systemctl status kalshi-agent
sudo journalctl -u kalshi-agent -f
```

The unit runs with `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
`MemoryDenyWriteExecute`, and read-write access only to
`/var/lib/kalshi-agent` and `/var/log/kalshi-agent`.

## 8. Cloudflare tunnel ingress

The service binds to `127.0.0.1:8787`. To expose it through your existing
`cloudflared` tunnel, add an ingress rule. Example `/etc/cloudflared/config.yml`:

```yaml
ingress:
  - hostname: kalshi-agent.your-domain.tld
    service: http://127.0.0.1:8787
    originRequest:
      noTLSVerify: true
  - service: http_status:404
```

```bash
sudo systemctl restart cloudflared
```

**Important**: the observer API requires the bearer token from non-localhost.
Without it you'll get `401`. The control API always requires it.

If you don't want the dashboard publicly reachable, restrict the tunnel hostname
to a Cloudflare Access policy (email-based gating). The agent itself does not
enforce TLS or origin auth beyond the bearer token model.

## 9. Drills (do these before relying on the service)

```bash
# (a) File-based halt
sudo -u kalshi /opt/kalshi-agent/scripts/halt.sh "drill"
# verify within 5s on the dashboard: kill switch ENGAGED
sudo -u kalshi /opt/kalshi-agent/scripts/resume.sh

# (b) HTTP halt
CONTROL_BEARER_TOKEN=<token> curl -X POST \
  -H "Authorization: Bearer $CONTROL_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"drill"}' \
  http://127.0.0.1:8787/api/control/halt
# resume:
curl -X POST -H "Authorization: Bearer $CONTROL_BEARER_TOKEN" \
  http://127.0.0.1:8787/api/control/resume

# (c) Restart reconciliation
sudo systemctl restart kalshi-agent
sudo journalctl -u kalshi-agent -n 50 | grep reconciliation
```

## 10. Backups

The SQLite DB at `/var/lib/kalshi-agent/kalshi-agent.db` is your audit trail.
Suggested: nightly `sqlite3 ... .dump > /backups/kalshi-$(date +%F).sql`
piped to your existing backup mechanism. The JSONL logs are append-only and
self-rotate; rsync `/var/log/kalshi-agent/` to a remote host if you want
extra durability.

## 10b. Verify Kalshi auth

Before letting the service run unattended, sanity-check the signing logic:

```bash
sudo -u kalshi bash -c '
  set -a
  source /etc/kalshi-agent/env
  set +a
  cd /opt/kalshi-agent
  ./scripts/verify_auth.sh
'
# Expected output:  OK balance=0.0000  (or your actual demo balance)
```

If this fails with `401`, regenerate the API key on Kalshi (Settings → API Keys),
re-upload the public key, and double-check `KALSHI_API_KEY_ID` matches the Key ID
the UI shows.

## 10c. SQLite backups

```bash
sudo crontab -u kalshi -e
# Add:
0 3 * * * /opt/kalshi-agent/scripts/backup.sh >> /var/log/kalshi-agent/backup.log 2>&1
```

Backups go to `/var/lib/kalshi-agent/backups/`, gzipped, 30-day retention.

## 10d. Liveness alerting (optional but recommended)

Create a check at https://healthchecks.io (free tier) or your alerting system,
copy the ping URL, and set it in `/etc/kalshi-agent/env`:

```ini
LIVENESS_HEARTBEAT_URL=https://hc-ping.com/your-uuid
```

The agent pings every 60s while alive. healthchecks.io alerts you if pings stop.

## 10e. Enabling the LLM strategy

Default strategy is `placeholder`. To switch to the LLM-driven assessor:

1. Add your OpenRouter API key to `/etc/kalshi-agent/env`:
   ```ini
   OPENROUTER_API_KEY=sk-or-v1-...
   ```
2. Edit `/etc/kalshi-agent/config.yaml`:
   ```yaml
   strategy:
     active: llm_assessor
     llm_assessor:
       tickers:
         - KXCPI-26MAY      # actual Kalshi tickers you want assessed
         - KXJOBS-26JUN
       min_edge: 0.04           # require 4%+ edge before signal
       min_confidence: 0.6      # require 0.6+ LLM confidence
       signal_ttl_minutes: 10
       min_seconds_between_signals_per_ticker: 1800

   llm:
     model: anthropic/claude-sonnet-4.6   # any OpenRouter model id
     temperature: 0.2
   ```
3. `sudo systemctl restart kalshi-agent` and watch the dashboard. Most LLM
   assessments will not meet the edge/confidence filter — that's expected and
   intentional. The strategy is conservative by design.

## 11. Switching to live mode

**Do not flip to live until you've watched the service in paper mode for at
least a day and confirmed:**

* placeholder trades are recorded in SQLite
* reconciliation runs without persistent discrepancies
* the halt drill engages within 5 seconds
* the Discord webhook is firing as expected
* daily PnL summary lands at the right moment (23:59 ET)

Then change `mode: paper` to `mode: live` in `/etc/kalshi-agent/config.yaml` (or
override at the CLI with `--mode live` in the systemd `ExecStart=` line) and
restart. The base URL switches from `demo-api.kalshi.co` to
`api.elections.kalshi.com` automatically.
