# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Turbo v2 scoring base — higher round-trip quality for SN-79 miners."""

from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import bittensor as bt

from competitive_utils import param_bool, turbo_v2_kappa_score_tick
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent
from taos.im.protocol.instructions import OrderDirection

_PROFILE_DEFAULTS_V2: dict[str, dict] = {
    "pulse": {
        "max_quantity": 0.46,
        "quantity_scale": 0.96,
        "max_books_per_tick": 12,
        "max_two_sided_per_tick": 5,
        "max_total_instructions": 30,
        "book_rotation_groups": 16,
        "cadence_interval_ns": 20_000_000_000,
        "max_spread_ratio": 0.0017,
        "inactive_book_frac": 0.28,
        "max_requote_per_tick": 5,
        "touch_join_on_requote": 1,
        "min_fill_score": 3.0,
    },
    "edge": {
        "max_quantity": 0.44,
        "quantity_scale": 0.95,
        "relative_threshold": 0.00007,
        "reversion_threshold": 0.00010,
        "max_books_per_tick": 10,
        "max_two_sided_per_tick": 2,
        "max_total_instructions": 24,
        "book_rotation_groups": 12,
        "cadence_interval_ns": 22_000_000_000,
        "max_spread_ratio": 0.0016,
        "inactive_book_frac": 0.26,
        "max_requote_per_tick": 4,
        "min_fill_score": 4.5,
    },
    "forge": {
        "max_quantity": 0.44,
        "quantity_scale": 0.95,
        "relative_threshold": 0.00008,
        "reversion_threshold": 0.00012,
        "max_books_per_tick": 11,
        "max_two_sided_per_tick": 4,
        "max_total_instructions": 28,
        "book_rotation_groups": 10,
        "cadence_interval_ns": 21_000_000_000,
        "max_spread_ratio": 0.0015,
        "inactive_book_frac": 0.28,
        "max_requote_per_tick": 4,
        "min_fill_score": 3.5,
    },
    "apex": {
        "max_quantity": 0.45,
        "quantity_scale": 0.97,
        "relative_threshold": 0.00007,
        "reversion_threshold": 0.00010,
        "max_books_per_tick": 9,
        "max_two_sided_per_tick": 3,
        "max_total_instructions": 26,
        "book_rotation_groups": 12,
        "cadence_interval_ns": 19_000_000_000,
        "max_spread_ratio": 0.0014,
        "inactive_book_frac": 0.24,
        "max_requote_per_tick": 6,
        "touch_join_on_requote": 1,
        "min_fill_score": 5.0,
        "max_cancel_per_book": 3,
    },
    "select": {
        "max_quantity": 0.42,
        "quantity_scale": 0.94,
        "relative_threshold": 0.00009,
        "reversion_threshold": 0.00011,
        "max_books_per_tick": 8,
        "max_two_sided_per_tick": 2,
        "max_total_instructions": 22,
        "book_rotation_groups": 14,
        "cadence_interval_ns": 23_000_000_000,
        "max_spread_ratio": 0.0014,
        "inactive_book_frac": 0.22,
        "max_requote_per_tick": 5,
        "touch_join_on_requote": 1,
        "min_fill_score": 6.0,
    },
}


class TurboV2ScoringAgent(FinanceSimulationAgent):
    """
    Maker-only v2 agent: stale cancel, fill-score ranking, post-fill requote.

    Run with lazy_load=1 and zero leverage. Prefer v2 over v1 when you have
    instruction headroom and want higher realized round-trip completion rate.
    """

    agent_label: str = "TurboV2ScoringAgent"
    default_turbo_profile: str = "forge"

    def initialize(self) -> None:
        profile = str(
            getattr(self.config, "turbo_profile", self.default_turbo_profile)
        ).lower()
        self.turbo_profile = profile
        defaults = _PROFILE_DEFAULTS_V2.get(profile, _PROFILE_DEFAULTS_V2["forge"])

        self.min_quantity = float(getattr(self.config, "min_quantity", 0.28))
        self.max_quantity = float(
            getattr(self.config, "max_quantity", defaults["max_quantity"])
        )
        self.quantity_scale = float(
            getattr(self.config, "quantity_scale", defaults["quantity_scale"])
        )
        self.max_fee_rate = float(getattr(self.config, "max_fee_rate", 0.002))
        self.relative_threshold = float(
            getattr(
                self.config,
                "relative_threshold",
                defaults.get("relative_threshold", 0.00008),
            )
        )
        self.reversion_threshold = float(
            getattr(
                self.config,
                "reversion_threshold",
                defaults.get("reversion_threshold", 0.00012),
            )
        )
        self.cadence_interval_ns = int(
            getattr(self.config, "cadence_interval_ns", defaults["cadence_interval_ns"])
        )
        self.inventory_skew_soft = float(
            getattr(self.config, "inventory_skew_soft", 0.024)
        )
        self.inventory_skew_hard = float(
            getattr(self.config, "inventory_skew_hard", 0.044)
        )
        self.max_books_per_tick = int(
            getattr(self.config, "max_books_per_tick", defaults["max_books_per_tick"])
        )
        self.book_rotation_groups = int(
            getattr(
                self.config, "book_rotation_groups", defaults["book_rotation_groups"]
            )
        )
        self.max_spread_ratio = float(
            getattr(self.config, "max_spread_ratio", defaults["max_spread_ratio"])
        )
        self.inactive_book_frac = float(
            getattr(self.config, "inactive_book_frac", defaults["inactive_book_frac"])
        )
        self.max_two_sided_per_tick = int(
            getattr(
                self.config,
                "max_two_sided_per_tick",
                defaults["max_two_sided_per_tick"],
            )
        )
        self.max_instructions_per_book = int(
            getattr(self.config, "max_instructions_per_book", 4)
        )
        self.max_total_instructions = int(
            getattr(
                self.config,
                "max_total_instructions",
                defaults["max_total_instructions"],
            )
        )
        self.expiry_period = int(
            getattr(self.config, "expiry_period", 180_000_000_000)
        )
        self.cancel_stale = param_bool(getattr(self.config, "cancel_stale", 1), True)
        self.max_cancel_per_book = int(
            getattr(self.config, "max_cancel_per_book", defaults.get("max_cancel_per_book", 2))
        )
        self.max_requote_per_tick = int(
            getattr(
                self.config,
                "max_requote_per_tick",
                defaults.get("max_requote_per_tick", 4),
            )
        )
        self.touch_join_on_requote = param_bool(
            getattr(
                self.config,
                "touch_join_on_requote",
                defaults.get("touch_join_on_requote", 0),
            ),
            False,
        )
        self.min_fill_score = float(
            getattr(self.config, "min_fill_score", defaults.get("min_fill_score", 0.0))
        )
        self.min_spread_ticks_rt = float(
            getattr(self.config, "min_spread_ticks_rt", 3.0)
        )
        self.requote_ttl_ticks = int(getattr(self.config, "requote_ttl_ticks", 6))

        self.direction: dict[int, OrderDirection] = {}
        self._last_mid: dict[int, float] = {}
        self._mids_scratch: dict[int, float] = {}
        self._requote: dict[int, tuple[OrderDirection, int]] = {}

        self.debug_state_log = param_bool(
            getattr(self.config, "debug_state_log", 0), False
        )
        self.debug_state_log_ticks = int(
            getattr(self.config, "debug_state_log_ticks", 3)
        )
        self._debug_state_tick = 0

        bt.logging.info(
            f"{self.agent_label} | profile={self.turbo_profile} v2 "
            f"qty[{self.min_quantity},{self.max_quantity}] "
            f"books/tick={self.max_books_per_tick} "
            f"instr_cap={self.max_total_instructions} "
            f"requote={self.max_requote_per_tick} "
            f"min_fill={self.min_fill_score}"
        )

    def _completion_side(self, event: TradeEvent) -> OrderDirection:
        """After a maker fill, quote the opposite side to complete the round trip."""
        return OrderDirection.BUY if event.side == 0 else OrderDirection.SELL

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        if event.makerAgentId != self.uid or event.bookId is None:
            return
        self._requote[event.bookId] = (self._completion_side(event), 0)

    def _decay_requote(self) -> None:
        expired: list[int] = []
        for book_id, (side, age) in self._requote.items():
            age += 1
            if age > self.requote_ttl_ticks:
                expired.append(book_id)
            else:
                self._requote[book_id] = (side, age)
        for book_id in expired:
            del self._requote[book_id]

    def _requote_hints(self) -> dict[int, OrderDirection]:
        return {book_id: side for book_id, (side, _age) in self._requote.items()}

    def update(self, state: MarketSimulationStateUpdate) -> None:
        if self.debug_state_log and self._debug_state_tick < self.debug_state_log_ticks:
            self._debug_state_tick += 1
            bt.logging.info(
                f"[{self.agent_label}-VAL-STATE] tick={self._debug_state_tick} "
                f"uid={self.uid} sim_T={state.timestamp} "
                f"books={len(state.books or {})} "
                f"notices={len((state.notices or {}).get(self.uid, []))} "
                f"requote_pending={len(self._requote)}"
            )
        super().update(state)

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        self._decay_requote()
        response = FinanceAgentResponse(agent_id=self.uid)
        turbo_v2_kappa_score_tick(
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
            reversion_threshold=self.reversion_threshold,
            relative_threshold=self.relative_threshold,
            cadence_interval_ns=self.cadence_interval_ns,
            inventory_skew_soft=self.inventory_skew_soft,
            inventory_skew_hard=self.inventory_skew_hard,
            expiry_period=self.expiry_period,
            max_books_per_tick=self.max_books_per_tick,
            book_rotation_groups=self.book_rotation_groups,
            max_spread_ratio=self.max_spread_ratio,
            inactive_book_frac=self.inactive_book_frac,
            max_two_sided_per_tick=self.max_two_sided_per_tick,
            max_instructions_per_book=self.max_instructions_per_book,
            max_total_instructions=self.max_total_instructions,
            turbo_profile=self.turbo_profile,
            cancel_stale=self.cancel_stale,
            max_cancel_per_book=self.max_cancel_per_book,
            max_requote_per_tick=self.max_requote_per_tick,
            touch_join_on_requote=self.touch_join_on_requote,
            min_fill_score=self.min_fill_score,
            min_spread_ticks_rt=self.min_spread_ticks_rt,
        )
        return response
