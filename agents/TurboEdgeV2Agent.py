# TurboEdgeV2Agent — cross-book edge with v2 execution quality
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_v2_agent_base import TurboV2ScoringAgent
from taos.common.agents import launch


class TurboEdgeV2Agent(TurboV2ScoringAgent):
    """v2 edge: stricter fill-score filter, fewer but higher-quality legs."""

    agent_label = "TurboEdgeV2Agent"
    default_turbo_profile = "edge"


if __name__ == "__main__":
    launch(TurboEdgeV2Agent)
