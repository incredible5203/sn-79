# AscendAgent — high-growth SN-79 scoring agent (κ>0.6, Penalty=0, +PnL)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _ascend_agent_base import AscendAgent
from taos.common.agents import launch


class AscendPrimeAgent(AscendAgent):
    agent_label = "AscendPrimeAgent"
    default_ascend_profile = "prime"


if __name__ == "__main__":
    launch(AscendPrimeAgent)
