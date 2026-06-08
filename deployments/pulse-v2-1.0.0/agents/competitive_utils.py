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
) -> None:
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
    qty = clamp_qty(min_quantity, vdec)
    if qty < min_quantity:
        return False
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
        + tape_notional(book) * 0.00015
        + touch_depth(book) * 0.35
        + maker_rebate_bonus(accounts, book_id)
        + mtr_maker_bonus(book)
    )
    if requote:
        score += 80.0
    return score


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
        edge_slots = max(4, max_books_per_tick - two_sided_cap)
        touch_join_on_requote = touch_join_on_requote or True
        max_requote_per_tick = max(max_requote_per_tick, 5)
    elif profile == "edge":
        two_sided_cap = min(2, max_two_sided_per_tick)
        edge_slots = max_books_per_tick
    elif profile == "apex":
        two_sided_cap = min(3, max_two_sided_per_tick)
        edge_slots = max(2, max_books_per_tick - two_sided_cap)
        touch_join_on_requote = True
        max_requote_per_tick = max(max_requote_per_tick, 6)
    elif profile == "select":
        two_sided_cap = min(2, max_two_sided_per_tick)
        edge_slots = max(4, max_books_per_tick - two_sided_cap)
    else:
        two_sided_cap = max_two_sided_per_tick
        edge_slots = max(3, max_books_per_tick - two_sided_cap)

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

    def _place_limit(
        book_id: int,
        trade_dir: OrderDirection,
        qty: float,
        best_bid: float,
        best_ask: float,
        *,
        inside: bool,
    ) -> bool:
        if not budget.can_place(book_id, 1):
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
    for book_id, comp_dir in requote_hints.items():
        row = book_rows.get(book_id)
        if row is None or _has_loan(accounts, book_id):
            continue
        book, best_bid, best_ask, mid = row
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
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
        if _place_limit(
            book_id,
            comp_dir,
            _qty(strength),
            best_bid,
            best_ask,
            inside=not touch_join_on_requote,
        ):
            placed_books.add(book_id)
            requote_count += 1

    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    two_sided: list[tuple[float, int, float, float, float]] = []
    edge_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []

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

        if abs(skew) >= inventory_skew_soft:
            trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
            strength = 12.0 + abs(skew) * 30.0 + fill_sc * 0.02
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
        if not budget.can_place(book_id, 2):
            continue
        _prep_book(book_id, best_bid, best_ask)
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
            book_id, trade_dir, _qty(strength), best_bid, best_ask, inside=True
        ):
            placed_books.add(book_id)
            edge_count += 1
