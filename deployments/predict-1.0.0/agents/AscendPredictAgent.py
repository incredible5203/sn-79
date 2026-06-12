# AscendPredictAgent — Ascend surge + CPU prediction overlay + veto layer
from __future__ import annotations

import os
import sys
import time

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import bittensor as bt

from _ascend_agent_base import AscendAgent
from _predict_overlay import PredictOverlay
from competitive_utils import ascend_score_tick
from taos.common.agents import launch
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate


class AscendPredictAgent(AscendAgent):
    """
    Extends AscendAgent with a lightweight sklearn overlay:
      - PassiveAggressiveRegressor per book (next-tick log return)
      - Veto: skip new quotes when prediction disagrees with intended side
      - If prediction and inventory skew agree → size up (capped)
      - Completions / flatten legs remain base size (inside ascend_score_tick)
    """

    agent_label = "AscendPredictAgent"
    default_ascend_profile = "surge"

    def initialize(self) -> None:
        super().initialize()
        self.predict_threshold = float(
            getattr(self.config, "predict_threshold", 0.002)
        )
        self.predict_veto_threshold = float(
            getattr(self.config, "predict_veto_threshold", self.predict_threshold)
        )
        self.predict_veto = int(getattr(self.config, "predict_veto", 1)) != 0
        self.agree_size_k = float(getattr(self.config, "agree_size_k", 0.5))
        self.predict_max_books = int(getattr(self.config, "predict_max_books", 13))
        self.predict_time_budget_ms = float(
            getattr(self.config, "predict_time_budget_ms", 400.0)
        )
        self._overlay: PredictOverlay | None = None
        bt.logging.info(
            f"{self.agent_label} | predict_threshold={self.predict_threshold} "
            f"veto={self.predict_veto} veto_threshold={self.predict_veto_threshold} "
            f"agree_size_k={self.agree_size_k} max_books={self.predict_max_books} "
            f"time_budget_ms={self.predict_time_budget_ms}"
        )

    def _ensure_overlay(self) -> PredictOverlay:
        if self._overlay is None:
            self._overlay = PredictOverlay(
                base_quantity=self.min_quantity,
                max_quantity=self.max_quantity,
                volume_decimals=int(self.simulation_config.volumeDecimals),
                inventory_skew_soft=self.inventory_skew_soft,
                predict_threshold=self.predict_threshold,
                veto_threshold=self.predict_veto_threshold,
                agree_size_k=self.agree_size_k,
                predict_max_books=self.predict_max_books,
                time_budget_ms=self.predict_time_budget_ms,
                veto_enabled=self.predict_veto,
            )
        return self._overlay

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        response = FinanceAgentResponse(agent_id=self.uid)
        if self._run_startup_cancel_all(response):
            return response
        self._decay_requote()

        t0 = time.perf_counter()
        overlay = self._ensure_overlay().overlay(
            state,
            self.accounts,
            self.simulation_config,
            self._last_mid,
        )
        overlay_ms = (time.perf_counter() - t0) * 1000.0

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
            book_quote_qty=overlay.book_quote_qty,
            book_pred_sign=overlay.book_pred_sign,
        )

        total_ms = (time.perf_counter() - t0) * 1000.0
        if total_ms > 500.0:
            bt.logging.warning(
                f"{self.agent_label} slow tick: overlay={overlay_ms:.1f}ms "
                f"total={total_ms:.1f}ms books_sized={len(overlay.book_quote_qty)} "
                f"veto_books={len(overlay.book_pred_sign)}"
            )
        return response


if __name__ == "__main__":
    launch(AscendPredictAgent)
