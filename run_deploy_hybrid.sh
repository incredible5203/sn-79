#!/bin/bash
# UID 196 — AscendRealizedAgent (rocket + realized-PnL overlay)
# 1. Edit deployments/hybrid-1.0.0/miner.env (PM2_NAME, UID, wallet, port)
# 2. ./run_deploy_hybrid.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/hybrid-1.0.0/run.sh" "$@"
