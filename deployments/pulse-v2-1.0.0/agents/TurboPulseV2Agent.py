# TurboPulseV2Agent — max round-trip throughput v2
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_v2_agent_base import TurboV2ScoringAgent
from taos.common.agents import launch


class TurboPulseV2Agent(TurboV2ScoringAgent):
    """v2 pulse: more two-sided + touch-join completion legs."""

    agent_label = "TurboPulseV2Agent"
    default_turbo_profile = "pulse"


if __name__ == "__main__":
    launch(TurboPulseV2Agent)
