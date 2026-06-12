# AdaptiveSteadyMaker — SN-79 Option B (inside-spread + microprice-only bias)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import bittensor as bt

from _adaptive_signals import MicropriceSignalEngine
from _maker_common import (
    BookHealth,
    CompletionHint,
    InstructionBudget,
    completion_price,
    free_qty,
    inventory_skew,
    param_bool,
    round_price,
    round_qty,
)
from taos.common.agents import launch
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent
from taos.im.protocol.instructions import OrderDirection, TimeInForce


class AdaptiveSteadyMaker(FinanceSimulationAgent):
    """Option B: inside-spread maker with microprice-only side selection."""

    agent_label = "AdaptiveSteadyMaker"

    def initialize(self) -> None:
        self.history_len = 0
        self.rotation_groups = int(getattr(self.config, "rotation_groups", 12))
        self.max_books_per_tick = int(getattr(self.config, "max_books_per_tick", 11))
        self.max_total_instructions = int(
            getattr(self.config, "max_total_instructions", 28)
        )
        self.max_per_book = int(getattr(self.config, "max_instructions_per_book", 4))
        self.min_spread_ticks = float(getattr(self.config, "min_spread_ticks", 5.0))
        self.max_spread_ratio = float(getattr(self.config, "max_spread_ratio", 0.002))
        self.max_maker_fee = float(getattr(self.config, "max_fee_rate", 0.0013))
        self.base_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.min_quantity = float(getattr(self.config, "min_quantity", 0.25))
        self.quantity_scale = float(getattr(self.config, "quantity_scale", 1.0))
        self.inside_ticks = int(getattr(self.config, "inside_ticks", 1))
        self.micro_threshold = float(getattr(self.config, "micro_threshold", 0.5))
        self.signal_ewm_alpha = float(getattr(self.config, "signal_ewm_alpha", 0.30))
        self.min_rt_edge_ticks = float(
            getattr(self.config, "min_completion_rt_edge_ticks", 3.0)
        )
        self.completion_max_age_ns = int(
            getattr(self.config, "completion_max_age_ns", 12_000_000_000)
        )
        self.expiry_period = int(getattr(self.config, "expiry_period", 180_000_000_000))
        self.inventory_hard = float(getattr(self.config, "inventory_skew_hard", 0.20))
        self.inventory_soft = float(getattr(self.config, "inventory_skew_soft", 0.10))
        self.stale_ticks_outside = int(getattr(self.config, "stale_ticks_outside", 3))
        self.stale_age_ns = int(getattr(self.config, "stale_age_ns", 8_000_000_000))
        self.repay_loans_per_tick = int(getattr(self.config, "repay_loans_per_tick", 1))
        self.max_blacklisted_books = int(
            getattr(self.config, "max_blacklisted_books", 46)
        )
        self.cancel_all_on_startup = param_bool(
            getattr(self.config, "cancel_all_on_startup", 1), True
        )

        self._signal = MicropriceSignalEngine(alpha=self.signal_ewm_alpha)
        self._completions: dict[int, CompletionHint] = {}
        self._book_health: dict[int, BookHealth] = {}
        self._tick = 0
        self._bucket = 0
        self._startup_cancel_done = not self.cancel_all_on_startup

        bt.logging.info(
            f"{self.agent_label} | rot={self.rotation_groups} books/tick={self.max_books_per_tick} "
            f"min_spread={self.min_spread_ticks} inside={self.inside_ticks} "
            f"min_rt_edge={self.min_rt_edge_ticks} qty={self.base_quantity}"
        )

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        if event.makerAgentId != self.uid:
            return
        book_id = event.bookId
        if book_id in self._completions:
            return
        comp_side = OrderDirection.BUY if event.side == 0 else OrderDirection.SELL
        self._completions[book_id] = CompletionHint(
            book_id=book_id,
            side="BUY" if comp_side == OrderDirection.BUY else "SELL",
            fill_price=float(event.price),
            fill_qty=float(event.quantity),
            queued_ts_ns=int(event.timestamp),
        )

    def onOrderRejected(self, event) -> None:
        msg = str(getattr(event, "message", "")).lower()
        book_id = getattr(event, "bookId", None)
        if book_id is None or "loan" not in msg:
            return
        if self._blacklist_count() < self.max_blacklisted_books:
            self._book_health.setdefault(book_id, BookHealth()).blacklist_until = (
                self._tick + 15
            )

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        response = FinanceAgentResponse(agent_id=self.uid)
        if not self.simulation_config:
            return response

        pdec = self.simulation_config.priceDecimals
        vdec = self.simulation_config.volumeDecimals
        tick = 10.0 ** (-pdec)
        budget = InstructionBudget(self.max_total_instructions, self.max_per_book)

        if not self._startup_cancel_done:
            self._startup_cancel_all(response)
            if self._count_open_orders() > 0:
                return response
            self._startup_cancel_done = True
            bt.logging.info(f"{self.agent_label} startup cancel-all complete")

        self._repay_loans(response, budget)
        self._cancel_stale(state, response, budget, tick)
        self._place_completions(state, response, budget, tick, pdec, vdec)
        self._flatten_inventory(state, response, budget, pdec, vdec)
        self._place_inside_quotes(state, response, budget, tick, pdec, vdec)

        self._tick += 1
        self._bucket = (self._bucket + 1) % max(self.rotation_groups, 1)
        return response

    def _startup_cancel_all(self, response: FinanceAgentResponse) -> None:
        n = 0
        for book_id, account in self.accounts.items():
            if not account.orders:
                continue
            ids = [o.id for o in account.orders]
            if ids:
                response.cancel_orders(book_id, ids)
                n += len(ids)
                if n >= self.max_total_instructions:
                    break

    def _count_open_orders(self) -> int:
        return sum(len(a.orders) for a in self.accounts.values())

    def _blacklist_count(self) -> int:
        return sum(
            1 for bh in self._book_health.values() if bh.blacklist_until > self._tick
        )

    def _repay_loans(self, response: FinanceAgentResponse, budget: InstructionBudget):
        repaid = 0
        for book_id, account in self.accounts.items():
            if repaid >= self.repay_loans_per_tick or not budget.ok(book_id):
                continue
            if getattr(account, "loans", None):
                response.close_positions(book_id)
                budget.use(book_id)
                repaid += 1

    def _cancel_stale(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: InstructionBudget,
        tick: float,
    ) -> None:
        cancelled = 0
        for book_id, account in self.accounts.items():
            if not budget.ok(book_id) or not account.orders:
                continue
            book = state.books.get(book_id)
            if not book or not book.bids or not book.asks:
                continue
            bid_p, ask_p = book.bids[0].price, book.asks[0].price
            stale_ids = []
            for o in account.orders:
                age_ns = state.timestamp - o.timestamp
                is_stale = age_ns > self.stale_age_ns
                if not is_stale:
                    if o.side == OrderDirection.BUY:
                        is_stale = o.price < bid_p - self.stale_ticks_outside * tick
                    else:
                        is_stale = o.price > ask_p + self.stale_ticks_outside * tick
                if is_stale:
                    stale_ids.append(o.id)
            if stale_ids:
                response.cancel_orders(book_id, stale_ids)
                budget.use(book_id)
                cancelled += len(stale_ids)
            if cancelled >= 12:
                break

    def _round_qty(self, qty: float, vdec: int) -> float:
        qty = max(self.min_quantity, qty * self.quantity_scale)
        return round_qty(qty, vdec)

    def _place_completions(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: InstructionBudget,
        tick: float,
        pdec: int,
        vdec: int,
    ) -> None:
        done: list[int] = []
        for book_id, hint in list(self._completions.items()):
            if not budget.ok(book_id):
                continue
            book = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if not book or not account or not book.bids or not book.asks:
                continue
            bid_p, ask_p = book.bids[0].price, book.asks[0].price
            if state.timestamp - hint.queued_ts_ns > self.completion_max_age_ns:
                done.append(book_id)
                continue

            priced = completion_price(
                hint, bid_p, ask_p, tick, pdec, self.min_rt_edge_ticks
            )
            if priced is None:
                continue
            trade_dir, price = priced
            qty = self._round_qty(max(hint.fill_qty, self.min_quantity), vdec)
            qty = free_qty(trade_dir, qty, price, account, self.min_quantity)
            if qty < self.min_quantity:
                continue

            response.limit_order(
                book_id,
                trade_dir,
                qty,
                price,
                postOnly=True,
                timeInForce=TimeInForce.GTT,
                expiryPeriod=min(self.expiry_period, self.completion_max_age_ns // 2),
            )
            budget.use(book_id)
            done.append(book_id)

        for b in done:
            self._completions.pop(b, None)

    def _flatten_inventory(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: InstructionBudget,
        pdec: int,
        vdec: int,
    ) -> None:
        for book_id, account in self.accounts.items():
            if not budget.ok(book_id):
                continue
            book = state.books.get(book_id)
            if not book or not book.bids or not book.asks:
                continue
            bid_p, ask_p = book.bids[0].price, book.asks[0].price
            mid = (bid_p + ask_p) * 0.5
            skew = inventory_skew(account, mid)
            if abs(skew) <= self.inventory_hard:
                continue

            if skew > self.inventory_hard:
                trade_dir = OrderDirection.SELL
                price = round_price(ask_p, pdec)
                qty = self._round_qty(
                    max(account.base_balance.free * 0.25, self.min_quantity), vdec
                )
            else:
                trade_dir = OrderDirection.BUY
                price = round_price(bid_p, pdec)
                qty = self._round_qty(
                    max(
                        account.quote_balance.free * 0.25 / max(ask_p, 1e-9),
                        self.min_quantity,
                    ),
                    vdec,
                )
            qty = free_qty(trade_dir, qty, price, account, self.min_quantity)
            if qty < self.min_quantity:
                continue
            if trade_dir == OrderDirection.BUY and price >= ask_p:
                continue
            if trade_dir == OrderDirection.SELL and price <= bid_p:
                continue
            response.limit_order(book_id, trade_dir, qty, price, postOnly=True)
            budget.use(book_id)

    def _place_inside_quotes(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: InstructionBudget,
        tick: float,
        pdec: int,
        vdec: int,
    ) -> None:
        book_count = self.simulation_config.book_count
        placed = 0
        candidates: list[tuple] = []
        micro_cutoff = self.micro_threshold * 0.25

        for book_id in range(book_count):
            if book_id % self.rotation_groups != self._bucket:
                continue
            bh = self._book_health.setdefault(book_id, BookHealth())
            if bh.blacklist_until > self._tick or book_id in self._completions:
                continue

            book = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if not book or not account or not book.bids or not book.asks:
                continue

            bid_p, ask_p = book.bids[0].price, book.asks[0].price
            mid = (bid_p + ask_p) * 0.5
            spread = ask_p - bid_p
            if mid <= 0 or spread <= 0:
                continue

            spread_ticks = spread / tick
            if spread_ticks < self.min_spread_ticks:
                continue
            if spread / mid > self.max_spread_ratio:
                continue
            if spread_ticks < (2 * self.inside_ticks + 1):
                continue
            try:
                if account.fees.maker_fee_rate >= self.max_maker_fee:
                    continue
            except Exception:
                pass

            signal = self._signal.compute(book_id, book)
            skew = inventory_skew(account, mid)
            score = (
                0.5 * min(spread_ticks / 8.0, 1.0)
                + 0.3 * abs(signal)
                + 0.2
                * (
                    account.quote_balance.free
                    / max(account.quote_balance.total, 1.0)
                )
            )
            candidates.append((score, book_id, bid_p, ask_p, signal, skew, account))

        candidates.sort(key=lambda x: -x[0])

        for score, book_id, bid_p, ask_p, signal, skew, account in candidates:
            if placed >= self.max_books_per_tick or budget.remaining <= 0:
                break
            if not budget.ok(book_id):
                continue

            if signal > micro_cutoff:
                trade_dir = OrderDirection.SELL
                price = round_price(ask_p - self.inside_ticks * tick, pdec)
            elif signal < -micro_cutoff:
                trade_dir = OrderDirection.BUY
                price = round_price(bid_p + self.inside_ticks * tick, pdec)
            elif skew > self.inventory_soft:
                trade_dir = OrderDirection.SELL
                price = round_price(ask_p - self.inside_ticks * tick, pdec)
            elif skew < -self.inventory_soft:
                trade_dir = OrderDirection.BUY
                price = round_price(bid_p + self.inside_ticks * tick, pdec)
            else:
                trade_dir = (
                    OrderDirection.SELL
                    if (book_id + self._tick) % 2 == 0
                    else OrderDirection.BUY
                )
                price = round_price(
                    ask_p - self.inside_ticks * tick
                    if trade_dir == OrderDirection.SELL
                    else bid_p + self.inside_ticks * tick,
                    pdec,
                )

            qty = self._round_qty(self.base_quantity, vdec)
            qty = free_qty(trade_dir, qty, price, account, self.min_quantity)
            if qty < self.min_quantity:
                continue
            if trade_dir == OrderDirection.BUY and price >= ask_p:
                continue
            if trade_dir == OrderDirection.SELL and price <= bid_p:
                continue
            try:
                if price in {o.price for o in account.orders}:
                    continue
            except Exception:
                pass

            response.limit_order(
                book_id,
                trade_dir,
                qty,
                price,
                postOnly=True,
                timeInForce=TimeInForce.GTC,
            )
            budget.use(book_id)
            placed += 1


if __name__ == "__main__":
    launch(AdaptiveSteadyMaker)
