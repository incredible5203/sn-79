# TurboApexV2Agent — theoretical-optimum blend (quality-first v2)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_v2_agent_base import TurboV2ScoringAgent
from taos.common.agents import launch


class TurboApexV2Agent(TurboV2ScoringAgent):
    """
    Apex v2: tight spread filter, touch-join completions, aggressive requote.
    Best when you have instruction headroom and want highest round-trip quality.
    """

    agent_label = "TurboApexV2Agent"
    default_turbo_profile = "apex"


if __name__ == "__main__":
    launch(TurboApexV2Agent)
