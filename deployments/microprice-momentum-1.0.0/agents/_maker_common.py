"""Shared helpers for SN-79 maker deployment agents."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from taos.im.protocol.instructions import OrderDirection


@dataclass
class CompletionHint:
    book_id: int
    side: str
    fill_price: float
    fill_qty: float
    queued_ts_ns: int


@dataclass
class BookHealth:
    consecutive_losses: int = 0
    blacklist_until: int = 0
    total_rt_pnl: float = 0.0
    rt_count: int = 0


def param_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    try:
        return float(val) != 0.0
    except (TypeError, ValueError):
        return default


class InstructionBudget:
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


def round_price(price: float, pdec: int) -> float:
    return round(price, pdec)


def round_qty(qty: float, vdec: int) -> float:
    return round(qty, vdec)


def inventory_skew(account, mid: float) -> float:
    bv = account.base_balance.total * mid
    qv = account.quote_balance.total
    tv = bv + qv
    return (bv - qv) / max(tv, 1.0)


def free_qty(
    side: OrderDirection, qty: float, price: float, account, min_quantity: float
) -> float:
    if side == OrderDirection.BUY:
        avail = account.quote_balance.free
        if avail < qty * price:
            qty = avail / max(price, 1e-9)
    else:
        qty = min(qty, account.base_balance.free)
    return qty if qty >= min_quantity else 0.0


def completion_price(
    hint: CompletionHint,
    bid_p: float,
    ask_p: float,
    tick: float,
    pdec: int,
    min_rt_edge_ticks: float,
) -> tuple[OrderDirection, float] | None:
    """Post-only completion inside spread; None if min edge cannot be met."""
    edge = min_rt_edge_ticks * tick
    if ask_p - bid_p < 2 * tick:
        return None

    if hint.side == "BUY":
        trade_dir = OrderDirection.BUY
        cap = round_price(ask_p - tick, pdec)
        target = round_price(hint.fill_price - edge, pdec)
        price = min(target, cap)
        if price < bid_p:
            price = round_price(bid_p, pdec)
        if price >= ask_p or (hint.fill_price - price) < edge * 0.99:
            return None
        return trade_dir, price

    trade_dir = OrderDirection.SELL
    floor = round_price(bid_p + tick, pdec)
    target = round_price(hint.fill_price + edge, pdec)
    price = max(target, floor)
    if price > ask_p:
        price = round_price(ask_p, pdec)
    if price <= bid_p or (price - hint.fill_price) < edge * 0.99:
        return None
    return trade_dir, price
