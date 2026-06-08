# TurboEdgeAgent — cross-book edge + reversion (profile: edge)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_agent_base import TurboScoringAgent
from taos.common.agents import launch


class TurboEdgeAgent(TurboScoringAgent):
    """Directional inside-spread limits on rich/cheap books vs cross-book median."""

    agent_label = "TurboEdgeAgent"
    default_turbo_profile = "edge"


if __name__ == "__main__":
    launch(TurboEdgeAgent)
