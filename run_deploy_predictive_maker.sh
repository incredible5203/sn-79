#!/bin/bash
# New miner — PredictiveMakerAgent (Option A from SN-79-prediction-agent-strategy-guide)
# 1. Edit deployments/predictive-maker-1.0.0/miner.env (PM2_NAME, UID, wallet, hotkey, port)
# 2. ./run_deploy_predictive_maker.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/predictive-maker-1.0.0/run.sh" "$@"
