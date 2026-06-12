#!/bin/bash
# New miner — AscendPredictAgent (Ascend surge + prediction/skew overlay)
# 1. Edit deployments/predict-1.0.0/miner.env (PM2_NAME, UID, wallet, hotkey, port)
# 2. ./run_deploy_predict.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/predict-1.0.0/run.sh" "$@"
