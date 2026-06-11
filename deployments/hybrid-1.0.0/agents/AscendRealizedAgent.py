# AscendRealizedAgent — UID 196 realized-PnL-first maker (rocket + overlay)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _ascend_agent_base import AscendAgent
from _realized_overlay import RealizedOverlay
from competitive_utils import ascend_score_tick
from taos.common.agents import launch
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent


class AscendRealizedAgent(AscendAgent):
    """
    Ascend rocket engine with a thin realized-PnL-first overlay:
      - OFI toxicity veto on new quotes
      - Inventory gate (complete before quote)
      - Per-book realized-loss risk-off
    """

    agent_label = "AscendRealizedAgent"
    default_ascend_profile = "rocket"

    def initialize(self) -> None:
        super().initialize()
        self._overlay = RealizedOverlay()

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        super().onTrade(event, validator)
        self._overlay.record_trade(event, self.uid)

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        response = FinanceAgentResponse(agent_id=self.uid)
        if self._run_startup_cancel_all(response):
            return response
        self._decay_requote()
        requote_ids = set(self._requote.keys())
        blocked = self._overlay.apply(
            state,
            self.accounts,
            inventory_skew_soft=self.inventory_skew_soft,
            requote_book_ids=requote_ids,
        )
        ascend_score_tick(
            response,
            state,
            self.accounts,
            self.simulation_config,
            self.direction,
            last_mid=self._last_mid,
            mids_scratch=self._mids_scratch,
            requote_hints=self._requote_hints(),
            min_quantity=self.min_quantity,
            max_quantity=self.max_quantity,
            max_fee_rate=self.max_fee_rate,
            quantity_scale=self.quantity_scale,
            expiry_period=self.expiry_period,
            inventory_skew_soft=self.inventory_skew_soft,
            inventory_skew_hard=self.inventory_skew_hard,
            max_books_per_tick=self.max_books_per_tick,
            max_instructions_per_book=self.max_instructions_per_book,
            max_total_instructions=self.max_total_instructions,
            book_rotation_groups=self.book_rotation_groups,
            cadence_interval_ns=self.cadence_interval_ns,
            max_spread_ratio=self.max_spread_ratio,
            min_spread_ticks=self.min_spread_ticks,
            min_rt_edge_ticks=self.min_rt_edge_ticks,
            min_completion_rt_edge_ticks=self.min_completion_rt_edge_ticks,
            min_quote_spread_ticks=self.min_quote_spread_ticks,
            min_quote_rt_edge_ticks=self.min_quote_rt_edge_ticks,
            min_microprice_edge_ticks=self.min_microprice_edge_ticks,
            max_requote_per_tick=self.max_requote_per_tick,
            max_tape_imbalance=self.max_tape_imbalance,
            cold_book_volume_threshold=self.cold_book_volume_threshold,
            rotation_windows=self.rotation_windows,
            inside_depth_ticks=self.inside_depth_ticks,
            deep_spread_ticks=self.deep_spread_ticks,
            inactive_book_frac=self.inactive_book_frac,
            risk_off_skewed_books=self.risk_off_skewed_books,
            two_sided_wide_ticks=self.two_sided_wide_ticks,
            max_flatten_per_tick=self.max_flatten_per_tick,
            touch_join_spread_ticks=self.touch_join_spread_ticks,
            max_touch_per_tick=self.max_touch_per_tick,
            max_cold_books_per_tick=self.max_cold_books_per_tick,
            overlay_blocked=blocked,
        )
        return response


if __name__ == "__main__":
    launch(AscendRealizedAgent)
