#!/usr/bin/env bash
# Atomic canary deploy for watch-llm-bridge:
# 1. Runs full server pytest suite in an isolated /tmp environment.
# 2. Snapshots live /opt/bridge code on Hetzner to /opt/bridge.prev.
# 3. Rsyncs server/ via DEPLOY.sh and restarts bridge.service.
# 4. Probes local + public /health for up to 15s; auto-rolls back on failure.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$REPO_ROOT/DEPLOY.sh"
DEFAULT_HOST="$(printf '%s\n' "$DEPLOY_RSYNC" | sed -n 's/.* \([^ :]*\):.*/\1/p')"
REMOTE_HOST="${BRIDGE_SSH_HOST:-$DEFAULT_HOST}"
if [[ -z "$REMOTE_HOST" ]]; then
  echo "ERROR: Could not determine SSH target from DEPLOY.sh or BRIDGE_SSH_HOST" >&2
  exit 1
fi
PUBLIC_HEALTH="${BRIDGE_HEALTH_URL:-https://bridge.upperpeas.com/health}"
TEST_DB="/tmp/bridge_canary_$$.db"

cleanup() {
  rm -f "$TEST_DB" "$TEST_DB-wal" "$TEST_DB-shm"
}
trap cleanup EXIT

echo "[1/4] Running isolated pytest suite..."
(
  cd /tmp
  env -i PATH="$PATH" HOME=/tmp \
    BRIDGE_DB="$TEST_DB" \
    BRIDGE_TOKEN=test-token \
    TRELLO_KEY=test-key \
    TRELLO_TOKEN=test-trello \
    "$REPO_ROOT/.venv/bin/pytest" -q "$REPO_ROOT/server/"
)

echo "[2/4] Snapshotting /opt/bridge -> /opt/bridge.prev on $REMOTE_HOST..."
ssh "$REMOTE_HOST" 'mkdir -p /opt/bridge.prev && rsync -a --delete --exclude .env --exclude .venv --exclude "__pycache__" --exclude "*.db" --exclude "*.db-wal" --exclude "*.db-shm" /opt/bridge/ /opt/bridge.prev/'

echo "[3/4] Deploying server/ and restarting bridge.service..."
(
  cd "$REPO_ROOT"
  # shellcheck disable=SC1091
  source ./DEPLOY.sh
  eval "$DEPLOY_RSYNC"
)
ssh "$REMOTE_HOST" 'systemctl restart bridge.service'

echo "[4/4] Probing health..."
healthy=0
for _ in $(seq 1 15); do
  if ssh "$REMOTE_HOST" 'curl -sf http://127.0.0.1:8123/health >/dev/null' && \
     curl -sf "$PUBLIC_HEALTH" | grep -q '"ok":true'; then
    healthy=1
    break
  fi
  sleep 1
done

if [[ "$healthy" -eq 1 ]]; then
  echo "Canary deploy succeeded: $PUBLIC_HEALTH is healthy."
  exit 0
fi

echo "ERROR: Canary health check failed after 15s! Rolling back to /opt/bridge.prev..." >&2
ssh "$REMOTE_HOST" 'journalctl -u bridge.service -n 25 --no-pager' >&2 || true
ssh "$REMOTE_HOST" 'rsync -a --delete --exclude .env --exclude .venv --exclude "__pycache__" --exclude "*.db" --exclude "*.db-wal" --exclude "*.db-shm" /opt/bridge.prev/ /opt/bridge/ && systemctl restart bridge.service'
echo "Rollback applied." >&2
exit 1
