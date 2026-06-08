# TurboPulseAgent — max two-sided round trips (profile: pulse)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_agent_base import TurboScoringAgent
from taos.common.agents import launch


class TurboPulseAgent(TurboScoringAgent):
    """Fast κ₃ via inside-spread two-sided quotes on the widest-spread calm books."""

    agent_label = "TurboPulseAgent"
    default_turbo_profile = "pulse"


if __name__ == "__main__":
    launch(TurboPulseAgent)
