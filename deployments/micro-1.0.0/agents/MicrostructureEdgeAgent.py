"""
MicrostructureEdgeAgent v2 — thin wrapper over ascend_score_tick.ScoreTickEngine
==================================================================================
Book subset: 43..85 (43 books)
Profile: ALPHA-WEIGHTED -- this agent leans into the OFI-directional
         lane harder (more books/tick, lower OFI threshold = more
         signals acted on, larger alpha size) since its job is
         primarily kappa_score / total_realized_pnl growth via
         directionally-informed round trips. Presence lane still runs
         to keep kappa_penalty == 0 on this subset.

Same fee-aware required_edge() fix as AscendKappaAgent -- see
ascend_score_tick.py header.

USAGE
-----
Copy ascend_score_tick.py, _sn79_compat.py, and this file to
~/.taos/agents/. In miner.env:
    AGENT_NAME=MicrostructureEdgeAgent
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

BOOK_RANGE = range(43, 86)

PROFILE = LaneProfile(
    presence_qty=0.5,               # protocol-minimum-ish; just enough
                                     # for the round-trip floor here
    alpha_qty=0.75,                 # larger alpha size -- this agent's
                                     # main contribution to PnL/kappa
    alpha_enabled=True,
    min_spread_ticks_alpha=3.0,
    max_spread_ratio_alpha=0.0035,
    ofi_threshold=0.10,              # lower threshold -> more signals
    ofi_window_ticks=5,
    alpha_books_per_tick=10,
    presence_max_per_tick=10,
)


class MicrostructureEdgeAgent:
    def __init__(self, uid: int, config=None, log_dir=None, **kwargs):
        self.uid = uid
        self.config = config
        self.log_dir = log_dir
        self.engine = ScoreTickEngine(
            uid=uid, book_subset=BOOK_RANGE, profile=PROFILE,
            agent_name="MicrostructureEdgeAgent")

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
            logger.error(f"MicrostructureEdgeAgent error: {e}", exc_info=True)
        elapsed = time.time() - t0
        if elapsed > 2.0:
            logger.warning(f"MicrostructureEdgeAgent slow tick: {elapsed:.2f}s")
        inner = unwrap_response(response)
        log_agent_tick(self.uid, events, inner)
        return inner
