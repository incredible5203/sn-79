# DEPRECATED: use FluxPrimeAgent for UID 209 (vault_engine bled PnL).
# VaultPrimeAgent — high-growth SN-79 scoring (κ→1+, Penalty=0, +PnL)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _vault_agent_base import VaultAgent
from taos.common.agents import launch


class VaultPrimeAgent(VaultAgent):
    agent_label = "VaultPrimeAgent"


if __name__ == "__main__":
    launch(VaultPrimeAgent)
