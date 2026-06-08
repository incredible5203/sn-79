# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""SteadyMaker base — non-Turbo SN-79 scoring agent."""

from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import bittensor as bt

from competitive_utils import (
    _InstructionBudget,
    param_bool,
    startup_cancel_all_orders,
    steady_maker_score_tick,
)
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.instructions import OrderDirection

_PROFILE_DEFAULTS: dict[str, dict] = {
    "apex": {
        "max_books_per_tick": 3,
        "max_total_instructions": 8,
        "max_instructions_per_book": 2,
        "book_rotation_groups": 24,
        "cadence_interval_ns": 35_000_000_000,
        "min_spread_ticks": 6.0,
        "min_rt_edge_ticks": 5.0,
        "max_spread_ratio": 0.0010,
        "inventory_skew_soft": 0.006,
        "inventory_skew_hard": 0.014,
    },
    "pulse": {
        "max_books_per_tick": 4,
        "max_total_instructions": 10,
        "max_instructions_per_book": 2,
        "book_rotation_groups": 20,
        "cadence_interval_ns": 30_000_000_000,
        "min_spread_ticks": 5.0,
        "min_rt_edge_ticks": 4.0,
        "max_spread_ratio": 0.0010,
        "inventory_skew_soft": 0.008,
        "inventory_skew_hard": 0.018,
    },
    "forge": {
        "max_books_per_tick": 5,
        "max_total_instructions": 12,
        "max_instructions_per_book": 2,
        "book_rotation_groups": 16,
        "cadence_interval_ns": 28_000_000_000,
        "min_spread_ticks": 5.0,
        "min_rt_edge_ticks": 4.0,
        "max_spread_ratio": 0.0011,
        "inventory_skew_soft": 0.008,
        "inventory_skew_hard": 0.018,
    },
}


class SteadyMakerAgent(FinanceSimulationAgent):
    """
    Conservative maker-only agent for SN-79 kappa scoring.

    Not a Turbo agent: no requote legs, no two-sided churn, no touch-join.
    """

    agent_label: str = "SteadyMakerAgent"
    default_steady_profile: str = "pulse"

    def initialize(self) -> None:
        profile = str(
            getattr(self.config, "steady_profile", self.default_steady_profile)
        ).lower()
        self.steady_profile = profile
        defaults = _PROFILE_DEFAULTS.get(profile, _PROFILE_DEFAULTS["pulse"])

        self.min_quantity = float(getattr(self.config, "min_quantity", 0.30))
        self.max_quantity = float(
            getattr(self.config, "max_quantity", defaults.get("max_quantity", 0.30))
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
                defaults.get("inventory_skew_soft", 0.008),
            )
        )
        self.inventory_skew_hard = float(
            getattr(
                self.config,
                "inventory_skew_hard",
                defaults.get("inventory_skew_hard", 0.018),
            )
        )
        self.min_spread_ticks = float(
            getattr(
                self.config,
                "min_spread_ticks",
                defaults.get("min_spread_ticks", 6.0),
            )
        )
        self.min_rt_edge_ticks = float(
            getattr(
                self.config,
                "min_rt_edge_ticks",
                defaults.get("min_rt_edge_ticks", 5.0),
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
                defaults.get("max_instructions_per_book", 2),
            )
        )
        self.max_total_instructions = int(
            getattr(
                self.config,
                "max_total_instructions",
                defaults.get("max_total_instructions", 10),
            )
        )
        self.book_rotation_groups = int(
            getattr(
                self.config,
                "book_rotation_groups",
                defaults.get("book_rotation_groups", 20),
            )
        )
        self.cadence_interval_ns = int(
            getattr(
                self.config,
                "cadence_interval_ns",
                defaults.get("cadence_interval_ns", 30_000_000_000),
            )
        )
        self.max_spread_ratio = float(
            getattr(
                self.config,
                "max_spread_ratio",
                defaults.get("max_spread_ratio", 0.0010),
            )
        )
        self.max_tape_imbalance = float(
            getattr(self.config, "max_tape_imbalance", 0.35)
        )

        self.cancel_all_on_startup = param_bool(
            getattr(self.config, "cancel_all_on_startup", True),
            True,
        )
        self._startup_cancel_active = self.cancel_all_on_startup

        self.direction: dict[int, OrderDirection] = {}
        self._last_mid: dict[int, float] = {}
        self._mids_scratch: dict[int, float] = {}

        bt.logging.info(
            f"{self.agent_label} | steady_profile={self.steady_profile} "
            f"qty[{self.min_quantity},{self.max_quantity}] "
            f"books/tick={self.max_books_per_tick} "
            f"instr_cap={self.max_total_instructions} "
            f"min_spread_ticks={self.min_spread_ticks} "
            f"cancel_all_on_startup={self.cancel_all_on_startup}"
        )

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
        steady_maker_score_tick(
            response,
            state,
            self.accounts,
            self.simulation_config,
            self.direction,
            last_mid=self._last_mid,
            mids_scratch=self._mids_scratch,
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
            max_tape_imbalance=self.max_tape_imbalance,
        )
        return response
