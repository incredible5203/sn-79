#!/bin/bash
# New miner — TurboApexV2Agent (frozen deployment bundle)
# 1. Edit deployments/apex-v2-1.0.0/miner.env (PM2_NAME, UID, wallet, port)
# 2. ./run_deploy_apex_v2.sh
set -e
export BT_NO_PARSE_CLI_ARGS=false
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$REPO_ROOT/deployments/apex-v2-1.0.0/run.sh" "$@"
