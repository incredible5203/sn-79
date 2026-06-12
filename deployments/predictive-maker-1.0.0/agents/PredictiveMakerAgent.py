# PredictiveMakerAgent — SN-79 Option A (touch-join + OFI/microprice signals)
from __future__ import annotations

import os
import sys
from collections import defaultdict

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

import bittensor as bt

from _predictive_signals import BookHealth, CompletionHint, SignalEngine
from taos.common.agents import launch
from taos.im.agents import FinanceSimulationAgent
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.events import TradeEvent
from taos.im.protocol.instructions import OrderDirection, TimeInForce


def _param_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    try:
        return float(val) != 0.0
    except (TypeError, ValueError):
        return default


class _InstructionBudget:
    def __init__(self, total: int, per_book: int):
        self._total_cap = int(total)
        self._per_book = int(per_book)
        self._used = 0
        self._book_used: dict[int, int] = defaultdict(int)

    def ok(self, book_id: int, n: int = 1) -> bool:
        used = self._book_used[book_id]
        return self._used + n <= self._total_cap and used + n <= self._per_book

    def use(self, book_id: int, n: int = 1) -> None:
        self._used += n
        self._book_used[book_id] += n

    @property
    def remaining(self) -> int:
        return self._total_cap - self._used


class PredictiveMakerAgent(FinanceSimulationAgent):
    """
    Option A from SN-79-prediction-agent-strategy-guide:
    inside-spread maker + combined microstructure signal for side selection,
    completion-first round-trips, 12-bucket rotation for penalty=0.
    """

    agent_label = "PredictiveMakerAgent"

    def initialize(self) -> None:
        self.history_len = 0
        self.rotation_groups = int(getattr(self.config, "rotation_groups", 12))
        self.max_books_per_tick = int(getattr(self.config, "max_books_per_tick", 11))
        self.max_total_instructions = int(
            getattr(self.config, "max_total_instructions", 28)
        )
        self.max_per_book = int(getattr(self.config, "max_instructions_per_book", 4))
        self.min_spread_ticks = float(getattr(self.config, "min_spread_ticks", 4.0))
        self.max_spread_ratio = float(getattr(self.config, "max_spread_ratio", 0.003))
        self.max_maker_fee = float(getattr(self.config, "max_fee_rate", 0.0014))
        self.max_tape_imbalance = float(
            getattr(self.config, "max_tape_imbalance", 0.75)
        )
        self.base_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.min_quantity = float(getattr(self.config, "min_quantity", 0.32))
        self.quantity_scale = float(getattr(self.config, "quantity_scale", 1.0))
        self.signal_threshold_strong = float(
            getattr(self.config, "signal_threshold_strong", 0.15)
        )
        self.signal_threshold_weak = float(
            getattr(self.config, "signal_threshold_weak", 0.08)
        )
        self.signal_adverse_skip = float(
            getattr(self.config, "signal_adverse_skip", 0.50)
        )
        self.signal_ewm_alpha = float(getattr(self.config, "signal_ewm_alpha", 0.30))
        self.min_rt_edge_ticks = float(
            getattr(self.config, "min_completion_rt_edge_ticks", 4.0)
        )
        self.inside_ticks = int(getattr(self.config, "inside_ticks", 1))
        self.completion_max_age_ns = int(
            getattr(self.config, "completion_max_age_ns", 12_000_000_000)
        )
        self.expiry_period = int(getattr(self.config, "expiry_period", 180_000_000_000))
        self.inventory_hard = float(getattr(self.config, "inventory_skew_hard", 0.20))
        self.inventory_soft = float(getattr(self.config, "inventory_skew_soft", 0.08))
        self.stale_ticks_outside = int(getattr(self.config, "stale_ticks_outside", 2))
        self.stale_age_ns = int(getattr(self.config, "stale_age_ns", 7_000_000_000))
        self.repay_loans_per_tick = int(getattr(self.config, "repay_loans_per_tick", 1))
        self.blacklist_after_losses = int(
            getattr(self.config, "blacklist_after_losses", 3)
        )
        self.blacklist_duration = int(getattr(self.config, "blacklist_duration", 20))
        self.max_blacklisted_books = int(
            getattr(self.config, "max_blacklisted_books", 46)
        )
        self.cancel_all_on_startup = _param_bool(
            getattr(self.config, "cancel_all_on_startup", 1), True
        )

        self._signal = SignalEngine(alpha=self.signal_ewm_alpha)
        self._completions: dict[int, CompletionHint] = {}
        self._book_health: dict[int, BookHealth] = {}
        self._tick = 0
        self._bucket = 0
        self._startup_cancel_done = not self.cancel_all_on_startup

        bt.logging.info(
            f"{self.agent_label} | rot={self.rotation_groups} books/tick={self.max_books_per_tick} "
            f"min_spread={self.min_spread_ticks} inside={self.inside_ticks} "
            f"min_rt_edge={self.min_rt_edge_ticks} "
            f"signal_strong={self.signal_threshold_strong} qty={self.base_quantity} "
            f"cancel_all_on_startup={self.cancel_all_on_startup}"
        )

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        if event.makerAgentId != self.uid:
            return
        book_id = event.bookId
        if book_id in self._completions:
            return
        comp_side = (
            OrderDirection.BUY if event.side == 0 else OrderDirection.SELL
        )
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
        bh = self._book_health.setdefault(book_id, BookHealth())
        if self._blacklist_count() < self.max_blacklisted_books:
            bh.blacklist_until = self._tick + 15

    def respond(self, state: MarketSimulationStateUpdate) -> FinanceAgentResponse:
        response = FinanceAgentResponse(agent_id=self.uid)
        if not self.simulation_config:
            return response

        cfg = self.simulation_config
        pdec = cfg.priceDecimals
        vdec = cfg.volumeDecimals
        tick = 10.0 ** (-pdec)
        budget = _InstructionBudget(self.max_total_instructions, self.max_per_book)

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
        self._place_predictive_quotes(state, response, budget, tick, pdec, vdec)

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

    def _repay_loans(self, response: FinanceAgentResponse, budget: _InstructionBudget):
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
        budget: _InstructionBudget,
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
        return round(qty, vdec)

    def _round_price(self, price: float, pdec: int) -> float:
        return round(price, pdec)

    def _min_spread_ticks_for_rt(self) -> float:
        """Room for inside entry + inside completion + min RT edge."""
        return 2 * self.inside_ticks + self.min_rt_edge_ticks + 1

    def _spread_ticks_ok(self, spread_ticks: float) -> bool:
        required = max(self.min_spread_ticks, self._min_spread_ticks_for_rt())
        return spread_ticks >= required

    def _instruction_count(self, response: FinanceAgentResponse) -> int:
        return len(response.instructions)

    def _try_limit_order(self, response: FinanceAgentResponse, **kwargs) -> bool:
        before = self._instruction_count(response)
        response.limit_order(**kwargs)
        return self._instruction_count(response) > before

    def _inside_quote_price(
        self,
        trade_dir: OrderDirection,
        bid_p: float,
        ask_p: float,
        tick: float,
        pdec: int,
    ) -> float | None:
        """Safe inside-spread maker price (bid+tick / ask-tick), not touch-join."""
        spread_ticks = (ask_p - bid_p) / tick
        if not self._spread_ticks_ok(spread_ticks):
            return None
        if trade_dir == OrderDirection.BUY:
            price = self._round_price(bid_p + self.inside_ticks * tick, pdec)
            if price >= ask_p:
                return None
            return price
        price = self._round_price(ask_p - self.inside_ticks * tick, pdec)
        if price <= bid_p:
            return None
        return price

    def _inventory_skew(self, account, mid: float) -> float:
        bv = account.base_balance.total * mid
        qv = account.quote_balance.total
        tv = bv + qv
        return (bv - qv) / max(tv, 1.0)

    def _free_qty(
        self, side: OrderDirection, qty: float, price: float, account
    ) -> float:
        if side == OrderDirection.BUY:
            avail = account.quote_balance.free
            if avail < qty * price:
                qty = avail / max(price, 1e-9)
        else:
            qty = min(qty, account.base_balance.free)
        return qty if qty >= self.min_quantity else 0.0

    def _completion_price(
        self,
        hint: CompletionHint,
        bid_p: float,
        ask_p: float,
        tick: float,
        pdec: int,
    ) -> tuple[OrderDirection, float] | None:
        """
        Post-only completion inside spread.
        Never raise BUY above fill-edge or lower SELL below fill+edge.
        """
        edge = self.min_rt_edge_ticks * tick
        spread_ticks = (ask_p - bid_p) / tick
        if not self._spread_ticks_ok(spread_ticks):
            return None

        inside_buy = self._round_price(bid_p + self.inside_ticks * tick, pdec)
        inside_sell = self._round_price(ask_p - self.inside_ticks * tick, pdec)

        if hint.side == "BUY":
            # Sold at fill_price → buy back at most fill_price - edge
            trade_dir = OrderDirection.BUY
            max_pay = self._round_price(hint.fill_price - edge, pdec)
            price = min(max_pay, inside_buy)
            if price >= ask_p or price > max_pay:
                return None
            if (hint.fill_price - price) < edge * 0.99:
                return None
            return trade_dir, price

        # Bought at fill_price → sell at least fill_price + edge
        trade_dir = OrderDirection.SELL
        min_receive = self._round_price(hint.fill_price + edge, pdec)
        price = max(min_receive, inside_sell)
        if price <= bid_p or price >= ask_p or price < min_receive:
            return None
        if (price - hint.fill_price) < edge * 0.99:
            return None
        return trade_dir, price

    def _place_completions(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: _InstructionBudget,
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
            age_ns = state.timestamp - hint.queued_ts_ns
            if age_ns > self.completion_max_age_ns:
                # Re-queue: keep hint alive for flatten/completion on next ticks
                hint.attempts += 1
                hint.queued_ts_ns = state.timestamp
                continue

            qty = self._round_qty(max(hint.fill_qty, self.min_quantity), vdec)
            priced = self._completion_price(hint, bid_p, ask_p, tick, pdec)
            if priced is None:
                continue
            trade_dir, price = priced

            qty = self._free_qty(trade_dir, qty, price, account)
            if qty < self.min_quantity:
                continue

            expiry = min(self.expiry_period, self.completion_max_age_ns // 2)
            if self._try_limit_order(
                response,
                book_id=book_id,
                direction=trade_dir,
                quantity=qty,
                price=price,
                postOnly=True,
                timeInForce=TimeInForce.GTT,
                expiryPeriod=expiry,
            ):
                budget.use(book_id)
                done.append(book_id)

        for b in done:
            self._completions.pop(b, None)

    def _flatten_inventory(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: _InstructionBudget,
        tick: float,
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
            skew = self._inventory_skew(account, mid)
            if abs(skew) <= self.inventory_hard:
                continue

            if skew > self.inventory_hard:
                trade_dir = OrderDirection.SELL
                price = self._inside_quote_price(
                    trade_dir, bid_p, ask_p, tick, pdec
                )
                qty = self._round_qty(max(account.base_balance.free * 0.25, self.min_quantity), vdec)
            else:
                trade_dir = OrderDirection.BUY
                price = self._inside_quote_price(
                    trade_dir, bid_p, ask_p, tick, pdec
                )
                qty = self._round_qty(
                    max(account.quote_balance.free * 0.25 / max(ask_p, 1e-9), self.min_quantity),
                    vdec,
                )
            if price is None:
                continue
            qty = self._free_qty(trade_dir, qty, price, account)
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

    def _place_predictive_quotes(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
        budget: _InstructionBudget,
        tick: float,
        pdec: int,
        vdec: int,
    ) -> None:
        book_count = self.simulation_config.book_count
        placed = 0
        candidates: list[tuple] = []

        for book_id in range(book_count):
            if book_id % self.rotation_groups != self._bucket:
                continue
            bh = self._book_health.setdefault(book_id, BookHealth())
            if bh.blacklist_until > self._tick:
                continue
            if book_id in self._completions:
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
                if account.fees.maker_fee_rate > self.max_maker_fee:
                    continue
            except Exception:
                pass

            buy_vol = sell_vol = 0.0
            for ev in book.events:
                if getattr(ev, "y", None) == "t":
                    q = float(getattr(ev, "quantity", 0.0))
                    s = getattr(ev, "side", -1)
                    if s == 0:
                        buy_vol += q
                    elif s == 1:
                        sell_vol += q
            total = buy_vol + sell_vol
            imb = abs(buy_vol - sell_vol) / max(total, 1e-9)
            if imb > self.max_tape_imbalance:
                continue

            signal = self._signal.compute(book_id, book)
            skew = self._inventory_skew(account, mid)
            score = (
                0.4 * min(spread_ticks / 8.0, 1.0)
                + 0.3 * abs(signal)
                + 0.2 * (1.0 - imb)
                + 0.1
                * (
                    account.quote_balance.free
                    / max(account.quote_balance.total, 1.0)
                )
            )
            candidates.append(
                (score, book_id, bid_p, ask_p, mid, signal, skew, account, imb)
            )

        candidates.sort(key=lambda x: -x[0])

        for score, book_id, bid_p, ask_p, mid, signal, skew, account, imb in candidates:
            if placed >= self.max_books_per_tick or budget.remaining <= 0:
                break
            if not budget.ok(book_id):
                continue

            if signal > self.signal_threshold_strong:
                trade_dir = OrderDirection.SELL
            elif signal < -self.signal_threshold_strong:
                trade_dir = OrderDirection.BUY
            elif skew > self.inventory_soft:
                trade_dir = OrderDirection.SELL
            elif skew < -self.inventory_soft:
                trade_dir = OrderDirection.BUY
            elif signal > self.signal_threshold_weak:
                trade_dir = OrderDirection.SELL
            elif signal < -self.signal_threshold_weak:
                trade_dir = OrderDirection.BUY
            else:
                trade_dir = (
                    OrderDirection.SELL
                    if (book_id + self._tick) % 2 == 0
                    else OrderDirection.BUY
                )

            price = self._inside_quote_price(trade_dir, bid_p, ask_p, tick, pdec)
            if price is None:
                continue

            if abs(signal) > self.signal_adverse_skip:
                if signal > 0 and trade_dir == OrderDirection.BUY:
                    continue
                if signal < 0 and trade_dir == OrderDirection.SELL:
                    continue

            qty = self._round_qty(self.base_quantity, vdec)
            qty = self._free_qty(trade_dir, qty, price, account)
            if qty < self.min_quantity:
                continue

            try:
                if price in {o.price for o in account.orders}:
                    continue
            except Exception:
                pass

            if trade_dir == OrderDirection.BUY and price >= ask_p:
                continue
            if trade_dir == OrderDirection.SELL and price <= bid_p:
                continue

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
    launch(PredictiveMakerAgent)
