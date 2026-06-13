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
    attempts: int = 0
    last_place_ts_ns: int = 0


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


def min_spread_ticks_for_rt(inside_depth: int, min_rt_edge_ticks: float) -> float:
    return 2 * inside_depth + min_rt_edge_ticks + 1


def inside_quote_price(
    trade_dir: OrderDirection,
    bid_p: float,
    ask_p: float,
    tick: float,
    pdec: int,
    inside_ticks: int = 1,
) -> float | None:
    if trade_dir == OrderDirection.BUY:
        price = round_price(bid_p + inside_ticks * tick, pdec)
        if price >= ask_p:
            return None
        return price
    price = round_price(ask_p - inside_ticks * tick, pdec)
    if price <= bid_p:
        return None
    return price


def completion_price(
    hint: CompletionHint,
    bid_p: float,
    ask_p: float,
    tick: float,
    pdec: int,
    min_rt_edge_ticks: float,
    inside_ticks: int = 1,
    relax_ticks: float = 0.0,
) -> tuple[OrderDirection, float] | None:
    """Post-only completion inside spread; relax_ticks lowers required edge on retry."""
    if tick <= 0:
        return None

    edge_ticks = max(min_rt_edge_ticks - relax_ticks, 1.0)
    spread_ticks = (ask_p - bid_p) / tick
    min_spread = min_spread_ticks_for_rt(inside_ticks, edge_ticks)
    if spread_ticks < min_spread:
        if relax_ticks < min_rt_edge_ticks - 1.0 and spread_ticks >= inside_ticks + 2:
            edge_ticks = max(spread_ticks - 2 * inside_ticks - 1, 1.0)
        else:
            return None

    edge = edge_ticks * tick
    inside_buy = round_price(
        min(bid_p + inside_ticks * tick, ask_p - tick), pdec
    )
    inside_sell = round_price(
        max(ask_p - inside_ticks * tick, bid_p + tick), pdec
    )

    if hint.side == "BUY":
        trade_dir = OrderDirection.BUY
        max_pay = round_price(hint.fill_price - edge, pdec)
        price = min(max_pay, inside_buy)
        if price >= ask_p or (hint.fill_price - price) < edge * 0.95:
            return None
        return trade_dir, price

    trade_dir = OrderDirection.SELL
    min_receive = round_price(hint.fill_price + edge, pdec)
    price = max(min_receive, inside_sell)
    if price <= bid_p or price >= ask_p or (price - hint.fill_price) < edge * 0.95:
        return None
    return trade_dir, price


def has_open_order_near(
    account,
    trade_dir: OrderDirection,
    price: float,
    tick: float,
) -> bool:
    for order in account.orders:
        if order.side != trade_dir:
            continue
        if abs(order.price - price) <= tick * 0.51:
            return True
    return False
