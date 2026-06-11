#!/bin/bash
# UID 196 — AscendPulseAgent (UID 65 engine, tweaked params)
# 1. Edit deployments/kappa-1.0.0/miner.env (PM2_NAME, UID, wallet, port)
# 2. ./run_deploy_kappa.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/kappa-1.0.0/run.sh" "$@"
