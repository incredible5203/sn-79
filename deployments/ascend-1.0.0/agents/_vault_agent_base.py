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

_VAULT_DEFAULTS: dict[str, float | int] = {
    "max_books_per_tick": 5,
    "max_total_instructions": 14,
    "max_instructions_per_book": 3,
    "max_requote_per_tick": 5,
    "book_rotation_groups": 24,
    "cadence_interval_ns": 24_000_000_000,
    "rotation_windows": 4,
    "min_spread_ticks": 7.0,
    "min_quote_spread_ticks": 8.5,
    "min_rt_edge_ticks": 6.5,
    "min_quote_rt_edge_ticks": 7.0,
    "min_completion_edge_ticks": 8.0,
    "min_microprice_edge_ticks": 2.0,
    "max_spread_ratio": 0.0009,
    "inventory_skew_soft": 0.003,
    "inventory_skew_hard": 0.007,
    "inside_depth_ticks": 1,
    "deep_spread_ticks": 11.0,
    "inactive_book_frac": 0.15,
    "max_tape_imbalance": 0.20,
    "cold_book_volume_threshold": 500.0,
    "risk_off_skewed_books": 2,
    "two_sided_wide_ticks": 12.0,
    "max_flatten_per_tick": 8,
}


class VaultAgent(FinanceSimulationAgent):
    """
    Vault agent — PnL-preserving maker for fast incentive growth.

    Design goals:
      - Median κ₃ → 0.8+ → 1+ with penalty exactly 0
      - Positive, rising realized PnL (no narrow-spread churn)
      - Profitable round-trips across 128 books via rotation + completion legs
    """

    agent_label: str = "VaultAgent"

    def initialize(self) -> None:
        d = _VAULT_DEFAULTS
        self.min_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.max_quantity = float(getattr(self.config, "max_quantity", 0.32))
        self.quantity_scale = float(getattr(self.config, "quantity_scale", 1.0))
        self.max_fee_rate = float(getattr(self.config, "max_fee_rate", 0.001))
        self.expiry_period = int(getattr(self.config, "expiry_period", 180_000_000_000))

        self.max_books_per_tick = int(getattr(self.config, "max_books_per_tick", d["max_books_per_tick"]))
        self.max_total_instructions = int(getattr(self.config, "max_total_instructions", d["max_total_instructions"]))
        self.max_instructions_per_book = int(getattr(self.config, "max_instructions_per_book", d["max_instructions_per_book"]))
        self.max_requote_per_tick = int(getattr(self.config, "max_requote_per_tick", d["max_requote_per_tick"]))
        self.book_rotation_groups = int(getattr(self.config, "book_rotation_groups", d["book_rotation_groups"]))
        self.cadence_interval_ns = int(getattr(self.config, "cadence_interval_ns", d["cadence_interval_ns"]))
        self.rotation_windows = int(getattr(self.config, "rotation_windows", d["rotation_windows"]))
        self.min_spread_ticks = float(getattr(self.config, "min_spread_ticks", d["min_spread_ticks"]))
        self.min_quote_spread_ticks = float(getattr(self.config, "min_quote_spread_ticks", d["min_quote_spread_ticks"]))
        self.min_rt_edge_ticks = float(getattr(self.config, "min_rt_edge_ticks", d["min_rt_edge_ticks"]))
        self.min_quote_rt_edge_ticks = float(getattr(self.config, "min_quote_rt_edge_ticks", d["min_quote_rt_edge_ticks"]))
        self.min_completion_edge_ticks = float(
            getattr(
                self.config,
                "min_completion_edge_ticks",
                getattr(self.config, "min_completion_rt_edge_ticks", d["min_completion_edge_ticks"]),
            )
        )
        self.min_microprice_edge_ticks = float(getattr(self.config, "min_microprice_edge_ticks", d["min_microprice_edge_ticks"]))
        self.max_spread_ratio = float(getattr(self.config, "max_spread_ratio", d["max_spread_ratio"]))
        self.inventory_skew_soft = float(getattr(self.config, "inventory_skew_soft", d["inventory_skew_soft"]))
        self.inventory_skew_hard = float(getattr(self.config, "inventory_skew_hard", d["inventory_skew_hard"]))
        self.inside_depth_ticks = int(getattr(self.config, "inside_depth_ticks", d["inside_depth_ticks"]))
        self.deep_spread_ticks = float(getattr(self.config, "deep_spread_ticks", d["deep_spread_ticks"]))
        self.inactive_book_frac = float(getattr(self.config, "inactive_book_frac", d["inactive_book_frac"]))
        self.max_tape_imbalance = float(getattr(self.config, "max_tape_imbalance", d["max_tape_imbalance"]))
        self.cold_book_volume_threshold = float(getattr(self.config, "cold_book_volume_threshold", d["cold_book_volume_threshold"]))
        self.risk_off_skewed_books = int(getattr(self.config, "risk_off_skewed_books", d["risk_off_skewed_books"]))
        self.two_sided_wide_ticks = float(getattr(self.config, "two_sided_wide_ticks", d["two_sided_wide_ticks"]))
        self.max_flatten_per_tick = int(getattr(self.config, "max_flatten_per_tick", d["max_flatten_per_tick"]))
        self.requote_ttl_ticks = int(getattr(self.config, "requote_ttl_ticks", 6))

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
            f"min_spread={self.min_spread_ticks} min_rt={self.min_rt_edge_ticks} "
            f"min_comp={self.min_completion_edge_ticks} two_sided>={self.two_sided_wide_ticks} "
            f"skew_soft={self.inventory_skew_soft} flatten/tick={self.max_flatten_per_tick} "
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
            min_quote_spread_ticks=self.min_quote_spread_ticks,
            min_rt_edge_ticks=self.min_rt_edge_ticks,
            min_quote_rt_edge_ticks=self.min_quote_rt_edge_ticks,
            min_completion_edge_ticks=self.min_completion_edge_ticks,
            min_microprice_edge_ticks=self.min_microprice_edge_ticks,
            max_spread_ratio=self.max_spread_ratio,
            inventory_skew_soft=self.inventory_skew_soft,
            inventory_skew_hard=self.inventory_skew_hard,
            inside_depth_ticks=self.inside_depth_ticks,
            deep_spread_ticks=self.deep_spread_ticks,
            inactive_book_frac=self.inactive_book_frac,
            max_tape_imbalance=self.max_tape_imbalance,
            cold_book_volume_threshold=self.cold_book_volume_threshold,
            risk_off_skewed_books=self.risk_off_skewed_books,
            two_sided_wide_ticks=self.two_sided_wide_ticks,
            max_flatten_per_tick=self.max_flatten_per_tick,
        )
        return response
