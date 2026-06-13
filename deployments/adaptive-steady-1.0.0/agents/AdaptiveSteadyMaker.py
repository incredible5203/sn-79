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
    has_open_order_near,
    inside_quote_price,
    inventory_skew,
    min_spread_ticks_for_rt,
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
        self.rotation_groups = int(getattr(self.config, "rotation_groups", 8))
        self.max_books_per_tick = int(getattr(self.config, "max_books_per_tick", 10))
        self.max_completions_per_tick = int(
            getattr(self.config, "max_completions_per_tick", 6)
        )
        self.max_total_instructions = int(
            getattr(self.config, "max_total_instructions", 28)
        )
        self.max_per_book = int(getattr(self.config, "max_instructions_per_book", 4))
        self.min_spread_ticks = float(getattr(self.config, "min_spread_ticks", 5.0))
        self.max_spread_ratio = float(getattr(self.config, "max_spread_ratio", 0.003))
        self.max_maker_fee = float(getattr(self.config, "max_fee_rate", 0.0013))
        self.base_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.min_quantity = float(getattr(self.config, "min_quantity", 0.25))
        self.quantity_scale = float(getattr(self.config, "quantity_scale", 1.0))
        self.inside_ticks = int(getattr(self.config, "inside_ticks", 1))
        self.micro_threshold = float(getattr(self.config, "micro_threshold", 0.35))
        self.signal_ewm_alpha = float(getattr(self.config, "signal_ewm_alpha", 0.30))
        self.min_rt_edge_ticks = float(
            getattr(self.config, "min_completion_rt_edge_ticks", 3.0)
        )
        self.flatten_max_qty_mult = float(
            getattr(self.config, "flatten_max_qty_mult", 2.0)
        )
        self.completion_max_age_ns = int(
            getattr(self.config, "completion_max_age_ns", 12_000_000_000)
        )
        self.completion_place_cooldown_ns = int(
            getattr(self.config, "completion_place_cooldown_ns", 2_000_000_000)
        )
        self.completion_max_attempts = int(
            getattr(self.config, "completion_max_attempts", 24)
        )
        self.expiry_period = int(getattr(self.config, "expiry_period", 180_000_000_000))
        self.inventory_hard = float(getattr(self.config, "inventory_skew_hard", 0.15))
        self.inventory_soft = float(getattr(self.config, "inventory_skew_soft", 0.08))
        self.stale_ticks_outside = int(getattr(self.config, "stale_ticks_outside", 2))
        self.stale_age_ns = int(getattr(self.config, "stale_age_ns", 7_000_000_000))
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
            f"completions/tick={self.max_completions_per_tick} min_spread={self.min_spread_ticks} "
            f"inside={self.inside_ticks} min_rt_edge={self.min_rt_edge_ticks} qty={self.base_quantity}"
        )

    def _completion_side(self, event: TradeEvent) -> str:
        # Taker side: 0=bought (we sold) → buy back; 1=sold (we bought) → sell back
        return "BUY" if event.side == 0 else "SELL"

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        if event.makerAgentId != self.uid:
            return
        book_id = event.bookId
        if book_id is None:
            return

        pending = self._completions.get(book_id)
        if pending is not None:
            # Completion leg filled: pending BUY filled on bid (taker sold), etc.
            if (pending.side == "BUY" and event.side == 1) or (
                pending.side == "SELL" and event.side == 0
            ):
                self._completions.pop(book_id, None)
                return

        self._completions[book_id] = CompletionHint(
            book_id=book_id,
            side=self._completion_side(event),
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
        self._flatten_inventory(state, response, budget, tick, pdec, vdec)
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

    def _flatten_cap_qty(self) -> float:
        return self.base_quantity * self.flatten_max_qty_mult

    def _min_rt_spread_ticks(self) -> float:
        return min_spread_ticks_for_rt(self.inside_ticks, self.min_rt_edge_ticks)

    def _spread_ticks_ok(self, spread_ticks: float) -> bool:
        return spread_ticks >= max(self.min_spread_ticks, self._min_rt_spread_ticks())

    def _instruction_count(self, response: FinanceAgentResponse) -> int:
        return len(response.instructions)

    def _try_limit_order(self, response: FinanceAgentResponse, **kwargs) -> bool:
        before = self._instruction_count(response)
        response.limit_order(**kwargs)
        return self._instruction_count(response) > before

    def _completion_relax_ticks(self, attempts: int) -> float:
        if attempts >= 8:
            return max(self.min_rt_edge_ticks - 1.0, 1.0)
        if attempts >= 4:
            return 1.0
        return 0.0

    def _completion_post_only(self, attempts: int) -> bool:
        return attempts < 3

    def _place_completions(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: InstructionBudget,
        tick: float,
        pdec: int,
        vdec: int,
    ) -> None:
        dropped: list[int] = []
        placed = 0
        jobs: list[tuple] = []

        for book_id, hint in self._completions.items():
            age_ns = state.timestamp - hint.queued_ts_ns
            if age_ns > self.completion_max_age_ns:
                hint.attempts += 1
                hint.queued_ts_ns = state.timestamp
            if hint.attempts >= self.completion_max_attempts:
                dropped.append(book_id)
            jobs.append((age_ns, book_id, hint))

        jobs.sort(key=lambda x: -x[0])

        for _age_ns, book_id, hint in jobs:
            if placed >= self.max_completions_per_tick:
                break
            if not budget.ok(book_id):
                continue
            book = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if not book or not account or not book.bids or not book.asks:
                continue

            if (
                hint.last_place_ts_ns
                and state.timestamp - hint.last_place_ts_ns
                < self.completion_place_cooldown_ns
            ):
                continue

            bid_p, ask_p = book.bids[0].price, book.asks[0].price
            relax = self._completion_relax_ticks(hint.attempts)
            priced = completion_price(
                hint,
                bid_p,
                ask_p,
                tick,
                pdec,
                self.min_rt_edge_ticks,
                inside_ticks=self.inside_ticks,
                relax_ticks=relax,
            )
            if priced is None:
                hint.attempts += 1
                continue

            trade_dir, price = priced
            if has_open_order_near(account, trade_dir, price, tick):
                continue

            qty = self._round_qty(max(hint.fill_qty, self.min_quantity), vdec)
            qty = free_qty(trade_dir, qty, price, account, self.min_quantity)
            if qty < self.min_quantity:
                continue

            expiry = min(self.expiry_period, self.completion_max_age_ns // 2)
            if self._try_limit_order(
                response,
                book_id=book_id,
                direction=trade_dir,
                quantity=qty,
                price=price,
                postOnly=self._completion_post_only(hint.attempts),
                timeInForce=TimeInForce.GTT,
                expiryPeriod=expiry,
            ):
                budget.use(book_id)
                placed += 1
                hint.attempts += 1
                hint.last_place_ts_ns = state.timestamp

        for b in dropped:
            self._completions.pop(b, None)

    def _flatten_inventory(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: InstructionBudget,
        tick: float,
        pdec: int,
        vdec: int,
    ) -> None:
        cap = self._flatten_cap_qty()
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
                price = inside_quote_price(
                    trade_dir, bid_p, ask_p, tick, pdec, self.inside_ticks
                )
                qty = self._round_qty(
                    min(account.base_balance.free * 0.10, cap), vdec
                )
            else:
                trade_dir = OrderDirection.BUY
                price = inside_quote_price(
                    trade_dir, bid_p, ask_p, tick, pdec, self.inside_ticks
                )
                qty = self._round_qty(
                    min(
                        account.quote_balance.free * 0.10 / max(ask_p, 1e-9),
                        cap,
                    ),
                    vdec,
                )
            if price is None:
                continue
            qty = free_qty(trade_dir, qty, price, account, self.min_quantity)
            if qty < self.min_quantity:
                continue
            if self._try_limit_order(
                response,
                book_id=book_id,
                direction=trade_dir,
                quantity=qty,
                price=price,
                postOnly=True,
            ):
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
            if bh.blacklist_until > self._tick:
                continue
            pending = self._completions.get(book_id)
            if pending is not None and pending.attempts < 6:
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
            if not self._spread_ticks_ok(spread_ticks):
                continue
            if spread / mid > self.max_spread_ratio:
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
            elif signal < -micro_cutoff:
                trade_dir = OrderDirection.BUY
            elif skew > self.inventory_soft:
                trade_dir = OrderDirection.SELL
            elif skew < -self.inventory_soft:
                trade_dir = OrderDirection.BUY
            else:
                trade_dir = (
                    OrderDirection.SELL
                    if (book_id + self._tick) % 2 == 0
                    else OrderDirection.BUY
                )

            price = inside_quote_price(
                trade_dir, bid_p, ask_p, tick, pdec, self.inside_ticks
            )
            if price is None:
                continue

            qty = self._round_qty(self.base_quantity, vdec)
            qty = free_qty(trade_dir, qty, price, account, self.min_quantity)
            if qty < self.min_quantity:
                continue
            try:
                if price in {o.price for o in account.orders}:
                    continue
            except Exception:
                pass

            if self._try_limit_order(
                response,
                book_id=book_id,
                direction=trade_dir,
                quantity=qty,
                price=price,
                postOnly=True,
                timeInForce=TimeInForce.GTC,
            ):
                budget.use(book_id)
                placed += 1


if __name__ == "__main__":
    launch(AdaptiveSteadyMaker)
