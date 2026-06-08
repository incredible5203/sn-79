# TurboSelectV2Agent — top fill-probability books only
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_v2_agent_base import TurboV2ScoringAgent
from taos.common.agents import launch


class TurboSelectV2Agent(TurboV2ScoringAgent):
    """
    Select v2: trade only the highest fill-score books each tick.
    Fewer placements, higher completion probability per round trip.
    """

    agent_label = "TurboSelectV2Agent"
    default_turbo_profile = "select"


if __name__ == "__main__":
    launch(TurboSelectV2Agent)
