# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Ascend base — SN-79 high-growth scoring agent (κ>0.6, Penalty=0, +PnL)."""

from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import bittensor as bt

from competitive_utils import (
    _InstructionBudget,
    ascend_score_tick,
    param_bool,
    startup_cancel_all_orders,
)
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent
from taos.im.protocol.instructions import OrderDirection

_PROFILE_DEFAULTS: dict[str, dict] = {
    "rocket": {
        "max_books_per_tick": 13,
        "max_total_instructions": 30,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 12,
        "max_touch_per_tick": 12,
        "max_flatten_per_tick": 14,
        "book_rotation_groups": 10,
        "cadence_interval_ns": 8_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 2.5,
        "min_quote_spread_ticks": 2.5,
        "min_rt_edge_ticks": 2.5,
        "min_quote_rt_edge_ticks": 3.0,
        "min_completion_rt_edge_ticks": 4.0,
        "min_microprice_edge_ticks": 0.5,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 5.5,
        "two_sided_wide_ticks": 4.5,
        "touch_join_spread_ticks": 2.5,
        "inactive_book_frac": 0.0,
        "max_spread_ratio": 0.0015,
        "inventory_skew_soft": 0.0015,
        "inventory_skew_hard": 0.0035,
        "cold_book_volume_threshold": 40.0,
        "max_cold_books_per_tick": 8,
        "max_tape_imbalance": 0.30,
        "risk_off_skewed_books": 7,
    },
    "prime": {
        "max_books_per_tick": 12,
        "max_total_instructions": 29,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 11,
        "max_touch_per_tick": 11,
        "max_flatten_per_tick": 13,
        "book_rotation_groups": 11,
        "cadence_interval_ns": 9_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 2.5,
        "min_quote_spread_ticks": 3.0,
        "min_rt_edge_ticks": 2.5,
        "min_quote_rt_edge_ticks": 3.0,
        "min_completion_rt_edge_ticks": 4.0,
        "min_microprice_edge_ticks": 0.6,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 6.0,
        "two_sided_wide_ticks": 4.5,
        "touch_join_spread_ticks": 3.0,
        "inactive_book_frac": 0.0,
        "max_spread_ratio": 0.0014,
        "inventory_skew_soft": 0.0016,
        "inventory_skew_hard": 0.0035,
        "cold_book_volume_threshold": 50.0,
        "max_cold_books_per_tick": 7,
        "max_tape_imbalance": 0.30,
        "risk_off_skewed_books": 7,
    },
    "surge": {
        "max_books_per_tick": 12,
        "max_total_instructions": 29,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 11,
        "max_touch_per_tick": 12,
        "max_flatten_per_tick": 13,
        "book_rotation_groups": 11,
        "cadence_interval_ns": 9_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 2.5,
        "min_quote_spread_ticks": 3.0,
        "min_rt_edge_ticks": 3.0,
        "min_quote_rt_edge_ticks": 3.5,
        "min_completion_rt_edge_ticks": 4.5,
        "min_microprice_edge_ticks": 0.6,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 6.0,
        "two_sided_wide_ticks": 4.5,
        "touch_join_spread_ticks": 3.0,
        "inactive_book_frac": 0.0,
        "max_spread_ratio": 0.0013,
        "inventory_skew_soft": 0.0016,
        "inventory_skew_hard": 0.0035,
        "cold_book_volume_threshold": 50.0,
        "max_cold_books_per_tick": 7,
        "max_tape_imbalance": 0.30,
        "risk_off_skewed_books": 6,
    },
    "forge": {
        "max_books_per_tick": 12,
        "max_total_instructions": 29,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 11,
        "max_touch_per_tick": 11,
        "max_flatten_per_tick": 13,
        "book_rotation_groups": 11,
        "cadence_interval_ns": 9_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 2.5,
        "min_quote_spread_ticks": 3.0,
        "min_rt_edge_ticks": 3.0,
        "min_quote_rt_edge_ticks": 3.5,
        "min_completion_rt_edge_ticks": 4.5,
        "min_microprice_edge_ticks": 0.6,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 6.0,
        "two_sided_wide_ticks": 4.5,
        "touch_join_spread_ticks": 3.0,
        "inactive_book_frac": 0.0,
        "max_spread_ratio": 0.0013,
        "inventory_skew_soft": 0.0016,
        "inventory_skew_hard": 0.0035,
        "cold_book_volume_threshold": 50.0,
        "max_cold_books_per_tick": 7,
        "max_tape_imbalance": 0.30,
        "risk_off_skewed_books": 6,
    },
    "flux": {
        "max_books_per_tick": 12,
        "max_total_instructions": 29,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 11,
        "max_touch_per_tick": 12,
        "max_flatten_per_tick": 14,
        "book_rotation_groups": 10,
        "cadence_interval_ns": 9_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 2.5,
        "min_quote_spread_ticks": 2.5,
        "min_rt_edge_ticks": 2.5,
        "min_quote_rt_edge_ticks": 3.0,
        "min_completion_rt_edge_ticks": 4.0,
        "min_microprice_edge_ticks": 0.5,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 5.5,
        "two_sided_wide_ticks": 4.5,
        "touch_join_spread_ticks": 2.5,
        "inactive_book_frac": 0.0,
        "max_spread_ratio": 0.0015,
        "inventory_skew_soft": 0.0015,
        "inventory_skew_hard": 0.003,
        "cold_book_volume_threshold": 40.0,
        "max_cold_books_per_tick": 8,
        "max_tape_imbalance": 0.30,
        "risk_off_skewed_books": 7,
    },
    "apex": {
        "max_books_per_tick": 12,
        "max_total_instructions": 29,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 11,
        "max_touch_per_tick": 11,
        "max_flatten_per_tick": 13,
        "book_rotation_groups": 11,
        "cadence_interval_ns": 9_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 2.5,
        "min_quote_spread_ticks": 3.0,
        "min_rt_edge_ticks": 3.0,
        "min_quote_rt_edge_ticks": 3.5,
        "min_completion_rt_edge_ticks": 4.5,
        "min_microprice_edge_ticks": 0.6,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 6.0,
        "two_sided_wide_ticks": 4.5,
        "touch_join_spread_ticks": 3.0,
        "inactive_book_frac": 0.0,
        "max_spread_ratio": 0.0013,
        "inventory_skew_soft": 0.0016,
        "inventory_skew_hard": 0.0035,
        "cold_book_volume_threshold": 50.0,
        "max_cold_books_per_tick": 7,
        "max_tape_imbalance": 0.30,
        "risk_off_skewed_books": 6,
    },
    "recover": {
        "max_books_per_tick": 10,
        "max_total_instructions": 26,
        "max_instructions_per_book": 5,
        "max_requote_per_tick": 10,
        "max_touch_per_tick": 8,
        "max_flatten_per_tick": 16,
        "book_rotation_groups": 11,
        "cadence_interval_ns": 10_000_000_000,
        "rotation_windows": 10,
        "min_spread_ticks": 3.0,
        "min_quote_spread_ticks": 4.0,
        "min_rt_edge_ticks": 3.5,
        "min_quote_rt_edge_ticks": 4.0,
        "min_completion_rt_edge_ticks": 5.5,
        "min_microprice_edge_ticks": 0.8,
        "inside_depth_ticks": 1,
        "deep_spread_ticks": 7.0,
        "two_sided_wide_ticks": 6.0,
        "touch_join_spread_ticks": 4.5,
        "inactive_book_frac": 0.01,
        "max_spread_ratio": 0.0012,
        "inventory_skew_soft": 0.0016,
        "inventory_skew_hard": 0.0035,
        "cold_book_volume_threshold": 50.0,
        "max_cold_books_per_tick": 6,
        "max_tape_imbalance": 0.26,
        "risk_off_skewed_books": 4,
    },
}


class AscendAgent(FinanceSimulationAgent):
    """
    High-growth maker agent for SN-79 incentive scoring.

    Targets: Median κ₃ > 0.6, Penalty = 0, positive & rising realized PnL,
    fast incentive growth via profitable round-trips across all 128 books.
    """

    agent_label: str = "AscendAgent"
    default_ascend_profile: str = "prime"

    def initialize(self) -> None:
        profile = str(
            getattr(self.config, "ascend_profile", self.default_ascend_profile)
        ).lower()
        self.ascend_profile = profile
        defaults = _PROFILE_DEFAULTS.get(profile, _PROFILE_DEFAULTS["prime"])

        self.min_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.max_quantity = float(
            getattr(self.config, "max_quantity", defaults.get("max_quantity", 0.32))
        )
        self.quantity_scale = float(
            getattr(self.config, "quantity_scale", defaults.get("quantity_scale", 1.0))
        )
        self.max_fee_rate = float(getattr(self.config, "max_fee_rate", 0.001))
        self.expiry_period = int(getattr(self.config, "expiry_period", 180_000_000_000))
        self.inventory_skew_soft = float(
            getattr(
                self.config,
                "inventory_skew_soft",
                defaults.get("inventory_skew_soft", 0.003),
            )
        )
        self.inventory_skew_hard = float(
            getattr(
                self.config,
                "inventory_skew_hard",
                defaults.get("inventory_skew_hard", 0.007),
            )
        )
        self.min_spread_ticks = float(
            getattr(
                self.config,
                "min_spread_ticks",
                defaults.get("min_spread_ticks", 7.0),
            )
        )
        self.min_rt_edge_ticks = float(
            getattr(
                self.config,
                "min_rt_edge_ticks",
                defaults.get("min_rt_edge_ticks", 6.5),
            )
        )
        self.min_completion_rt_edge_ticks = float(
            getattr(
                self.config,
                "min_completion_rt_edge_ticks",
                defaults.get("min_completion_rt_edge_ticks", self.min_rt_edge_ticks),
            )
        )
        self.cold_book_volume_threshold = float(
            getattr(
                self.config,
                "cold_book_volume_threshold",
                defaults.get("cold_book_volume_threshold", 500.0),
            )
        )
        self.max_books_per_tick = int(
            getattr(
                self.config,
                "max_books_per_tick",
                defaults.get("max_books_per_tick", 4),
            )
        )
        self.max_instructions_per_book = int(
            getattr(
                self.config,
                "max_instructions_per_book",
                defaults.get("max_instructions_per_book", 3),
            )
        )
        self.max_total_instructions = int(
            getattr(
                self.config,
                "max_total_instructions",
                defaults.get("max_total_instructions", 12),
            )
        )
        self.book_rotation_groups = int(
            getattr(
                self.config,
                "book_rotation_groups",
                defaults.get("book_rotation_groups", 32),
            )
        )
        self.cadence_interval_ns = int(
            getattr(
                self.config,
                "cadence_interval_ns",
                defaults.get("cadence_interval_ns", 28_000_000_000),
            )
        )
        self.max_spread_ratio = float(
            getattr(
                self.config,
                "max_spread_ratio",
                defaults.get("max_spread_ratio", 0.00085),
            )
        )
        self.max_tape_imbalance = float(
            getattr(
                self.config,
                "max_tape_imbalance",
                defaults.get("max_tape_imbalance", 0.20),
            )
        )
        self.max_requote_per_tick = int(
            getattr(
                self.config,
                "max_requote_per_tick",
                defaults.get("max_requote_per_tick", 4),
            )
        )
        self.min_microprice_edge_ticks = float(
            getattr(
                self.config,
                "min_microprice_edge_ticks",
                defaults.get("min_microprice_edge_ticks", 2.0),
            )
        )
        self.min_quote_spread_ticks = float(
            getattr(
                self.config,
                "min_quote_spread_ticks",
                defaults.get(
                    "min_quote_spread_ticks",
                    defaults.get("min_spread_ticks", 9.0),
                ),
            )
        )
        self.min_quote_rt_edge_ticks = float(
            getattr(
                self.config,
                "min_quote_rt_edge_ticks",
                defaults.get(
                    "min_quote_rt_edge_ticks",
                    defaults.get("min_rt_edge_ticks", 7.0),
                ),
            )
        )
        self.rotation_windows = int(
            getattr(
                self.config,
                "rotation_windows",
                defaults.get("rotation_windows", 3),
            )
        )
        self.inside_depth_ticks = int(
            getattr(
                self.config,
                "inside_depth_ticks",
                defaults.get("inside_depth_ticks", 1),
            )
        )
        self.deep_spread_ticks = float(
            getattr(
                self.config,
                "deep_spread_ticks",
                defaults.get("deep_spread_ticks", 11.0),
            )
        )
        self.inactive_book_frac = float(
            getattr(
                self.config,
                "inactive_book_frac",
                defaults.get("inactive_book_frac", 0.25),
            )
        )
        self.risk_off_skewed_books = int(
            getattr(
                self.config,
                "risk_off_skewed_books",
                defaults.get("risk_off_skewed_books", 3),
            )
        )
        self.two_sided_wide_ticks = float(
            getattr(
                self.config,
                "two_sided_wide_ticks",
                defaults.get("two_sided_wide_ticks", 0.0),
            )
        )
        self.touch_join_spread_ticks = float(
            getattr(
                self.config,
                "touch_join_spread_ticks",
                defaults.get("touch_join_spread_ticks", 6.0),
            )
        )
        self.max_touch_per_tick = int(
            getattr(
                self.config,
                "max_touch_per_tick",
                defaults.get("max_touch_per_tick", 8),
            )
        )
        self.max_flatten_per_tick = int(
            getattr(
                self.config,
                "max_flatten_per_tick",
                defaults.get("max_flatten_per_tick", 12),
            )
        )
        self.max_cold_books_per_tick = int(
            getattr(
                self.config,
                "max_cold_books_per_tick",
                defaults.get("max_cold_books_per_tick", 4),
            )
        )
        self.requote_ttl_ticks = int(getattr(self.config, "requote_ttl_ticks", 8))

        self.cancel_all_on_startup = param_bool(
            getattr(self.config, "cancel_all_on_startup", True),
            True,
        )
        self._startup_cancel_active = self.cancel_all_on_startup

        self.direction: dict[int, OrderDirection] = {}
        self._last_mid: dict[int, float] = {}
        self._mids_scratch: dict[int, float] = {}
        self._requote: dict[int, tuple[OrderDirection, int, float]] = {}

        bt.logging.info(
            f"{self.agent_label} | ascend_profile={self.ascend_profile} "
            f"qty[{self.min_quantity},{self.max_quantity}] "
            f"books/tick={self.max_books_per_tick} "
            f"instr_cap={self.max_total_instructions} "
            f"rot={self.book_rotation_groups}x{self.rotation_windows} "
            f"min_spread={self.min_spread_ticks} "
            f"min_rt={self.min_rt_edge_ticks} "
            f"min_comp={self.min_completion_rt_edge_ticks} "
            f"two_sided>={self.two_sided_wide_ticks} "
            f"skew_soft={self.inventory_skew_soft} "
            f"inactive_skip={self.inactive_book_frac} "
            f"cancel_all_on_startup={self.cancel_all_on_startup}"
        )

    def _completion_side(self, event: TradeEvent) -> OrderDirection:
        return OrderDirection.BUY if event.side == 0 else OrderDirection.SELL

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        if event.makerAgentId != self.uid or event.bookId is None:
            return
        self._requote[event.bookId] = (
            self._completion_side(event),
            0,
            float(event.price),
        )

    def _decay_requote(self) -> None:
        expired: list[int] = []
        for book_id, (side, age, fill_price) in self._requote.items():
            age += 1
            if age > self.requote_ttl_ticks:
                expired.append(book_id)
            else:
                self._requote[book_id] = (side, age, fill_price)
        for book_id in expired:
            del self._requote[book_id]

    def _requote_hints(self) -> dict[int, tuple[OrderDirection, float]]:
        return {
            book_id: (side, fill_price)
            for book_id, (side, _age, fill_price) in self._requote.items()
        }

    def _run_startup_cancel_all(self, response: FinanceAgentResponse) -> bool:
        if not self._startup_cancel_active:
            return False
        cancel_budget = _InstructionBudget(5, 28)
        had_orders = any(acct.orders for acct in self.accounts.values())
        if not had_orders:
            self._startup_cancel_active = False
            bt.logging.info(f"{self.agent_label} | startup cancel-all complete (no orders)")
            return False
        complete = startup_cancel_all_orders(
            response, self.accounts, cancel_budget
        )
        if complete:
            self._startup_cancel_active = False
            bt.logging.info(f"{self.agent_label} | startup cancel-all complete")
            return bool(response.instructions)
        bt.logging.info(
            f"{self.agent_label} | startup cancel-all in progress "
            f"({len(response.instructions)} cancels this tick)"
        )
        return True

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        response = FinanceAgentResponse(agent_id=self.uid)
        if self._run_startup_cancel_all(response):
            return response
        self._decay_requote()
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
        )
        return response
