# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
Vault scoring engine — fresh design for fast κ₃, Penalty=0, positive realized PnL.

Targets (from mainnet research + agent-table top miners UID 202/26/251):
  - Always submit orders when books are tradable (no risk-off shutdown)
  - Uniform rotation across 128 books → consistent median κ, penalty stays 0
  - Two-sided inside-spread quotes → round-trip observations for κ₃
  - Completion legs after maker fills → capture spread, positive PnL
  - Moderate inventory flatten only when skew is material (not 0.3%)
"""

from __future__ import annotations

from taos.im.protocol.models import Book
from taos.im.protocol.instructions import OrderDirection

from competitive_utils import (
    _InstructionBudget,
    _completion_rt_edge_ticks,
    _limit_price,
    _rt_edge_ticks,
    _touch,
    inventory_skew,
    maker_fee_ok,
    place_limit,
    repay_loans_fifo,
    sim_order_qty,
    tick_size,
)


def vault_score_tick(
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
    max_books_per_tick: int = 8,
    max_total_instructions: int = 16,
    max_instructions_per_book: int = 2,
    max_requote_per_tick: int = 6,
    book_rotation_groups: int = 16,
    cadence_interval_ns: int = 20_000_000_000,
    rotation_windows: int = 6,
    min_spread_ticks: float = 2.0,
    min_two_sided_ticks: float = 3.0,
    min_rt_edge_ticks: float = 2.0,
    min_completion_edge_ticks: float = 3.0,
    max_spread_ratio: float = 0.002,
    inventory_skew_soft: float = 0.06,
    inventory_skew_hard: float = 0.12,
    inside_depth_ticks: int = 1,
    max_tape_imbalance: float = 0.35,
) -> None:
    requote_hints = requote_hints or {}
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
    depth = max(1, inside_depth_ticks)
    qty = sim_order_qty(min_quantity, max_quantity, quantity_scale, vdec)

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
        if not maker_fee_ok(accounts, book_id, max_fee_rate):
            continue
        mids_scratch[book_id] = mid
        book_rows[book_id] = (book, best_bid, best_ask, mid)
        last_mid[book_id] = mid

    if not book_rows:
        return

    placed: set[int] = set()

    # --- 1. Complete round-trips after maker fills (highest priority) ---
    requote_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (comp_dir, fill_price) in requote_hints.items():
        row = book_rows.get(book_id)
        if row is None:
            continue
        _book, best_bid, best_ask, mid = row
        comp_edge = _completion_rt_edge_ticks(
            fill_price, comp_dir, best_bid, best_ask, tick, pdec
        )
        if comp_edge < min_completion_edge_ticks:
            continue
        requote_jobs.append((comp_edge + 100.0, book_id, best_bid, best_ask, mid, comp_dir))
    requote_jobs.sort(key=lambda x: -x[0])

    requote_count = 0
    for _prio, book_id, best_bid, best_ask, _mid, trade_dir in requote_jobs:
        if requote_count >= max_requote_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode="inside", depth_ticks=depth
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

    # --- 2. Flatten material inventory skew ---
    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 100.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    for _prio, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if len(placed) >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        mode = "cross" if abs(skew) >= inventory_skew_hard else "inside"
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode=mode, depth_ticks=depth
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
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    # --- 3. Two-sided inside-spread quotes (κ round-trip observations) ---
    two_sided: list[tuple[float, int, float, float, float]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed:
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_two_sided_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) > inventory_skew_soft * 0.5:
            continue
        two_sided.append((spread_ticks, book_id, best_bid, best_ask, mid))
    two_sided.sort(key=lambda x: -x[0])

    two_sided_count = 0
    max_two_sided = max(2, max_books_per_tick // 2)
    for _spread, book_id, best_bid, best_ask, mid in two_sided:
        if two_sided_count >= max_two_sided or len(placed) >= max_books_per_tick:
            break
        if book_id in placed or not budget.can_place(book_id, 2):
            continue
        buy_price = _limit_price(
            OrderDirection.BUY, best_bid, best_ask, tick, pdec,
            mode="inside", depth_ticks=depth,
        )
        sell_price = _limit_price(
            OrderDirection.SELL, best_bid, best_ask, tick, pdec,
            mode="inside", depth_ticks=depth,
        )
        account = accounts[book_id]
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
            two_sided_count += 1

    # --- 4. Single-sided rotation quotes (always-on coverage) ---
    single_jobs: list[tuple[int, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed:
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_spread_ticks:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if skew > inventory_skew_soft * 0.35:
            trade_dir = OrderDirection.SELL
        elif skew < -inventory_skew_soft * 0.35:
            trade_dir = OrderDirection.BUY
        else:
            trade_dir = (
                OrderDirection.BUY
                if (book_id + rot) % 2 == 0
                else OrderDirection.SELL
            )
        single_jobs.append((book_id, book_id, best_bid, best_ask, mid, trade_dir))
    single_jobs.sort(key=lambda x: x[0])

    quote_count = 0
    for _sort, book_id, best_bid, best_ask, mid, trade_dir in single_jobs:
        if quote_count >= max_books_per_tick or len(placed) >= max_books_per_tick:
            break
        if budget.total >= max_total_instructions or book_id in placed:
            continue
        price = _limit_price(
            trade_dir, best_bid, best_ask, tick, pdec, mode="inside", depth_ticks=depth
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
        quote_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    # --- 5. Bootstrap: guarantee at least one order per tick ---
    if not response.instructions:
        for book_id in sorted(book_rows.keys()):
            _book, best_bid, best_ask, mid = book_rows[book_id]
            spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
            if spread_ticks < 1.0:
                continue
            skew = inventory_skew(accounts, book_id, mid)
            trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
            mode = "join_touch" if spread_ticks < min_spread_ticks else "inside"
            price = _limit_price(
                trade_dir, best_bid, best_ask, tick, pdec, mode=mode, depth_ticks=depth
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
            direction[book_id] = (
                OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
            )
            break
