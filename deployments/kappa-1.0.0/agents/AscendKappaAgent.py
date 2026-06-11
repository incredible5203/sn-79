"""
AscendKappaAgent v2 — thin wrapper over ascend_score_tick.ScoreTickEngine
==========================================================================
Book subset: 0..42 (43 books)
Profile: PRESENCE-WEIGHTED -- larger presence size to drive
         total_roundtrip_volume on this subset, moderate alpha.

This agent's primary job is satisfying the >=3 round-trips/book floor
(kappa_penalty -> 0) and contributing strongly to total_roundtrip_volume,
while still running a moderate alpha lane for kappa_score growth.

All round-trip completions go through ScoreTickEngine's fee-aware
required_edge() -- see ascend_score_tick.py header for why this fixes
the previously-monotonic total_realized_pnl decline (every prior
completion was profitable in PRICE terms by ~2 ticks but lost ~3-15bps
after fees; the new edge formula covers both legs' maker fees + a
profit margin before any completion is placed).

USAGE
-----
Copy ascend_score_tick.py, _sn79_compat.py, and this file to
~/.taos/agents/. In miner.env:
    AGENT_NAME=AscendKappaAgent
"""

from __future__ import annotations

import os
import sys
import time
import logging

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _sn79_compat import CompatFinanceAgentResponse, log_agent_tick, unwrap_response
from ascend_score_tick import ScoreTickEngine, LaneProfile

logger = logging.getLogger(__name__)

# Disjoint book range for this agent. Adjust if your deployment uses a
# different book_count than 128.
BOOK_RANGE = range(0, 43)

PROFILE = LaneProfile(
    presence_qty=0.75,             # larger than bare minimum -> more
                                    # round-trip volume per presence cycle
    alpha_qty=0.5,
    alpha_enabled=True,
    min_spread_ticks_alpha=3.0,
    max_spread_ratio_alpha=0.0030,
    ofi_threshold=0.15,
    ofi_window_ticks=4,
    alpha_books_per_tick=6,
    presence_max_per_tick=14,
)


class AscendKappaAgent:
    def __init__(self, uid: int, config=None, log_dir=None, **kwargs):
        self.uid = uid
        self.config = config
        self.log_dir = log_dir
        self.engine = ScoreTickEngine(
            uid=uid, book_subset=BOOK_RANGE, profile=PROFILE,
            agent_name="AscendKappaAgent")

    def process(self, notification):
        notification.acknowledged = True
        return notification

    def handle(self, state) -> object:
        t0 = time.time()
        self.config = getattr(state, "config", None)
        response = CompatFinanceAgentResponse(agent_id=self.uid, accounts={})
        events = []
        try:
            events = state.notices.get(self.uid, [])
            response.set_accounts(state.accounts.get(self.uid, {}))
            self.engine.tick(state, response)
        except Exception as e:
            logger.error(f"AscendKappaAgent error: {e}", exc_info=True)
        elapsed = time.time() - t0
        if elapsed > 2.0:
            logger.warning(f"AscendKappaAgent slow tick: {elapsed:.2f}s")
        inner = unwrap_response(response)
        log_agent_tick(self.uid, events, inner)
        return inner
