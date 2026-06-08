# TurboForgeV2Agent — balanced v2 profile (recommended v2 default)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _turbo_v2_agent_base import TurboV2ScoringAgent
from taos.common.agents import launch


class TurboForgeV2Agent(TurboV2ScoringAgent):
    """Balanced v2: cancel stale, fill-score rank, post-fill requote."""

    agent_label = "TurboForgeV2Agent"
    default_turbo_profile = "forge"


if __name__ == "__main__":
    launch(TurboForgeV2Agent)
