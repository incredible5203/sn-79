# AscendKappaFastAgent — fast κ growth (UID 65 velocity + surge PnL discipline)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _ascend_agent_base import AscendAgent
from taos.common.agents import launch


class AscendKappaFastAgent(AscendAgent):
    """
    Ascend stack tuned for rapid kappa_score population while keeping:
      - kappa_penalty = 0  (full 128-book rotation + aggressive cold sweep)
      - total_realized_pnl > 0  (4.25-tick completion edge, tight inventory)

    Profile "kappa" blends rocket cadence (7.5s, 13 books/tick) with
    completion gates between rocket (4.0) and surge (4.5).
    """

    agent_label = "AscendKappaFastAgent"
    default_ascend_profile = "kappa"


if __name__ == "__main__":
    launch(AscendKappaFastAgent)
