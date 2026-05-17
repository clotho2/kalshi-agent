#!/usr/bin/env bash
# Nightly SQLite backup. Run from cron, e.g.:
#   0 3 * * * /opt/kalshi-agent/scripts/backup.sh
set -euo pipefail
DB="${KALSHI_AGENT_DB:-/var/lib/kalshi-agent/kalshi-agent.db}"
BACKUP_DIR="${KALSHI_AGENT_BACKUP_DIR:-/var/lib/kalshi-agent/backups}"
RETAIN_DAYS="${KALSHI_AGENT_BACKUP_RETAIN_DAYS:-30}"
mkdir -p "$BACKUP_DIR"
DATE=$(date -u +"%Y-%m-%d_%H%M%S")
OUT="$BACKUP_DIR/kalshi-agent-${DATE}.db"
sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
echo "backup written: ${OUT}.gz"
find "$BACKUP_DIR" -name "kalshi-agent-*.db.gz" -mtime "+${RETAIN_DAYS}" -delete
