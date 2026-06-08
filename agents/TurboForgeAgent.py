# TurboForgeAgent — balanced turbo profile (recommended default)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_agent_base import TurboScoringAgent
from taos.common.agents import launch


class TurboForgeAgent(TurboScoringAgent):
    """Balanced flatten + two-sided + edge; best starting point for testnet/mainnet."""

    agent_label = "TurboForgeAgent"
    default_turbo_profile = "forge"


if __name__ == "__main__":
    launch(TurboForgeAgent)
