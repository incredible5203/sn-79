#!/bin/bash
set -e
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$DEPLOY_ROOT/miner.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$DEPLOY_ROOT/miner.env"
    set +a
fi
SN79_REPO="${SN79_REPO:-}"
if [[ -z "$SN79_REPO" || ! -f "$SN79_REPO/run_miner.sh" ]]; then
    echo "ERROR: Set SN79_REPO in miner.env to your sn-79 checkout (contains run_miner.sh)." >&2
    exit 1
fi
exec "$SN79_REPO/run_miner.sh" -d "$DEPLOY_ROOT" "$@"
