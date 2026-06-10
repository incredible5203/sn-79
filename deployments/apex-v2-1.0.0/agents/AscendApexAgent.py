# AscendApexAgent — UID 10 PnL-first maker (apex profile)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _ascend_agent_base import AscendAgent
from taos.common.agents import launch


class AscendApexAgent(AscendAgent):
    agent_label = "AscendApexAgent"
    default_ascend_profile = "apex"


if __name__ == "__main__":
    launch(AscendApexAgent)
