# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Vault base — SN-79 high-growth agent (κ→1+, Penalty=0, +PnL)."""

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
)
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent
from taos.im.protocol.instructions import OrderDirection
from vault_engine import vault_score_tick


class VaultAgent(FinanceSimulationAgent):
    """
    Vault agent — always-on maker for fast incentive growth.

    Design goals:
      - Median κ₃ → 0.8+ → 1+ with penalty exactly 0
      - Positive, rising realized PnL
      - High round-trip volume across all 128 books
    """

    agent_label: str = "VaultAgent"

    def initialize(self) -> None:
        self.min_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.max_quantity = float(getattr(self.config, "max_quantity", 0.32))
        self.quantity_scale = float(getattr(self.config, "quantity_scale", 1.0))
        self.max_fee_rate = float(getattr(self.config, "max_fee_rate", 0.001))
        self.expiry_period = int(getattr(self.config, "expiry_period", 180_000_000_000))

        self.max_books_per_tick = int(getattr(self.config, "max_books_per_tick", 8))
        self.max_total_instructions = int(getattr(self.config, "max_total_instructions", 16))
        self.max_instructions_per_book = int(getattr(self.config, "max_instructions_per_book", 2))
        self.max_requote_per_tick = int(getattr(self.config, "max_requote_per_tick", 6))
        self.book_rotation_groups = int(getattr(self.config, "book_rotation_groups", 16))
        self.cadence_interval_ns = int(getattr(self.config, "cadence_interval_ns", 20_000_000_000))
        self.rotation_windows = int(getattr(self.config, "rotation_windows", 6))
        self.min_spread_ticks = float(getattr(self.config, "min_spread_ticks", 2.0))
        self.min_two_sided_ticks = float(getattr(self.config, "min_two_sided_ticks", 3.0))
        self.min_rt_edge_ticks = float(getattr(self.config, "min_rt_edge_ticks", 2.0))
        self.min_completion_edge_ticks = float(getattr(self.config, "min_completion_edge_ticks", 3.0))
        self.max_spread_ratio = float(getattr(self.config, "max_spread_ratio", 0.002))
        self.inventory_skew_soft = float(getattr(self.config, "inventory_skew_soft", 0.06))
        self.inventory_skew_hard = float(getattr(self.config, "inventory_skew_hard", 0.12))
        self.inside_depth_ticks = int(getattr(self.config, "inside_depth_ticks", 1))
        self.max_tape_imbalance = float(getattr(self.config, "max_tape_imbalance", 0.35))
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
            f"{self.agent_label} | qty[{self.min_quantity},{self.max_quantity}] "
            f"books/tick={self.max_books_per_tick} instr_cap={self.max_total_instructions} "
            f"rot={self.book_rotation_groups}x{self.rotation_windows} "
            f"min_spread={self.min_spread_ticks} two_sided>={self.min_two_sided_ticks} "
            f"min_rt={self.min_rt_edge_ticks} min_comp={self.min_completion_edge_ticks} "
            f"skew_soft={self.inventory_skew_soft} "
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
        vault_score_tick(
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
            max_books_per_tick=self.max_books_per_tick,
            max_total_instructions=self.max_total_instructions,
            max_instructions_per_book=self.max_instructions_per_book,
            max_requote_per_tick=self.max_requote_per_tick,
            book_rotation_groups=self.book_rotation_groups,
            cadence_interval_ns=self.cadence_interval_ns,
            rotation_windows=self.rotation_windows,
            min_spread_ticks=self.min_spread_ticks,
            min_two_sided_ticks=self.min_two_sided_ticks,
            min_rt_edge_ticks=self.min_rt_edge_ticks,
            min_completion_edge_ticks=self.min_completion_edge_ticks,
            max_spread_ratio=self.max_spread_ratio,
            inventory_skew_soft=self.inventory_skew_soft,
            inventory_skew_hard=self.inventory_skew_hard,
            inside_depth_ticks=self.inside_depth_ticks,
            max_tape_imbalance=self.max_tape_imbalance,
        )
        return response
