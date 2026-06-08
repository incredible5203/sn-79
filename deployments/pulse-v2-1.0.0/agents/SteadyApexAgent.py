# SteadyApexAgent — conservative non-Turbo maker (UID 10)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _steady_maker_base import SteadyMakerAgent
from taos.common.agents import launch


class SteadyApexAgent(SteadyMakerAgent):
    agent_label = "SteadyApexAgent"
    default_steady_profile = "apex"


if __name__ == "__main__":
    launch(SteadyApexAgent)
