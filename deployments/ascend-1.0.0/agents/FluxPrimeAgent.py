# FluxPrimeAgent — UID 209 order-flow maker (ascend engine, flux profile)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _ascend_agent_base import AscendAgent
from taos.common.agents import launch


class FluxPrimeAgent(AscendAgent):
    """UID 209 — proven ascend_score_tick with flux throughput + PnL-first completion."""

    agent_label = "FluxPrimeAgent"
    default_ascend_profile = "flux"


if __name__ == "__main__":
    launch(FluxPrimeAgent)
