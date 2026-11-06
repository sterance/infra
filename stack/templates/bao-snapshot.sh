#!/usr/bin/env bash
# Rendered by bootstrap.sh. Nightly OpenBao Raft snapshot; run by /etc/cron.d/bao-snapshot.
# Add ${STACK_DIR}/openbao/snapshots to your Backblaze job. Never back up ${STACK_DIR}/openbao/secrets.
set -euo pipefail
cd "${STACK_DIR}"
TOKEN_FILE="${STACK_DIR}/openbao/backup.token"
if [[ ! -s "$TOKEN_FILE" ]]; then
  echo "bao-snapshot: $TOKEN_FILE missing — run bin/bao-configure on your laptop to issue one" >&2
  exit 1
fi
T=$(cat "$TOKEN_FILE")
docker compose exec -T -e BAO_TOKEN="$T" openbao \
  bao operator raft snapshot save "/openbao/snapshots/bao-$(date +%F).snap"
docker compose exec -T -e BAO_TOKEN="$T" openbao bao token renew >/dev/null   # keep the periodic token alive
find "${STACK_DIR}/openbao/snapshots" -name 'bao-*.snap' -mtime "+${SNAPSHOT_KEEP_DAYS}" -delete
