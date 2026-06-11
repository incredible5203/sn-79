#!/bin/bash
# MicrostructureEdgeAgent — frozen deployment bundle
# 1. Edit deployments/micro-1.0.0/miner.env (PM2_NAME, UID, wallet, port)
# 2. ./run_deploy_micro.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/micro-1.0.0/run.sh" "$@"
