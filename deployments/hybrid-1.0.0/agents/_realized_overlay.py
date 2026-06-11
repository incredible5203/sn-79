# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Realized-PnL-first overlay for AscendRealizedAgent."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING

from taos.im.protocol.instructions import OrderDirection

if TYPE_CHECKING:
    from taos.im.protocol.events import TradeEvent

OFI_TOXICITY_THRESHOLD = 0.35
PNL_LOSS_THRESHOLD = -0.004
PNL_WINDOW = 300
RISK_OFF_TICKS = 200
INVENTORY_GATE_SOFT_MULT = 0.35


def compute_ofi(book) -> float:
    """Order Flow Imbalance from L3 tape in [-1, +1]."""
    buy_vol = sell_vol = 0.0
    new_bid = new_ask = 0.0
    cancel_bid = cancel_ask = 0.0
    try:
        best_bid = book.bids[0].price if book.bids else 0.0
        best_ask = book.asks[0].price if book.asks else float("inf")
        for ev in book.events:
            y = getattr(ev, "y", None)
            if y is None:
                y = type(ev).__name__[:1].lower()
            if y == "t":
                q = getattr(ev, "quantity", 0.0)
                s = getattr(ev, "side", 0)
                if s == 0:
                    buy_vol += q
                else:
                    sell_vol += q
            elif y == "o":
                q = getattr(ev, "quantity", 0.0)
                s = getattr(ev, "side", 0)
                if s == 0:
                    new_bid += q
                else:
                    new_ask += q
            elif y == "c":
                q = getattr(ev, "quantity", 0.0)
                p = getattr(ev, "price", 0.0)
                if p <= best_bid:
                    cancel_bid += q
                else:
                    cancel_ask += q
    except Exception:
        return 0.0

    buy_pressure = buy_vol + new_bid - cancel_ask
    sell_pressure = sell_vol + new_ask - cancel_bid
    total = buy_pressure + sell_pressure
    if total < 1e-9:
        return 0.0
    return (buy_pressure - sell_pressure) / total


class _BookLedger:
    __slots__ = ("net_qty", "avg_cost")

    def __init__(self) -> None:
        self.net_qty = 0.0
        self.avg_cost = 0.0

    def apply_fill(self, side: str, price: float, qty: float) -> float:
        """Return realized PnL delta when position nets toward flat."""
        realized = 0.0
        if side == "BUY":
            if self.net_qty < 0:
                close_qty = min(qty, abs(self.net_qty))
                realized += (self.avg_cost - price) * close_qty
                self.net_qty += close_qty
                qty -= close_qty
                if abs(self.net_qty) < 1e-12:
                    self.net_qty = 0.0
            if qty > 1e-12:
                total_cost = self.avg_cost * max(self.net_qty, 0.0) + price * qty
                self.net_qty += qty
                if self.net_qty > 1e-12:
                    self.avg_cost = total_cost / self.net_qty
        else:
            if self.net_qty > 0:
                close_qty = min(qty, self.net_qty)
                realized += (price - self.avg_cost) * close_qty
                self.net_qty -= close_qty
                qty -= close_qty
                if abs(self.net_qty) < 1e-12:
                    self.net_qty = 0.0
            if qty > 1e-12:
                total_cost = self.avg_cost * max(-self.net_qty, 0.0) + price * qty
                self.net_qty -= qty
                if self.net_qty < -1e-12:
                    self.avg_cost = total_cost / abs(self.net_qty)
        return realized


class RealizedOverlay:
    """OFI veto, inventory gate, and per-book realized-loss risk-off."""

    def __init__(self) -> None:
        self._tick = 0
        self._ofi: dict[int, float] = {}
        self._ledgers: dict[int, _BookLedger] = defaultdict(_BookLedger)
        self._pnl_window: dict[int, deque] = defaultdict(
            lambda: deque(maxlen=PNL_WINDOW)
        )
        self._risk_off_until: dict[int, int] = {}

    def record_trade(self, event: TradeEvent, uid: int) -> None:
        book_id = event.bookId
        if book_id is None:
            return
        price = float(event.price)
        qty = float(event.quantity)
        if price <= 0 or qty <= 0:
            return

        maker_id = event.makerAgentId
        taker_id = event.takerAgentId
        taker_buy = event.side == 0

        if maker_id == uid:
            our_side = "SELL" if taker_buy else "BUY"
        elif taker_id == uid:
            our_side = "BUY" if taker_buy else "SELL"
        else:
            return

        realized = self._ledgers[book_id].apply_fill(our_side, price, qty)
        if abs(realized) > 1e-12:
            self._pnl_window[book_id].append(realized)

    def apply(
        self,
        state,
        accounts,
        *,
        inventory_skew_soft: float,
        requote_book_ids: set[int],
    ) -> set[int]:
        self._tick += 1
        blocked: set[int] = set()

        if state.books:
            for book_id, book in state.books.items():
                self._ofi[book_id] = compute_ofi(book)

        from competitive_utils import inventory_skew, tick_size

        pdec = state.config.priceDecimals if state.config else 2
        tick = tick_size(pdec)

        for book_id, book in (state.books or {}).items():
            if book_id in self._risk_off_until:
                if self._tick < self._risk_off_until[book_id]:
                    blocked.add(book_id)
                    continue
                del self._risk_off_until[book_id]

            window = self._pnl_window.get(book_id)
            if window and len(window) >= 3:
                rolling = sum(window)
                if rolling < PNL_LOSS_THRESHOLD:
                    self._risk_off_until[book_id] = self._tick + RISK_OFF_TICKS
                    blocked.add(book_id)
                    continue

            account = accounts.get(book_id)
            if account is None:
                continue
            t = None
            try:
                if book.bids and book.asks:
                    bid = book.bids[0].price
                    ask = book.asks[0].price
                    mid = (bid + ask) / 2
                    t = (bid, ask, mid)
            except Exception:
                pass
            if t is None:
                continue
            _bid, _ask, mid = t
            skew = inventory_skew(accounts, book_id, mid)

            if book_id in requote_book_ids:
                continue

            if abs(skew) >= inventory_skew_soft * INVENTORY_GATE_SOFT_MULT:
                blocked.add(book_id)
                continue

            ofi = self._ofi.get(book_id, 0.0)
            if abs(ofi) >= OFI_TOXICITY_THRESHOLD:
                if ofi > 0 and skew <= 0:
                    blocked.add(book_id)
                elif ofi < 0 and skew >= 0:
                    blocked.add(book_id)

        return blocked

    def ofi_opposes(self, book_id: int, trade_dir: OrderDirection) -> bool:
        ofi = self._ofi.get(book_id, 0.0)
        if abs(ofi) < OFI_TOXICITY_THRESHOLD:
            return False
        if trade_dir == OrderDirection.BUY and ofi < -OFI_TOXICITY_THRESHOLD:
            return True
        if trade_dir == OrderDirection.SELL and ofi > OFI_TOXICITY_THRESHOLD:
            return True
        return False
