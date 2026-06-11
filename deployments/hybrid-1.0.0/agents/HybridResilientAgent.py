"""
HybridResilientAgent v2 — thin wrapper over ascend_score_tick.ScoreTickEngine
================================================================================
Book subset: 86..127 (42 books)
Profile: BALANCED -- equal-ish weight to presence (roundtrip volume +
         kappa_penalty floor) and alpha (kappa_score / PnL growth).
         This is the "default" profile and the one most directly
         comparable to the original HybridResilientAgent's intent.

THIS FILE FIXES THE PNL-DECAY BUG that was observed in production for
UID 196 (see ascend_score_tick.py module docstring for the full
diagnosis from the agents_2026*.json metrics):

    total_realized_pnl was DECREASING by 3-15.5 bps of round-trip
    volume on EVERY interval -- a systematic fee leak because the old
    "no-loss" completion check used a fixed MIN_RT_EDGE_TICKS=2 (=2bps
    at price~100) which is SMALLER than the ~4-10bps round-trip maker
    fee cost. Every completion "passed" the check but lost money after
    fees.

ascend_score_tick.required_edge() reads account.fees.maker_fee_rate
fresh every tick and requires:

    edge >= price * maker_fee_rate * 2  (both legs)
          + price * (PROFIT_MARGIN_BPS / 10000)  (extra profit)
          + EXTRA_EDGE_TICKS * tick_size  (rounding buffer)

before ANY completion is placed. Combined with the per-book round-trip
floor (presence lane forces >=3 RTs/book/window for kappa_penalty==0)
and the OFI alpha lane (kappa_score growth), this targets all four
requested metrics simultaneously:

    total_realized_pnl     UP  (every completed RT is now net-profitable)
    total_roundtrip_volume UP  (presence lane sized at presence_qty=0.6,
                                 forced to floor on lagging books)
    kappa_score             UP  (alpha lane + positively-skewed RT
                                 distribution from no-loss completions)
    kappa_penalty           0   (every book gets >=3 RTs/window via the
                                 presence lane's behind_floor override)

USAGE
-----
Copy ascend_score_tick.py, _sn79_compat.py, and this file to
~/.taos/agents/. In miner.env:
    AGENT_NAME=HybridResilientAgent
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

BOOK_RANGE = range(86, 128)

PROFILE = LaneProfile(
    presence_qty=0.6,
    alpha_qty=0.6,
    alpha_enabled=True,
    min_spread_ticks_alpha=3.5,
    max_spread_ratio_alpha=0.0030,
    ofi_threshold=0.13,
    ofi_window_ticks=4,
    alpha_books_per_tick=8,
    presence_max_per_tick=12,
)


class HybridResilientAgent:
    def __init__(self, uid: int, config=None, log_dir=None, **kwargs):
        self.uid = uid
        self.config = config
        self.log_dir = log_dir
        self.engine = ScoreTickEngine(
            uid=uid, book_subset=BOOK_RANGE, profile=PROFILE,
            agent_name="HybridResilientAgent")

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
            logger.error(f"HybridResilientAgent error: {e}", exc_info=True)
        elapsed = time.time() - t0
        if elapsed > 2.0:
            logger.warning(f"HybridResilientAgent slow tick: {elapsed:.2f}s")
        inner = unwrap_response(response)
        log_agent_tick(self.uid, events, inner)
        return inner
