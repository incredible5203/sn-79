#!/bin/bash
# New miner — AscendKappaFastAgent (fast kappa_score + penalty=0 + positive realized PnL)
# 1. Edit deployments/kappa-fast-1.0.0/miner.env (PM2_NAME, UID, wallet, hotkey, port)
# 2. ./run_deploy_kappa_fast.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/kappa-fast-1.0.0/run.sh" "$@"
