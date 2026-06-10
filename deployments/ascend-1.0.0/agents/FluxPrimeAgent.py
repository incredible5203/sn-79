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
    """UID 209 — fast κ growth via flux profile (cold-book sweep + touch-join)."""

    agent_label = "FluxPrimeAgent"
    default_ascend_profile = "flux"


if __name__ == "__main__":
    launch(FluxPrimeAgent)
