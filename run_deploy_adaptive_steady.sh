#!/bin/bash
# New miner — AdaptiveSteadyMaker (Option B from SN-79 strategy guide)
# 1. Edit deployments/adaptive-steady-1.0.0/miner.env (PM2_NAME, UID, wallet, hotkey, port)
# 2. ./run_deploy_adaptive_steady.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/adaptive-steady-1.0.0/run.sh" "$@"
