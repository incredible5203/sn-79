# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
SN-79 scoring-aligned trading helpers (τaos v0.4.5+).

Targets: high Realized PnL, Median Kappa-3, Kappa Score, Trading Score;
          Penalty → 0 via consistent per-book maker round-trips.

Validator defaults (reward.py / taos/im/config):
  - Trading score ≈ 79% Kappa + 21% PnL
  - max_instructions_per_book = 5
  - max_inactive_books_ratio = 37.5%
  - Outlier penalty on weak books vs median (IQR rule)
"""

from __future__ import annotations

import math

from taos.im.protocol.instructions import OrderDirection, STP, TimeInForce
from taos.im.protocol.models import Book


def param_bool(val, default: bool = True) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def tick_size(price_decimals: int) -> float:
    return 10 ** (-price_decimals)


def round_price(price: float, price_decimals: int) -> float:
    return round(price, price_decimals)


def clamp_qty(qty: float, volume_decimals: int) -> float:
    return round(max(qty, 0.0), volume_decimals)


def sim_order_qty(
    min_quantity: float,
    max_quantity: float,
    quantity_scale: float,
    volume_decimals: int,
    *,
    sim_min: float = 0.32,
) -> float:
    """Round-trip safe order size (finney min is 0.31 BASE after wire rounding)."""
    step = 10 ** (-volume_decimals)
    raw = max(min_quantity, max_quantity * quantity_scale, sim_min)
    qty = math.ceil((raw - 1e-12) / step) * step
    return round(qty, volume_decimals)


def _touch(book: Book) -> tuple[float, float, float] | None:
    if not book.bids or not book.asks:
        return None
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    if best_ask <= best_bid:
        return None
    return best_bid, best_ask, (best_bid + best_ask) * 0.5


def microprice(book: Book) -> float | None:
    if not book.bids or not book.asks:
        return None
    bid_p, bid_q = book.bids[0].price, book.bids[0].quantity
    ask_p, ask_q = book.asks[0].price, book.asks[0].quantity
    denom = bid_q + ask_q
    if denom <= 0:
        return None
    return (bid_p * ask_q + ask_p * bid_q) / denom


def inventory_skew(accounts, book_id: int, mid: float) -> float:
    account = accounts[book_id]
    base_val = account.base_balance.free * mid
    quote_val = account.quote_balance.free
    total = base_val + quote_val
    if total <= 0:
        return 0.0
    return (base_val / total) - 0.5


def maker_fee_ok(accounts, book_id: int, max_fee_rate: float) -> bool:
    fees = accounts[book_id].fees
    if fees is None:
        return True
    return fees.maker_fee_rate <= max_fee_rate


def place_limit(
    response,
    book_id: int,
    direction: OrderDirection,
    qty: float,
    price: float,
    expiry_period: int,
    *,
    volume_decimals: int | None = None,
    min_quantity: float = 0.32,
) -> None:
    if volume_decimals is not None:
        qty = sim_order_qty(min_quantity, qty, 1.0, volume_decimals)
    response.limit_order(
        book_id,
        direction,
        qty,
        price,
        stp=STP.CANCEL_BOTH,
        timeInForce=TimeInForce.GTT,
        expiryPeriod=expiry_period,
    )


def _has_loan(accounts, book_id: int) -> bool:
    account = accounts[book_id]
    if account.base_loan > 1e-9 or account.quote_loan > 1e-9:
        return True
    return bool(account.loans)


def repay_loans_fifo(
    response,
    accounts,
    simulation_config,
    *,
    min_quantity: float,
    rotate_key: int,
) -> bool:
    """Repay at most one margin loan per tick (rotating across indebted books)."""
    vdec = simulation_config.volumeDecimals
    indebted = sorted(book_id for book_id in accounts if accounts[book_id].loans)
    if not indebted:
        return False
    book_id = indebted[rotate_key % len(indebted)]
    order_id = min(accounts[book_id].loans.keys())
    qty = sim_order_qty(min_quantity, min_quantity, 1.0, vdec)
    response.close_position(book_id=book_id, order_id=order_id, quantity=qty)
    return True


def turbo_kappa_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    reversion_threshold: float,
    relative_threshold: float,
    cadence_interval_ns: int,
    inventory_skew_soft: float,
    inventory_skew_hard: float,
    expiry_period: int,
    max_books_per_tick: int = 11,
    book_rotation_groups: int = 16,
    max_spread_ratio: float = 0.0018,
    inactive_book_frac: float = 0.30,
    max_two_sided_per_tick: int = 4,
    max_instructions_per_book: int = 4,
    max_total_instructions: int = 28,
    turbo_profile: str = "forge",
) -> None:
    """
    Maker-only scoring engine optimized for fast κ₃ + PnL growth and Penalty → 0.

    Design vs slow mainnet miners:
      - Instruction budget (≤4/book, ≤28–30/tick) → fewer rejects under 5/book cap
      - lazy_load=1 in run script → respond within ~3s validator timeout
      - FIFO loan repay → avoid EXCEEDING_LOAN lockups
      - 3-group rotation across 16 cadence buckets → round-trips on all 128 books
      - Skip worst ~30% books for new risk → stay inside 37.5% inactive tolerance
      - Two-sided inside-spread quotes → realized round-trip observations for κ₃
    """
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    q_span = max_quantity - min_quantity
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    active_rots = {
        rot,
        (rot + 1) % book_rotation_groups,
        (rot + 2) % book_rotation_groups,
    }
    profile = (turbo_profile or "forge").lower()
    if profile == "pulse":
        two_sided_cap = max_two_sided_per_tick + 1
        edge_slots = max(4, max_books_per_tick - two_sided_cap)
    elif profile == "edge":
        two_sided_cap = min(2, max_two_sided_per_tick)
        edge_slots = max_books_per_tick
    else:
        two_sided_cap = max_two_sided_per_tick
        edge_slots = max(3, max_books_per_tick - two_sided_cap)

    instr_by_book: dict[int, int] = {}
    total_instr = 0

    def _can_place(book_id: int, n: int = 1) -> bool:
        nonlocal total_instr
        if total_instr + n > max_total_instructions:
            return False
        if instr_by_book.get(book_id, 0) + n > max_instructions_per_book:
            return False
        return True

    def _mark(book_id: int, n: int = 1) -> None:
        nonlocal total_instr
        instr_by_book[book_id] = instr_by_book.get(book_id, 0) + n
        total_instr += n

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)

    if len(mids_scratch) < 4:
        return
    med = sorted(mids_scratch.values())[len(mids_scratch) // 2]

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    two_sided: list[tuple[float, int, float, float, float]] = []
    edge_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []

    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue

        skew = inventory_skew(accounts, book_id, mid)
        prev_m = last_mid.get(book_id)
        last_mid[book_id] = mid
        ret = (mid - prev_m) / prev_m if prev_m and prev_m > 0 else 0.0
        rel = (mid - med) / med if med > 0 else 0.0
        mp = microprice(book)
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        quality = spread_ticks * max(0.1, 1.0 - 2.5 * abs(skew))
        loaned = _has_loan(accounts, book_id)

        if abs(skew) >= inventory_skew_soft:
            trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
            strength = 12.0 + abs(skew) * 30.0
            flatten_jobs.append(
                (strength, book_id, best_bid, best_ask, mid, trade_dir)
            )
            continue

        if loaned:
            continue

        if (
            abs(skew) <= inventory_skew_soft * 0.45
            and spread_ticks >= 2.0
            and abs(ret) < reversion_threshold * 0.6
            and abs(rel) < relative_threshold * 1.2
        ):
            if mp and mid > 0:
                if abs((mp - mid) / mid) <= 0.00004:
                    two_sided.append((quality, book_id, best_bid, best_ask, mid))
            else:
                two_sided.append((quality, book_id, best_bid, best_ask, mid))

        trade_dir: OrderDirection | None = None
        strength = 0.0
        if rel >= relative_threshold and skew <= inventory_skew_soft * 0.5:
            trade_dir, strength = OrderDirection.SELL, abs(rel) / relative_threshold
        elif rel <= -relative_threshold and skew >= -inventory_skew_soft * 0.5:
            trade_dir, strength = OrderDirection.BUY, abs(rel) / relative_threshold

        if ret >= reversion_threshold and trade_dir != OrderDirection.BUY:
            if trade_dir is None:
                trade_dir, strength = OrderDirection.SELL, abs(ret) / reversion_threshold
            elif trade_dir == OrderDirection.SELL:
                strength += abs(ret) / reversion_threshold
        elif ret <= -reversion_threshold and trade_dir != OrderDirection.SELL:
            if trade_dir is None:
                trade_dir, strength = OrderDirection.BUY, abs(ret) / reversion_threshold
            elif trade_dir == OrderDirection.BUY:
                strength += abs(ret) / reversion_threshold

        if trade_dir is None or strength < 1.0:
            continue

        if mp and mid > 0:
            edge = (mp - mid) / mid
            if trade_dir == OrderDirection.BUY and edge > 0.000025:
                continue
            if trade_dir == OrderDirection.SELL and edge < -0.000025:
                continue

        prev = direction.get(book_id)
        if prev == OrderDirection.BUY and trade_dir == OrderDirection.BUY and skew > 0.018:
            continue
        if prev == OrderDirection.SELL and trade_dir == OrderDirection.SELL and skew < -0.018:
            continue

        edge_jobs.append(
            (strength * quality, book_id, best_bid, best_ask, mid, trade_dir)
        )

    def _qty(strength: float) -> float:
        qty = round(
            (min_quantity + min(0.55, strength * 0.06) * q_span) * quantity_scale,
            vdec,
        )
        return max(qty, min_quantity)

    placed_books: set[int] = set()

    def _place_limit(
        book_id: int,
        trade_dir: OrderDirection,
        qty: float,
        best_bid: float,
        best_ask: float,
        *,
        inside: bool,
    ) -> bool:
        if not _can_place(book_id, 1):
            return False
        account = accounts[book_id]
        if inside:
            if trade_dir == OrderDirection.BUY:
                price = round_price(min(best_bid + tick, best_ask - tick), pdec)
            else:
                price = round_price(max(best_ask - tick, best_bid + tick), pdec)
        elif trade_dir == OrderDirection.BUY:
            price = round_price(best_ask, pdec)
        else:
            price = round_price(best_bid, pdec)

        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                return False
        elif price <= best_bid or account.base_balance.free < qty:
            return False

        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        _mark(book_id, 1)
        direction[book_id] = (
            OrderDirection.SELL
            if trade_dir == OrderDirection.BUY
            else OrderDirection.BUY
        )
        return True

    flatten_jobs.sort(key=lambda x: -x[0])
    for strength, book_id, best_bid, best_ask, _mid, trade_dir in flatten_jobs:
        if len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books:
            continue
        aggressive = abs(inventory_skew(accounts, book_id, _mid)) >= inventory_skew_hard
        if _place_limit(
            book_id,
            trade_dir,
            _qty(strength),
            best_bid,
            best_ask,
            inside=not aggressive,
        ):
            placed_books.add(book_id)

    two_sided.sort(key=lambda x: -x[0])
    n_skip = int(len(two_sided) * inactive_book_frac)
    two_sided = two_sided[n_skip:]
    two_sided_count = 0
    for quality, book_id, best_bid, best_ask, _mid in two_sided:
        if two_sided_count >= two_sided_cap or len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books or _has_loan(accounts, book_id):
            continue
        if not _can_place(book_id, 2):
            continue
        qty = _qty(quality)
        account = accounts[book_id]
        buy_p = round_price(min(best_bid + tick, best_ask - tick), pdec)
        sell_p = round_price(max(best_ask - tick, best_bid + tick), pdec)
        if buy_p >= sell_p:
            continue
        if account.quote_balance.free < qty * buy_p or account.base_balance.free < qty:
            continue
        place_limit(response, book_id, OrderDirection.BUY, qty, buy_p, expiry_period)
        place_limit(response, book_id, OrderDirection.SELL, qty, sell_p, expiry_period)
        _mark(book_id, 2)
        placed_books.add(book_id)
        two_sided_count += 1

    edge_jobs.sort(key=lambda x: -x[0])
    n_skip = int(len(edge_jobs) * inactive_book_frac)
    edge_jobs = edge_jobs[n_skip:]
    edge_count = 0
    for strength, book_id, best_bid, best_ask, _mid, trade_dir in edge_jobs:
        if edge_count >= edge_slots or len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books or _has_loan(accounts, book_id):
            continue
        if _place_limit(
            book_id, trade_dir, _qty(strength), best_bid, best_ask, inside=True
        ):
            placed_books.add(book_id)
            edge_count += 1


# ---------------------------------------------------------------------------
# v2 — higher round-trip quality (cancel stale, fill-score ranking, requote)
# ---------------------------------------------------------------------------

_MTR_TARGET = 0.4


class _InstructionBudget:
    """Per-tick instruction cap tracker (places + cancels share the budget)."""

    def __init__(self, max_per_book: int, max_total: int) -> None:
        self.max_per_book = max_per_book
        self.max_total = max_total
        self.by_book: dict[int, int] = {}
        self.total = 0

    def can_place(self, book_id: int, n: int = 1) -> bool:
        if self.total + n > self.max_total:
            return False
        if self.by_book.get(book_id, 0) + n > self.max_per_book:
            return False
        return True

    def mark(self, book_id: int, n: int = 1) -> None:
        self.by_book[book_id] = self.by_book.get(book_id, 0) + n
        self.total += n


def book_mtr(book: Book) -> float | None:
    raw = getattr(book, "_raw", None)
    if isinstance(raw, dict) and "mtr" in raw:
        return float(raw["mtr"])
    mtr = getattr(book, "MTR", None)
    if mtr is not None:
        return float(mtr)
    return None


def tape_notional(book: Book) -> float:
    events = book.events
    if not events:
        return 0.0
    total = 0.0
    for event in events:
        et = getattr(event, "type", None) or getattr(event, "y", None)
        if et in ("t", "ET"):
            total += event.quantity * event.price
    return total


def tape_imbalance_ratio(book: Book) -> float:
    """Buy vs sell tape notional in [-1, 1]. Extreme values often mean toxic flow."""
    events = book.events
    if not events:
        return 0.0
    buy_n = sell_n = 0.0
    for event in events:
        et = getattr(event, "type", None) or getattr(event, "y", None)
        if et not in ("t", "ET"):
            continue
        n = event.quantity * event.price
        if event.side == OrderDirection.BUY:
            buy_n += n
        else:
            sell_n += n
    total = buy_n + sell_n
    if total <= 0:
        return 0.0
    return (buy_n - sell_n) / total


def touch_depth(book: Book) -> float:
    if not book.bids or not book.asks:
        return 0.0
    return min(book.bids[0].quantity, book.asks[0].quantity)


def maker_rebate_bonus(accounts, book_id: int) -> float:
    fees = accounts[book_id].fees
    if fees is None:
        return 0.0
    return max(0.0, -fees.maker_fee_rate) * 5000.0


def mtr_maker_bonus(book: Book) -> float:
    mtr = book_mtr(book)
    if mtr is None:
        return 0.0
    if mtr < _MTR_TARGET:
        return (_MTR_TARGET - mtr) * 12.0
    return 0.0


def book_fill_score(
    book: Book,
    accounts,
    book_id: int,
    *,
    spread_ticks: float,
    requote: bool = False,
) -> float:
    score = (
        spread_ticks * 2.2
        + tape_notional(book) * 0.00004
        + touch_depth(book) * 0.35
        + maker_rebate_bonus(accounts, book_id)
        + mtr_maker_bonus(book)
    )
    imb = abs(tape_imbalance_ratio(book))
    if imb > 0.55:
        score *= max(0.2, 1.0 - imb)
    if requote:
        score += 80.0
    return score


def _limit_price(
    trade_dir: OrderDirection,
    best_bid: float,
    best_ask: float,
    tick: float,
    pdec: int,
    *,
    mode: str,
    depth_ticks: int = 1,
) -> float:
    """
    Price modes for maker orders:
      inside     — depth_ticks inside the spread (default depth 1)
      deep_inside — alias for inside with depth_ticks >= 2
      join_touch — join bid (buy) or ask (sell) queue passively at touch
      cross      — emergency flatten only (buy@ask / sell@bid)
    """
    if mode in ("inside", "deep_inside"):
        depth = max(1, depth_ticks if mode == "inside" else max(2, depth_ticks))
        if trade_dir == OrderDirection.BUY:
            return round_price(
                min(best_bid + depth * tick, best_ask - tick), pdec
            )
        return round_price(
            max(best_ask - depth * tick, best_bid + tick), pdec
        )
    if mode == "join_touch":
        if trade_dir == OrderDirection.BUY:
            return round_price(best_bid, pdec)
        return round_price(best_ask, pdec)
    if mode == "cross":
        if trade_dir == OrderDirection.BUY:
            return round_price(best_ask, pdec)
        return round_price(best_bid, pdec)
    raise ValueError(f"unknown price mode: {mode}")


def _inside_spread_prices(
    best_bid: float, best_ask: float, tick: float, pdec: int
) -> tuple[float, float]:
    buy_p = round_price(min(best_bid + tick, best_ask - tick), pdec)
    sell_p = round_price(max(best_ask - tick, best_bid + tick), pdec)
    return buy_p, sell_p


def _rt_edge_ticks(
    best_bid: float, best_ask: float, tick: float, pdec: int
) -> float:
    """Capturable inside-spread edge in tick units (must cover fees on round trip)."""
    if tick <= 0:
        return 0.0
    buy_p, sell_p = _inside_spread_prices(best_bid, best_ask, tick, pdec)
    if sell_p <= buy_p:
        return 0.0
    return (sell_p - buy_p) / tick


def _completion_rt_edge_ticks(
    fill_price: float,
    comp_dir: OrderDirection,
    best_bid: float,
    best_ask: float,
    tick: float,
    pdec: int,
) -> float:
    """Expected round-trip edge in ticks after a maker fill and inside completion."""
    if tick <= 0:
        return 0.0
    comp_price = _limit_price(
        comp_dir, best_bid, best_ask, tick, pdec, mode="inside"
    )
    if comp_dir == OrderDirection.SELL:
        edge = comp_price - fill_price
    else:
        edge = fill_price - comp_price
    return edge / tick


def _iter_requote_hints(
    requote_hints: dict[int, OrderDirection | tuple[OrderDirection, float]],
) -> list[tuple[int, OrderDirection, float | None]]:
    rows: list[tuple[int, OrderDirection, float | None]] = []
    for book_id, hint in requote_hints.items():
        if isinstance(hint, tuple):
            rows.append((book_id, hint[0], float(hint[1])))
        else:
            rows.append((book_id, hint, None))
    return rows


def startup_cancel_all_orders(
    response,
    accounts,
    budget: _InstructionBudget,
) -> bool:
    """
    Cancel every resting limit order across all books.

    May take multiple ticks when order count exceeds the instruction budget.
    Returns True when no open orders remain in the account snapshot.
    """
    for book_id in sorted(accounts.keys()):
        account = accounts[book_id]
        if not account.orders:
            continue
        for order in sorted(account.orders, key=lambda o: o.id):
            if not budget.can_place(book_id, 1):
                return False
            response.cancel_order(book_id, order.id)
            budget.mark(book_id, 1)
    return not any(acct.orders for acct in accounts.values())


def cancel_stale_orders(
    response,
    account,
    book_id: int,
    best_bid: float,
    best_ask: float,
    tick: float,
    budget: _InstructionBudget,
    *,
    max_cancel: int = 2,
) -> int:
    if not account.orders:
        return 0
    stale: list[tuple[int, int]] = []
    for order in account.orders:
        if order.price is None:
            continue
        if order.side == OrderDirection.BUY and order.price < best_bid - tick * 0.5:
            stale.append((order.timestamp, order.id))
        elif order.side == OrderDirection.SELL and order.price > best_ask + tick * 0.5:
            stale.append((order.timestamp, order.id))
    stale.sort()
    canceled = 0
    for _ts, order_id in stale:
        if canceled >= max_cancel:
            break
        if not budget.can_place(book_id, 1):
            break
        response.cancel_order(book_id, order_id)
        budget.mark(book_id, 1)
        canceled += 1
    return canceled


def turbo_v2_kappa_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    requote_hints: dict[int, OrderDirection] | None = None,
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    reversion_threshold: float,
    relative_threshold: float,
    cadence_interval_ns: int,
    inventory_skew_soft: float,
    inventory_skew_hard: float,
    expiry_period: int,
    max_books_per_tick: int = 11,
    book_rotation_groups: int = 16,
    max_spread_ratio: float = 0.0018,
    inactive_book_frac: float = 0.30,
    max_two_sided_per_tick: int = 4,
    max_instructions_per_book: int = 4,
    max_total_instructions: int = 28,
    turbo_profile: str = "forge",
    cancel_stale: bool = True,
    max_cancel_per_book: int = 2,
    max_requote_per_tick: int = 4,
    touch_join_on_requote: bool = False,
    min_fill_score: float = 0.0,
    min_spread_ticks_rt: float = 4.0,
    min_rt_edge_ticks: float = 2.5,
    max_edge_per_tick: int = 0,
) -> None:
    """
    v2 scoring engine: stale cancel + fill-probability ranking + post-fill requote.

    Improvements over turbo_kappa_score_tick:
      - Cancel off-touch resting limits before re-quoting (budget-aware)
      - Rank books by spread, tape activity, touch depth, maker rebate, MTR
      - Priority completion legs after maker fills (requote_hints from onTrade)
      - Optional touch-join on completion for higher fill probability
    """
    requote_hints = requote_hints or {}
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    q_span = max_quantity - min_quantity
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    active_rots = {
        rot,
        (rot + 1) % book_rotation_groups,
        (rot + 2) % book_rotation_groups,
    }
    profile = (turbo_profile or "forge").lower()
    if profile == "pulse":
        two_sided_cap = max_two_sided_per_tick + 1
        edge_slots = max_edge_per_tick if max_edge_per_tick >= 0 else max(2, max_books_per_tick - two_sided_cap)
    elif profile == "edge":
        two_sided_cap = min(2, max_two_sided_per_tick)
        edge_slots = max_edge_per_tick if max_edge_per_tick >= 0 else max_books_per_tick
    elif profile == "apex":
        two_sided_cap = min(3, max_two_sided_per_tick)
        edge_slots = max_edge_per_tick if max_edge_per_tick >= 0 else max(2, max_books_per_tick - two_sided_cap)
    elif profile == "select":
        two_sided_cap = min(2, max_two_sided_per_tick)
        edge_slots = max_edge_per_tick if max_edge_per_tick > 0 else 0
    else:
        two_sided_cap = max_two_sided_per_tick
        edge_slots = max_edge_per_tick if max_edge_per_tick > 0 else 0

    budget = _InstructionBudget(max_instructions_per_book, max_total_instructions)

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)

    if len(mids_scratch) < 4:
        return
    med = sorted(mids_scratch.values())[len(mids_scratch) // 2]

    placed_books: set[int] = set()

    def _qty(strength: float) -> float:
        qty = round(
            (min_quantity + min(0.55, strength * 0.06) * q_span) * quantity_scale,
            vdec,
        )
        return max(qty, min_quantity)

    def _requote_price_mode(
        comp_dir: OrderDirection, skew: float, book_id: int
    ) -> str:
        """Inside-only completion unless neutral skew and explicit touch_join enabled."""
        if comp_dir == OrderDirection.SELL and skew > 0.004:
            return "inside"
        if comp_dir == OrderDirection.BUY and skew < -0.004:
            return "inside"
        if not touch_join_on_requote or abs(skew) > 0.003:
            return "inside"
        return "join_touch"

    def _place_limit(
        book_id: int,
        trade_dir: OrderDirection,
        qty: float,
        best_bid: float,
        best_ask: float,
        *,
        mode: str = "inside",
    ) -> bool:
        if not budget.can_place(book_id, 1):
            return False
        account = accounts[book_id]
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode=mode
        )

        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                return False
        elif price <= best_bid or account.base_balance.free < qty:
            return False

        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        direction[book_id] = (
            OrderDirection.SELL
            if trade_dir == OrderDirection.BUY
            else OrderDirection.BUY
        )
        return True

    def _prep_book(book_id: int, best_bid: float, best_ask: float) -> None:
        if cancel_stale:
            cancel_stale_orders(
                response,
                accounts[book_id],
                book_id,
                best_bid,
                best_ask,
                tick,
                budget,
                max_cancel=max_cancel_per_book,
            )

    # Phase 0 — complete round trips after recent maker fills
    requote_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, comp_dir, _fill_price in _iter_requote_hints(requote_hints):
        row = book_rows.get(book_id)
        if row is None or _has_loan(accounts, book_id):
            continue
        book, best_bid, best_ask, mid = row
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        rt_edge = _rt_edge_ticks(best_bid, best_ask, tick, pdec)
        if spread_ticks < min_spread_ticks_rt or rt_edge < min_rt_edge_ticks:
            continue
        fs = book_fill_score(
            book, accounts, book_id, spread_ticks=spread_ticks, requote=True
        )
        if fs < min_fill_score:
            continue
        requote_jobs.append((fs, book_id, best_bid, best_ask, mid, comp_dir))
    requote_jobs.sort(key=lambda x: -x[0])
    requote_count = 0
    for fs, book_id, best_bid, best_ask, mid, comp_dir in requote_jobs:
        if requote_count >= max_requote_per_tick or len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books:
            continue
        _prep_book(book_id, best_bid, best_ask)
        strength = 8.0 + fs * 0.05
        skew = inventory_skew(accounts, book_id, mid)
        if _place_limit(
            book_id,
            comp_dir,
            _qty(strength),
            best_bid,
            best_ask,
            mode=_requote_price_mode(comp_dir, skew, book_id),
        ):
            placed_books.add(book_id)
            requote_count += 1

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    two_sided: list[tuple[float, int, float, float, float]] = []
    edge_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    skewed_books = sum(
        1
        for bid, (_book, _bb, _ba, mid) in book_rows.items()
        if abs(inventory_skew(accounts, bid, mid)) >= inventory_skew_soft * 0.45
    )
    risk_off = skewed_books >= 4

    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed_books:
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue

        skew = inventory_skew(accounts, book_id, mid)
        prev_m = last_mid.get(book_id)
        last_mid[book_id] = mid
        ret = (mid - prev_m) / prev_m if prev_m and prev_m > 0 else 0.0
        rel = (mid - med) / med if med > 0 else 0.0
        mp = microprice(book)
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        fill_sc = book_fill_score(book, accounts, book_id, spread_ticks=spread_ticks)
        if fill_sc < min_fill_score:
            continue
        quality = fill_sc * max(0.1, 1.0 - 2.5 * abs(skew))
        loaned = _has_loan(accounts, book_id)

        if abs(skew) >= inventory_skew_soft * 0.65:
            trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
            strength = 12.0 + abs(skew) * 30.0 + fill_sc * 0.02
            flatten_jobs.append(
                (strength, book_id, best_bid, best_ask, mid, trade_dir)
            )
            continue

        if loaned:
            continue

        if risk_off or abs(skew) >= inventory_skew_soft * 0.35:
            continue

        rt_edge = _rt_edge_ticks(best_bid, best_ask, tick, pdec)
        if (
            spread_ticks >= min_spread_ticks_rt
            and rt_edge >= min_rt_edge_ticks
            and abs(ret) < reversion_threshold * 0.6
            and abs(rel) < relative_threshold * 1.2
            and abs(tape_imbalance_ratio(book)) < 0.45
        ):
            if mp and mid > 0:
                if abs((mp - mid) / mid) <= 0.00004:
                    two_sided.append((quality, book_id, best_bid, best_ask, mid))
            else:
                two_sided.append((quality, book_id, best_bid, best_ask, mid))

        if max_edge_per_tick > 0 and rt_edge >= min_rt_edge_ticks:
            trade_dir: OrderDirection | None = None
            strength = 0.0
            if rel >= relative_threshold and skew <= inventory_skew_soft * 0.35:
                trade_dir, strength = OrderDirection.SELL, abs(rel) / relative_threshold
            elif rel <= -relative_threshold and skew >= -inventory_skew_soft * 0.35:
                trade_dir, strength = OrderDirection.BUY, abs(rel) / relative_threshold

            if ret >= reversion_threshold and trade_dir != OrderDirection.BUY:
                if trade_dir is None:
                    trade_dir, strength = OrderDirection.SELL, abs(ret) / reversion_threshold
                elif trade_dir == OrderDirection.SELL:
                    strength += abs(ret) / reversion_threshold
            elif ret <= -reversion_threshold and trade_dir != OrderDirection.SELL:
                if trade_dir is None:
                    trade_dir, strength = OrderDirection.BUY, abs(ret) / reversion_threshold
                elif trade_dir == OrderDirection.BUY:
                    strength += abs(ret) / reversion_threshold

            if trade_dir is not None and strength >= 1.0:
                if mp and mid > 0:
                    edge = (mp - mid) / mid
                    if trade_dir == OrderDirection.BUY and edge > 0.000025:
                        trade_dir = None
                    if trade_dir == OrderDirection.SELL and edge < -0.000025:
                        trade_dir = None

            if trade_dir is not None and strength >= 1.0:
                prev = direction.get(book_id)
                if prev == OrderDirection.BUY and trade_dir == OrderDirection.BUY and skew > 0.018:
                    trade_dir = None
                if prev == OrderDirection.SELL and trade_dir == OrderDirection.SELL and skew < -0.018:
                    trade_dir = None

            if trade_dir is not None and strength >= 1.0:
                edge_jobs.append(
                    (strength * quality, book_id, best_bid, best_ask, mid, trade_dir)
                )

    flatten_jobs.sort(key=lambda x: -x[0])
    for strength, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books:
            continue
        _prep_book(book_id, best_bid, best_ask)
        aggressive = abs(inventory_skew(accounts, book_id, mid)) >= inventory_skew_hard
        if _place_limit(
            book_id,
            trade_dir,
            _qty(strength),
            best_bid,
            best_ask,
            mode="cross" if aggressive else "inside",
        ):
            placed_books.add(book_id)

    two_sided.sort(key=lambda x: -x[0])
    n_skip = int(len(two_sided) * inactive_book_frac)
    two_sided = two_sided[n_skip:]
    two_sided_count = 0
    for quality, book_id, best_bid, best_ask, _mid in two_sided:
        if two_sided_count >= two_sided_cap or len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books or _has_loan(accounts, book_id):
            continue
        if not budget.can_place(book_id, 2):
            continue
        _prep_book(book_id, best_bid, best_ask)
        qty = _qty(quality)
        account = accounts[book_id]
        buy_p, sell_p = _inside_spread_prices(best_bid, best_ask, tick, pdec)
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if account.quote_balance.free < qty * buy_p or account.base_balance.free < qty:
            continue
        place_limit(response, book_id, OrderDirection.BUY, qty, buy_p, expiry_period)
        place_limit(response, book_id, OrderDirection.SELL, qty, sell_p, expiry_period)
        budget.mark(book_id, 2)
        placed_books.add(book_id)
        two_sided_count += 1

    edge_jobs.sort(key=lambda x: -x[0])
    n_skip = int(len(edge_jobs) * inactive_book_frac)
    edge_jobs = edge_jobs[n_skip:]
    edge_count = 0
    for strength, book_id, best_bid, best_ask, _mid, trade_dir in edge_jobs:
        if edge_count >= edge_slots or len(placed_books) >= max_books_per_tick:
            break
        if book_id in placed_books or _has_loan(accounts, book_id):
            continue
        _prep_book(book_id, best_bid, best_ask)
        if _place_limit(
            book_id, trade_dir, _qty(strength), best_bid, best_ask, mode="inside"
        ):
            placed_books.add(book_id)
            edge_count += 1


# ---------------------------------------------------------------------------
# survive — stop realized-PnL bleed: cancel, flatten, minimal wide-spread maker
# ---------------------------------------------------------------------------


def turbo_survive_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    expiry_period: int,
    inventory_skew_soft: float = 0.005,
    inventory_skew_hard: float = 0.012,
    max_books_per_tick: int = 3,
    max_instructions_per_book: int = 2,
    max_total_instructions: int = 5,
    book_rotation_groups: int = 20,
    cadence_interval_ns: int = 30_000_000_000,
    max_spread_ratio: float = 0.0010,
    min_spread_ticks: float = 5.0,
    min_rt_edge_ticks: float = 3.0,
) -> None:
    """
    Emergency capital-preservation mode.

    Stops the failure mode seen on UIDs 10/65/158: high round-trip volume with
    deeply negative realized PnL and kappa still None.

    Rules:
      - NO requote, NO two-sided, NO edge/reversion
      - Cancel stale resting orders first
      - Flatten any book with inventory skew (inside; cross only if hard breach)
      - At most max_books_per_tick new single-sided INSIDE quotes
      - Only books with wide spread (>= min_spread_ticks) and capturable edge
    """
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    budget = _InstructionBudget(max_instructions_per_book, max_total_instructions)

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if abs(tape_imbalance_ratio(book)) > 0.40:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)
        last_mid[book_id] = mid

    if not book_rows:
        return

    qty = round(max(min_quantity, max_quantity * quantity_scale), vdec)
    qty = max(qty, min_quantity)
    placed: set[int] = set()

    # Phase 1 — cancel stale on any book with open orders (free slots for flatten)
    for book_id, (_book, best_bid, best_ask, _mid) in sorted(book_rows.items()):
        if budget.total >= max_total_instructions:
            break
        cancel_stale_orders(
            response,
            accounts[book_id],
            book_id,
            best_bid,
            best_ask,
            tick,
            budget,
            max_cancel=max_instructions_per_book,
        )

    # Phase 2 — flatten skewed inventory (priority)
    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if _has_loan(accounts, book_id):
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 100.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    for _prio, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if budget.total >= max_total_instructions or len(placed) >= max_books_per_tick:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        mode = "cross" if abs(skew) >= inventory_skew_hard else "inside"
        price = _limit_price(trade_dir, best_bid, best_ask, tick, pdec, mode=mode)
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    # Phase 3 — passive inside quotes on widest spreads only (single-sided)
    quote_candidates: list[tuple[float, int, float, float, float]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) != rot:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        quote_candidates.append((spread_ticks, book_id, best_bid, best_ask, mid))
    quote_candidates.sort(key=lambda x: -x[0])

    quote_count = 0
    for spread_ticks, book_id, best_bid, best_ask, mid in quote_candidates:
        if quote_count >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if skew > 0.002:
            trade_dir = OrderDirection.SELL
        elif skew < -0.002:
            trade_dir = OrderDirection.BUY
        else:
            trade_dir = (
                OrderDirection.BUY if (book_id + rot) % 2 == 0 else OrderDirection.SELL
            )
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode="inside"
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        quote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )


def steady_maker_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    requote_hints: dict[int, tuple[OrderDirection, float]] | None = None,
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    expiry_period: int,
    inventory_skew_soft: float = 0.008,
    inventory_skew_hard: float = 0.018,
    max_books_per_tick: int = 2,
    max_instructions_per_book: int = 2,
    max_total_instructions: int = 6,
    max_requote_per_tick: int = 2,
    book_rotation_groups: int = 20,
    cadence_interval_ns: int = 30_000_000_000,
    max_spread_ratio: float = 0.0010,
    min_spread_ticks: float = 7.0,
    min_rt_edge_ticks: float = 6.0,
    min_completion_rt_edge_ticks: float | None = None,
    min_microprice_edge_ticks: float = 1.5,
    min_quote_spread_ticks: float | None = None,
    min_quote_rt_edge_ticks: float | None = None,
    max_tape_imbalance: float = 0.28,
    cold_book_volume_threshold: float = 500.0,
    rotation_windows: int = 1,
    inside_depth_ticks: int = 1,
    use_fill_score_ranking: bool = False,
) -> None:
    """
    Kappa-focused maker: wide books, microprice-filtered quotes, completion legs.

    Targets positive median Kappa-3 and realized PnL by completing round trips
    after maker fills (inside only, edge vs fill price) and skipping weak quotes.
    """
    requote_hints = requote_hints or {}
    completion_edge = (
        min_rt_edge_ticks
        if min_completion_rt_edge_ticks is None
        else min_completion_rt_edge_ticks
    )
    quote_spread_min = (
        min_spread_ticks
        if min_quote_spread_ticks is None
        else min_quote_spread_ticks
    )
    quote_rt_edge = (
        min_rt_edge_ticks
        if min_quote_rt_edge_ticks is None
        else min_quote_rt_edge_ticks
    )
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    active_rots = {
        (rot + i) % max(book_rotation_groups, 1)
        for i in range(max(1, rotation_windows))
    }
    budget = _InstructionBudget(max_instructions_per_book, max_total_instructions)
    quote_depth = max(1, inside_depth_ticks)

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if abs(tape_imbalance_ratio(book)) > max_tape_imbalance:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)
        last_mid[book_id] = mid

    if not book_rows:
        return

    qty = sim_order_qty(min_quantity, max_quantity, quantity_scale, vdec)
    placed: set[int] = set()

    for book_id, (_book, best_bid, best_ask, _mid) in sorted(book_rows.items()):
        if budget.total >= max_total_instructions:
            break
        cancel_stale_orders(
            response,
            accounts[book_id],
            book_id,
            best_bid,
            best_ask,
            tick,
            budget,
            max_cancel=max_instructions_per_book,
        )

    # Phase 0 — complete round trips after maker fills (inside only, edge vs fill)
    requote_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (comp_dir, fill_price) in requote_hints.items():
        row = book_rows.get(book_id)
        if row is None or _has_loan(accounts, book_id):
            continue
        _book, best_bid, best_ask, mid = row
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        comp_edge = _completion_rt_edge_ticks(
            fill_price, comp_dir, best_bid, best_ask, tick, pdec
        )
        if comp_edge < completion_edge:
            continue
        requote_jobs.append((comp_edge + 50.0, book_id, best_bid, best_ask, mid, comp_dir))
    requote_jobs.sort(key=lambda x: -x[0])
    requote_count = 0
    for _prio, book_id, best_bid, best_ask, _mid, trade_dir in requote_jobs:
        if requote_count >= max_requote_per_tick or len(placed) >= max_books_per_tick:
            break
        if book_id in placed:
            continue
        price = _limit_price(
            trade_dir,
            best_bid,
            best_ask,
            tick,
            pdec,
            mode="inside",
            depth_ticks=quote_depth,
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(
            response,
            book_id,
            trade_dir,
            qty,
            price,
            expiry_period,
            volume_decimals=vdec,
            min_quantity=min_quantity,
        )
        budget.mark(book_id, 1)
        placed.add(book_id)
        requote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if _has_loan(accounts, book_id):
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 100.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    for _prio, book_id, best_bid, best_ask, _mid, trade_dir in flatten_jobs:
        if budget.total >= max_total_instructions or len(placed) >= max_books_per_tick:
            break
        if book_id in placed:
            continue
        price = _limit_price(
            trade_dir,
            best_bid,
            best_ask,
            tick,
            pdec,
            mode="inside",
            depth_ticks=quote_depth,
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(
            response,
            book_id,
            trade_dir,
            qty,
            price,
            expiry_period,
            volume_decimals=vdec,
            min_quantity=min_quantity,
        )
        budget.mark(book_id, 1)
        placed.add(book_id)
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    quote_candidates: list[tuple[float, int, Book, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < quote_spread_min:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < quote_rt_edge:
            continue
        traded = float(getattr(accounts[book_id], "traded_volume", 0.0) or 0.0)
        cold_bonus = (
            2.0
            if traded < cold_book_volume_threshold * 0.1
            else (1.0 if traded < cold_book_volume_threshold else 0.0)
        )
        if use_fill_score_ranking:
            rank = book_fill_score(
                book, accounts, book_id, spread_ticks=spread_ticks
            ) + cold_bonus
        else:
            rank = spread_ticks + cold_bonus
        quote_candidates.append((rank, book_id, book, best_bid, best_ask, mid))
    quote_candidates.sort(key=lambda x: -x[0])

    quote_count = 0
    for _rank, book_id, book, best_bid, best_ask, mid in quote_candidates:
        if quote_count >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        mp = microprice(book)
        if mp is None or tick <= 0:
            continue
        edge_ticks = (mp - mid) / tick
        if skew > 0.004:
            trade_dir = OrderDirection.SELL
        elif skew < -0.004:
            trade_dir = OrderDirection.BUY
        elif edge_ticks >= min_microprice_edge_ticks:
            trade_dir = OrderDirection.BUY
        elif edge_ticks <= -min_microprice_edge_ticks:
            trade_dir = OrderDirection.SELL
        else:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        depth = quote_depth if spread_ticks >= quote_spread_min + 1 else 1
        price = _limit_price(
            trade_dir,
            best_bid,
            best_ask,
            tick,
            pdec,
            mode="inside",
            depth_ticks=depth,
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(
            response,
            book_id,
            trade_dir,
            qty,
            price,
            expiry_period,
            volume_decimals=vdec,
            min_quantity=min_quantity,
        )
        budget.mark(book_id, 1)
        placed.add(book_id)
        quote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )


def ascend_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    requote_hints: dict[int, tuple[OrderDirection, float]] | None = None,
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    expiry_period: int,
    inventory_skew_soft: float = 0.003,
    inventory_skew_hard: float = 0.007,
    max_books_per_tick: int = 4,
    max_instructions_per_book: int = 3,
    max_total_instructions: int = 12,
    max_requote_per_tick: int = 4,
    book_rotation_groups: int = 32,
    cadence_interval_ns: int = 28_000_000_000,
    rotation_windows: int = 3,
    max_spread_ratio: float = 0.00085,
    min_spread_ticks: float = 7.0,
    min_rt_edge_ticks: float = 6.5,
    min_completion_rt_edge_ticks: float | None = None,
    min_microprice_edge_ticks: float = 2.0,
    min_quote_spread_ticks: float | None = None,
    min_quote_rt_edge_ticks: float | None = None,
    max_tape_imbalance: float = 0.20,
    cold_book_volume_threshold: float = 500.0,
    inside_depth_ticks: int = 1,
    deep_spread_ticks: float = 11.0,
    inactive_book_frac: float = 0.25,
    risk_off_skewed_books: int = 2,
    two_sided_wide_ticks: float = 0.0,
    max_flatten_per_tick: int = 12,
    touch_join_spread_ticks: float = 4.5,
    max_touch_per_tick: int = 8,
    max_cold_books_per_tick: int = 4,
) -> None:
    """
    Ascend scoring engine — fast κ growth, Penalty → 0, positive realized PnL.

    Synthesizes top-miner patterns (UID 202/26/251: κ→1+, penalty=0, lean inventory)
    with lessons from failed Turbo/SteadyMaker/Vault deploys (10/65/158/209).

    Rules:
      - Completion-first inside-spread legs after maker fills (edge vs fill price)
      - Touch-join on wide spreads for fill rate (top miners satisfy more orders)
      - Two-sided only on very wide spreads with near-zero skew
      - Dedicated flatten budget (max_flatten_per_tick) so inventory does not accumulate
      - Microprice direction gate with rotation fallback; cold-book bonus for coverage
    """
    requote_hints = requote_hints or {}
    completion_edge = (
        min_rt_edge_ticks
        if min_completion_rt_edge_ticks is None
        else min_completion_rt_edge_ticks
    )
    quote_spread_min = (
        min_spread_ticks
        if min_quote_spread_ticks is None
        else min_quote_spread_ticks
    )
    quote_rt_edge = (
        min_rt_edge_ticks
        if min_quote_rt_edge_ticks is None
        else min_quote_rt_edge_ticks
    )
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    active_rots = {
        (rot + i) % max(book_rotation_groups, 1)
        for i in range(max(1, rotation_windows))
    }
    budget = _InstructionBudget(max_instructions_per_book, max_total_instructions)
    base_depth = max(1, inside_depth_ticks)

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if abs(tape_imbalance_ratio(book)) > max_tape_imbalance:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)
        last_mid[book_id] = mid

    if not book_rows:
        for book_id, book in state.books.items():
            t = _touch(book)
            if t is None:
                continue
            best_bid, best_ask, mid = t
            if mid <= 0:
                continue
            spread_r = (best_ask - best_bid) / mid
            if spread_r > max_spread_ratio * 1.5:
                continue
            spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
            if spread_ticks < max(1.0, min_spread_ticks - 1.0):
                continue
            if not maker_fee_ok(accounts, book_id, max_fee_rate):
                continue
            mids_scratch[book_id] = mid
            book_rows[book_id] = (book, best_bid, best_ask, mid)
            last_mid[book_id] = mid

    if not book_rows:
        return

    qty = sim_order_qty(min_quantity, max_quantity, quantity_scale, vdec)
    placed: set[int] = set()

    skewed_count = sum(
        1
        for bid, (_b, _bb, _ba, mid) in book_rows.items()
        if abs(inventory_skew(accounts, bid, mid)) >= inventory_skew_soft
    )
    risk_off = skewed_count >= risk_off_skewed_books

    for book_id, (_book, best_bid, best_ask, _mid) in sorted(book_rows.items()):
        if budget.total >= max_total_instructions:
            break
        cancel_stale_orders(
            response,
            accounts[book_id],
            book_id,
            best_bid,
            best_ask,
            tick,
            budget,
            max_cancel=max_instructions_per_book,
        )

    requote_jobs: list[tuple[float, int, float, float, float, OrderDirection, bool]] = []
    for book_id, (comp_dir, fill_price) in requote_hints.items():
        row = book_rows.get(book_id)
        if row is None or _has_loan(accounts, book_id):
            continue
        _book, best_bid, best_ask, mid = row
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        comp_edge = _completion_rt_edge_ticks(
            fill_price, comp_dir, best_bid, best_ask, tick, pdec
        )
        if comp_edge < min_rt_edge_ticks:
            continue
        touch_comp = comp_edge < completion_edge
        fs = book_fill_score(
            _book, accounts, book_id, spread_ticks=spread_ticks, requote=True
        )
        prio = comp_edge + fs * 0.15 + (120.0 if touch_comp else 150.0)
        requote_jobs.append(
            (prio, book_id, best_bid, best_ask, mid, comp_dir, touch_comp)
        )
    requote_jobs.sort(key=lambda x: -x[0])
    requote_count = 0
    for _prio, book_id, best_bid, best_ask, _mid, trade_dir, touch_comp in requote_jobs:
        if requote_count >= max_requote_per_tick or len(placed) >= max_books_per_tick + 1:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, _mid)
        if trade_dir == OrderDirection.BUY and skew <= -inventory_skew_soft:
            continue
        if trade_dir == OrderDirection.SELL and skew >= inventory_skew_soft:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        depth = base_depth + 1 if spread_ticks >= deep_spread_ticks else base_depth
        comp_mode = "join_touch" if touch_comp else "inside"
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec,
            mode=comp_mode, depth_ticks=depth if comp_mode == "inside" else 0,
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(
            response, book_id, trade_dir, qty, price, expiry_period,
            volume_decimals=vdec, min_quantity=min_quantity,
        )
        budget.mark(book_id, 1)
        placed.add(book_id)
        requote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if _has_loan(accounts, book_id):
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 200.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    flatten_count = 0
    for _prio, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if budget.total >= max_total_instructions or flatten_count >= max_flatten_per_tick:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        mode = "cross" if abs(skew) >= inventory_skew_hard else "inside"
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode=mode, depth_ticks=base_depth
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(
            response, book_id, trade_dir, qty, price, expiry_period,
            volume_decimals=vdec, min_quantity=min_quantity,
        )
        budget.mark(book_id, 1)
        placed.add(book_id)
        flatten_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    touch_jobs: list[tuple[float, int, Book, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < touch_join_spread_ticks:
            continue
        rt_edge = _rt_edge_ticks(best_bid, best_ask, tick, pdec)
        if rt_edge < quote_rt_edge or rt_edge < completion_edge:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        touch_skew_cap = inventory_skew_soft * (0.5 if not risk_off else 0.35)
        if abs(skew) >= touch_skew_cap:
            continue
        traded = float(getattr(accounts[book_id], "traded_volume", 0.0) or 0.0)
        if traded < cold_book_volume_threshold * 0.1:
            cold_bonus = 10.0
        elif traded < cold_book_volume_threshold:
            cold_bonus = 5.0
        else:
            cold_bonus = 0.0
        fill_sc = book_fill_score(
            book, accounts, book_id, spread_ticks=spread_ticks, requote=False
        )
        touch_jobs.append(
            (fill_sc + spread_ticks * 0.6 + cold_bonus, book_id, book, best_bid, best_ask, mid)
        )
    touch_jobs.sort(key=lambda x: -x[0])

    touch_count = 0
    if not risk_off:
        for _rank, book_id, book, best_bid, best_ask, mid in touch_jobs:
            if touch_count >= max_touch_per_tick or budget.total >= max_total_instructions:
                break
            if book_id in placed:
                continue
            skew = inventory_skew(accounts, book_id, mid)
            mp = microprice(book)
            if skew > 0.0015:
                trade_dir = OrderDirection.SELL
            elif skew < -0.0015:
                trade_dir = OrderDirection.BUY
            elif mp is not None and tick > 0:
                edge_ticks = (mp - mid) / tick
                if edge_ticks >= min_microprice_edge_ticks:
                    trade_dir = OrderDirection.BUY
                elif edge_ticks <= -min_microprice_edge_ticks:
                    trade_dir = OrderDirection.SELL
                else:
                    trade_dir = (
                        OrderDirection.BUY
                        if (book_id + rot) % 2 == 0
                        else OrderDirection.SELL
                    )
            else:
                trade_dir = (
                    OrderDirection.BUY
                    if (book_id + rot) % 2 == 0
                    else OrderDirection.SELL
                )
            price = _limit_price(
                trade_dir, best_bid, best_ask, tick, pdec, mode="join_touch"
            )
            account = accounts[book_id]
            if trade_dir == OrderDirection.BUY:
                if price >= best_ask or account.quote_balance.free < qty * price:
                    continue
            elif price <= best_bid or account.base_balance.free < qty:
                continue
            if not budget.can_place(book_id, 1):
                continue
            place_limit(
                response, book_id, trade_dir, qty, price, expiry_period,
                volume_decimals=vdec, min_quantity=min_quantity,
            )
            budget.mark(book_id, 1)
            placed.add(book_id)
            touch_count += 1
            direction[book_id] = (
                OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
            )

    if risk_off:
        for book_id, (_book, best_bid, best_ask, _mid) in sorted(book_rows.items()):
            if budget.total >= max_total_instructions:
                break
            if abs(inventory_skew(accounts, book_id, _mid)) < inventory_skew_soft:
                continue
            cancel_stale_orders(
                response,
                accounts[book_id],
                book_id,
                best_bid,
                best_ask,
                tick,
                budget,
                max_cancel=max_instructions_per_book,
            )

    quote_spread_floor = (
        min_spread_ticks if risk_off else quote_spread_min
    )
    quote_rt_floor = (
        min_rt_edge_ticks if risk_off else quote_rt_edge
    )

    quote_candidates: list[tuple[float, int, Book, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < quote_spread_floor:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < quote_rt_floor:
            continue
        traded = float(getattr(accounts[book_id], "traded_volume", 0.0) or 0.0)
        if traded < cold_book_volume_threshold * 0.1:
            cold_bonus = 6.0
        elif traded < cold_book_volume_threshold:
            cold_bonus = 3.0
        else:
            cold_bonus = 0.0
        fill_sc = book_fill_score(
            book, accounts, book_id, spread_ticks=spread_ticks, requote=False
        )
        rank = fill_sc + spread_ticks * 0.5 + cold_bonus
        quote_candidates.append((rank, book_id, book, best_bid, best_ask, mid))
    quote_candidates.sort(key=lambda x: -x[0])

    if inactive_book_frac > 0 and len(quote_candidates) > 4:
        n_skip = int(len(quote_candidates) * inactive_book_frac)
        quote_candidates = quote_candidates[: max(0, len(quote_candidates) - n_skip)]

    quote_count = 0
    for _rank, book_id, book, best_bid, best_ask, mid in quote_candidates:
        if quote_count >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        depth = base_depth + 1 if spread_ticks >= deep_spread_ticks else base_depth
        account = accounts[book_id]

        if (
            two_sided_wide_ticks > 0
            and spread_ticks >= two_sided_wide_ticks
            and abs(skew) < 0.0008
            and budget.can_place(book_id, 2)
        ):
            buy_price = _limit_price(
                OrderDirection.BUY, best_bid, best_ask, tick, pdec,
                mode="inside", depth_ticks=depth,
            )
            sell_price = _limit_price(
                OrderDirection.SELL, best_bid, best_ask, tick, pdec,
                mode="inside", depth_ticks=depth,
            )
            if (
                buy_price < best_ask
                and sell_price > best_bid
                and buy_price < sell_price
                and account.quote_balance.free >= qty * buy_price
                and account.base_balance.free >= qty
            ):
                place_limit(
                    response, book_id, OrderDirection.BUY, qty, buy_price,
                    expiry_period, volume_decimals=vdec, min_quantity=min_quantity,
                )
                place_limit(
                    response, book_id, OrderDirection.SELL, qty, sell_price,
                    expiry_period, volume_decimals=vdec, min_quantity=min_quantity,
                )
                budget.mark(book_id, 2)
                placed.add(book_id)
                quote_count += 1
                continue

        mp = microprice(book)
        if skew > 0.002:
            trade_dir = OrderDirection.SELL
        elif skew < -0.002:
            trade_dir = OrderDirection.BUY
        elif mp is not None and tick > 0:
            edge_ticks = (mp - mid) / tick
            if edge_ticks >= min_microprice_edge_ticks:
                trade_dir = OrderDirection.BUY
            elif edge_ticks <= -min_microprice_edge_ticks:
                trade_dir = OrderDirection.SELL
            elif abs(skew) < inventory_skew_soft * 0.5:
                trade_dir = (
                    OrderDirection.BUY
                    if (book_id + rot) % 2 == 0
                    else OrderDirection.SELL
                )
            else:
                continue
        elif abs(skew) < inventory_skew_soft * 0.5:
            trade_dir = (
                OrderDirection.BUY
                if (book_id + rot) % 2 == 0
                else OrderDirection.SELL
            )
        else:
            continue
        rt_edge = _rt_edge_ticks(best_bid, best_ask, tick, pdec)
        quote_mode = (
            "join_touch"
            if (
                spread_ticks >= touch_join_spread_ticks
                and rt_edge >= completion_edge
                and abs(skew) < inventory_skew_soft * 0.4
                and not risk_off
            )
            else "inside"
        )
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec,
            mode=quote_mode, depth_ticks=depth if quote_mode == "inside" else 0,
        )
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(
            response, book_id, trade_dir, qty, price, expiry_period,
            volume_decimals=vdec, min_quantity=min_quantity,
        )
        budget.mark(book_id, 1)
        placed.add(book_id)
        quote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    cold_count = 0
    if max_cold_books_per_tick > 0 and budget.total < max_total_instructions:
        cold_jobs: list[tuple[float, int, Book, float, float, float]] = []
        for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
            if book_id in placed or _has_loan(accounts, book_id):
                continue
            traded = float(getattr(accounts[book_id], "traded_volume", 0.0) or 0.0)
            if traded >= cold_book_volume_threshold:
                continue
            spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
            if spread_ticks < min_spread_ticks:
                continue
            if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
                continue
            skew = inventory_skew(accounts, book_id, mid)
            if abs(skew) >= inventory_skew_soft:
                continue
            cold_jobs.append((traded, book_id, book, best_bid, best_ask, mid))
        cold_jobs.sort(key=lambda x: x[0])
        for _traded, book_id, book, best_bid, best_ask, mid in cold_jobs:
            if cold_count >= max_cold_books_per_tick or budget.total >= max_total_instructions:
                break
            if book_id in placed:
                continue
            spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
            depth = base_depth + 1 if spread_ticks >= deep_spread_ticks else base_depth
            account = accounts[book_id]
            mp = microprice(book)
            if mp is not None and tick > 0:
                edge_ticks = (mp - mid) / tick
                if edge_ticks >= min_microprice_edge_ticks:
                    trade_dir = OrderDirection.BUY
                elif edge_ticks <= -min_microprice_edge_ticks:
                    trade_dir = OrderDirection.SELL
                else:
                    trade_dir = (
                        OrderDirection.BUY
                        if (book_id + rot) % 2 == 0
                        else OrderDirection.SELL
                    )
            else:
                trade_dir = (
                    OrderDirection.BUY
                    if (book_id + rot) % 2 == 0
                    else OrderDirection.SELL
                )
            price = _limit_price(
                trade_dir, best_bid, best_ask, tick, pdec,
                mode="inside", depth_ticks=depth,
            )
            if trade_dir == OrderDirection.BUY:
                if price >= best_ask or account.quote_balance.free < qty * price:
                    continue
            elif price <= best_bid or account.base_balance.free < qty:
                continue
            if not budget.can_place(book_id, 1):
                continue
            place_limit(
                response, book_id, trade_dir, qty, price, expiry_period,
                volume_decimals=vdec, min_quantity=min_quantity,
            )
            budget.mark(book_id, 1)
            placed.add(book_id)
            cold_count += 1
            direction[book_id] = (
                OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
            )

    if not response.instructions:
        bootstrap_jobs: list[tuple[float, int, Book, float, float, float]] = []
        for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
            if _has_loan(accounts, book_id):
                continue
            spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
            if spread_ticks < min_spread_ticks:
                continue
            if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
                continue
            bootstrap_jobs.append((spread_ticks, book_id, book, best_bid, best_ask, mid))
        bootstrap_jobs.sort(key=lambda x: -x[0])
        bootstrap_placed = 0
        for spread_ticks, book_id, book, best_bid, best_ask, mid in bootstrap_jobs:
            if bootstrap_placed >= 3:
                break
            skew = inventory_skew(accounts, book_id, mid)
            if skew > inventory_skew_soft * 0.35:
                trade_dir = OrderDirection.SELL
            elif skew < -inventory_skew_soft * 0.35:
                trade_dir = OrderDirection.BUY
            else:
                mp = microprice(book)
                if mp is not None and tick > 0:
                    edge_ticks = (mp - mid) / tick
                    if edge_ticks >= min_microprice_edge_ticks:
                        trade_dir = OrderDirection.BUY
                    elif edge_ticks <= -min_microprice_edge_ticks:
                        trade_dir = OrderDirection.SELL
                    else:
                        trade_dir = (
                            OrderDirection.BUY
                            if (book_id + rot) % 2 == 0
                            else OrderDirection.SELL
                        )
                else:
                    trade_dir = (
                        OrderDirection.BUY
                        if (book_id + rot) % 2 == 0
                        else OrderDirection.SELL
                    )
            rt_edge = _rt_edge_ticks(best_bid, best_ask, tick, pdec)
            if (
                spread_ticks >= touch_join_spread_ticks
                and rt_edge >= completion_edge
                and abs(skew) < inventory_skew_soft * 0.5
            ):
                mode = "join_touch"
            elif spread_ticks >= quote_spread_floor:
                mode = "inside"
            else:
                continue
            price = _limit_price(
                trade_dir, best_bid, best_ask, tick, pdec, mode=mode, depth_ticks=base_depth
            )
            account = accounts[book_id]
            if trade_dir == OrderDirection.BUY:
                if price >= best_ask or account.quote_balance.free < qty * price:
                    continue
            elif price <= best_bid or account.base_balance.free < qty:
                continue
            if not budget.can_place(book_id, 1):
                continue
            place_limit(
                response, book_id, trade_dir, qty, price, expiry_period,
                volume_decimals=vdec, min_quantity=min_quantity,
            )
            budget.mark(book_id, 1)
            direction[book_id] = (
                OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
            )
            bootstrap_placed += 1


def turbo_power_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    requote_hints: dict[int, OrderDirection] | None = None,
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    expiry_period: int,
    inventory_skew_soft: float = 0.015,
    inventory_skew_hard: float = 0.030,
    max_books_per_tick: int = 10,
    max_instructions_per_book: int = 4,
    max_total_instructions: int = 26,
    max_requote_per_tick: int = 6,
    book_rotation_groups: int = 12,
    cadence_interval_ns: int = 20_000_000_000,
    max_spread_ratio: float = 0.0014,
    min_spread_ticks: float = 4.0,
    min_rt_edge_ticks: float = 4.0,
    min_fill_score: float = 2.5,
) -> None:
    """
    Scoring-focused mode: recover discipline + v2 completion legs (inside only).

    More books/instructions than recover for Kappa observations; avoids v2 bugs
    (no two-sided, no edge, no touch-join). Completion legs require positive
    expected round-trip edge vs the maker fill price.
    """
    requote_hints = requote_hints or {}
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    active_rots = {
        rot,
        (rot + 1) % book_rotation_groups,
        (rot + 2) % book_rotation_groups,
        (rot + 3) % book_rotation_groups,
    }
    budget = _InstructionBudget(max_instructions_per_book, max_total_instructions)

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if abs(tape_imbalance_ratio(book)) > 0.45:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)
        last_mid[book_id] = mid

    if not book_rows:
        return

    qty = round(max(min_quantity, max_quantity * quantity_scale), vdec)
    qty = max(qty, min_quantity)
    placed: set[int] = set()

    # Phase 0 — complete round trips after maker fills (inside only, never touch-join)
    requote_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, comp_dir, fill_price in _iter_requote_hints(requote_hints):
        row = book_rows.get(book_id)
        if row is None or _has_loan(accounts, book_id):
            continue
        book, best_bid, best_ask, mid = row
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if fill_price is not None:
            comp_edge = _completion_rt_edge_ticks(
                fill_price, comp_dir, best_bid, best_ask, tick, pdec
            )
            if comp_edge < min_rt_edge_ticks:
                continue
        fs = book_fill_score(
            book, accounts, book_id, spread_ticks=spread_ticks, requote=True
        )
        requote_jobs.append((fs + 100.0, book_id, best_bid, best_ask, mid, comp_dir))
    requote_jobs.sort(key=lambda x: -x[0])
    requote_count = 0
    for _fs, book_id, best_bid, best_ask, _mid, comp_dir in requote_jobs:
        if requote_count >= max_requote_per_tick or len(placed) >= max_books_per_tick:
            break
        if book_id in placed:
            continue
        cancel_stale_orders(
            response,
            accounts[book_id],
            book_id,
            best_bid,
            best_ask,
            tick,
            budget,
            max_cancel=2,
        )
        price = _limit_price(comp_dir, best_bid, best_ask, tick, pdec, mode="inside")
        account = accounts[book_id]
        if comp_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, comp_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        requote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if comp_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    for book_id, (_book, best_bid, best_ask, _mid) in sorted(book_rows.items()):
        if budget.total >= max_total_instructions:
            break
        cancel_stale_orders(
            response,
            accounts[book_id],
            book_id,
            best_bid,
            best_ask,
            tick,
            budget,
            max_cancel=max_instructions_per_book,
        )

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if _has_loan(accounts, book_id):
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 100.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    for _prio, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if budget.total >= max_total_instructions or len(placed) >= max_books_per_tick:
            break
        if book_id in placed:
            continue
        price = _limit_price(trade_dir, best_bid, best_ask, tick, pdec, mode="inside")
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    quote_candidates: list[tuple[float, int, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft * 0.85:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        fill_score = book_fill_score(
            book, accounts, book_id, spread_ticks=spread_ticks, requote=False
        )
        if fill_score < min_fill_score:
            continue
        quote_candidates.append((fill_score, book_id, best_bid, best_ask, mid))
    quote_candidates.sort(key=lambda x: -x[0])

    quote_count = 0
    for _fill_score, book_id, best_bid, best_ask, mid in quote_candidates:
        if quote_count >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if skew > 0.006:
            trade_dir = OrderDirection.SELL
        elif skew < -0.006:
            trade_dir = OrderDirection.BUY
        else:
            trade_dir = (
                OrderDirection.BUY if (book_id + rot) % 2 == 0 else OrderDirection.SELL
            )
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode="inside"
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        quote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )


def turbo_recover_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction: dict[int, OrderDirection],
    *,
    last_mid: dict[int, float],
    mids_scratch: dict[int, float],
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    expiry_period: int,
    inventory_skew_soft: float = 0.012,
    inventory_skew_hard: float = 0.028,
    max_books_per_tick: int = 7,
    max_instructions_per_book: int = 3,
    max_total_instructions: int = 16,
    book_rotation_groups: int = 16,
    cadence_interval_ns: int = 24_000_000_000,
    max_spread_ratio: float = 0.0012,
    min_spread_ticks: float = 4.0,
    min_rt_edge_ticks: float = 4.0,
) -> None:
    """
    Post-bleed recovery mode: cautious single-sided maker quotes on wide books.

    Targets non-zero Kappa observations before immunity ends without repeating
    the v2 overtrade / touch-cross failure mode.
    """
    vdec = simulation_config.volumeDecimals
    pdec = simulation_config.priceDecimals
    ts = state.timestamp
    tick = tick_size(pdec)
    rot = (ts // cadence_interval_ns) % max(book_rotation_groups, 1)
    active_rots = {
        rot,
        (rot + 1) % book_rotation_groups,
        (rot + 2) % book_rotation_groups,
    }
    budget = _InstructionBudget(max_instructions_per_book, max_total_instructions)

    repay_loans_fifo(
        response,
        accounts,
        simulation_config,
        min_quantity=min_quantity,
        rotate_key=ts // max(cadence_interval_ns, 1),
    )

    mids_scratch.clear()
    book_rows: dict[int, tuple[Book, float, float, float]] = {}
    for book_id, book in state.books.items():
        t = _touch(book)
        if t is None:
            continue
        best_bid, best_ask, mid = t
        spread_r = (best_ask - best_bid) / mid if mid > 0 else 1.0
        if spread_r > max_spread_ratio:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        if abs(tape_imbalance_ratio(book)) > 0.50:
            continue
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)
        last_mid[book_id] = mid

    if not book_rows:
        return

    qty = round(max(min_quantity, max_quantity * quantity_scale), vdec)
    qty = max(qty, min_quantity)
    placed: set[int] = set()

    for book_id, (_book, best_bid, best_ask, _mid) in sorted(book_rows.items()):
        if budget.total >= max_total_instructions:
            break
        cancel_stale_orders(
            response,
            accounts[book_id],
            book_id,
            best_bid,
            best_ask,
            tick,
            budget,
            max_cancel=max_instructions_per_book,
        )

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if _has_loan(accounts, book_id):
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 100.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    for _prio, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if budget.total >= max_total_instructions or len(placed) >= max_books_per_tick:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        mode = "cross" if abs(skew) >= inventory_skew_hard else "inside"
        price = _limit_price(trade_dir, best_bid, best_ask, tick, pdec, mode=mode)
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    quote_candidates: list[tuple[float, int, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        fill_score = book_fill_score(
            book, accounts, book_id, spread_ticks=spread_ticks, requote=False
        )
        quote_candidates.append((fill_score, book_id, best_bid, best_ask, mid))
    quote_candidates.sort(key=lambda x: -x[0])

    quote_count = 0
    for _fill_score, book_id, best_bid, best_ask, mid in quote_candidates:
        if quote_count >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if skew > 0.004:
            trade_dir = OrderDirection.SELL
        elif skew < -0.004:
            trade_dir = OrderDirection.BUY
        else:
            trade_dir = (
                OrderDirection.BUY if (book_id + rot) % 2 == 0 else OrderDirection.SELL
            )
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode="inside"
        )
        account = accounts[book_id]
        if trade_dir == OrderDirection.BUY:
            if price >= best_ask or account.quote_balance.free < qty * price:
                continue
        elif price <= best_bid or account.base_balance.free < qty:
            continue
        if not budget.can_place(book_id, 1):
            continue
        place_limit(response, book_id, trade_dir, qty, price, expiry_period)
        budget.mark(book_id, 1)
        placed.add(book_id)
        quote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )


