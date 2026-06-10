#!/bin/bash
# FluxPrimeAgent — UID 209 SN-79 deployment (ascend engine, flux profile)
# 1. Edit deployments/ascend-1.0.0/miner.env (PM2_NAME, EXPECTED_UID, wallet, port)
# 2. ./run_deploy_ascend.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/ascend-1.0.0/run.sh" "$@"
