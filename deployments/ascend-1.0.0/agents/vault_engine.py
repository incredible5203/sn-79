# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
Vault scoring engine — maximum throughput + PnL guardrails (UID 209).

Targets top-miner metrics (UID 202/26/251): 4M+ RT volume, lean BASE (~5k),
κ→1+, positive realized PnL. v1.4 matches their fill-rate pattern:
  completion → flatten → two-sided → touch-join → inside → bootstrap
"""

from __future__ import annotations

from taos.im.protocol.models import Book
from taos.im.protocol.instructions import OrderDirection

from competitive_utils import (
    _InstructionBudget,
    _completion_rt_edge_ticks,
    _has_loan,
    _limit_price,
    _rt_edge_ticks,
    _touch,
    book_fill_score,
    cancel_stale_orders,
    inventory_skew,
    maker_fee_ok,
    microprice,
    place_limit,
    repay_loans_fifo,
    sim_order_qty,
    tape_imbalance_ratio,
    tick_size,
)


def _pick_direction(
    book: Book,
    book_id: int,
    mid: float,
    accounts,
    rot: int,
    *,
    skew_soft: float,
    min_microprice_edge_ticks: float,
    tick: float,
) -> OrderDirection:
    skew = inventory_skew(accounts, book_id, mid)
    if skew > skew_soft * 0.3:
        return OrderDirection.SELL
    if skew < -skew_soft * 0.3:
        return OrderDirection.BUY
    mp = microprice(book)
    if mp is not None and tick > 0:
        edge_ticks = (mp - mid) / tick
        if edge_ticks >= min_microprice_edge_ticks:
            return OrderDirection.BUY
        if edge_ticks <= -min_microprice_edge_ticks:
            return OrderDirection.SELL
    return (
        OrderDirection.BUY
        if (book_id + rot) % 2 == 0
        else OrderDirection.SELL
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
    max_books_per_tick: int = 11,
    max_total_instructions: int = 28,
    max_instructions_per_book: int = 5,
    max_requote_per_tick: int = 9,
    max_touch_per_tick: int = 10,
    book_rotation_groups: int = 12,
    cadence_interval_ns: int = 12_000_000_000,
    rotation_windows: int = 8,
    min_spread_ticks: float = 3.0,
    min_quote_spread_ticks: float = 3.5,
    min_rt_edge_ticks: float = 3.0,
    min_completion_edge_ticks: float = 5.0,
    min_two_sided_ticks: float = 5.5,
    touch_join_spread_ticks: float = 3.5,
    min_microprice_edge_ticks: float = 0.7,
    max_spread_ratio: float = 0.0013,
    inventory_skew_soft: float = 0.002,
    inventory_skew_hard: float = 0.005,
    inside_depth_ticks: int = 1,
    max_tape_imbalance: float = 0.30,
    max_flatten_per_tick: int = 12,
    cold_book_volume_threshold: float = 250.0,
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

    # --- 1. Complete round-trips after maker fills ---
    requote_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (comp_dir, fill_price) in requote_hints.items():
        row = book_rows.get(book_id)
        if row is None or _has_loan(accounts, book_id):
            continue
        _book, best_bid, best_ask, mid = row
        comp_edge = _completion_rt_edge_ticks(
            fill_price, comp_dir, best_bid, best_ask, tick, pdec
        )
        if comp_edge < min_completion_edge_ticks:
            continue
        fs = book_fill_score(
            _book, accounts, book_id,
            spread_ticks=(best_ask - best_bid) / tick if tick > 0 else 0.0,
            requote=True,
        )
        requote_jobs.append((comp_edge + fs * 0.1 + 200.0, book_id, best_bid, best_ask, mid, comp_dir))
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

    # --- 2. Flatten inventory skew ---
    flatten_jobs: list[tuple[float, int, float, float, float, OrderDirection]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if _has_loan(accounts, book_id):
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) < inventory_skew_soft:
            continue
        trade_dir = OrderDirection.SELL if skew > 0 else OrderDirection.BUY
        flatten_jobs.append((abs(skew) * 300.0, book_id, best_bid, best_ask, mid, trade_dir))
    flatten_jobs.sort(key=lambda x: -x[0])

    flatten_count = 0
    for _prio, book_id, best_bid, best_ask, mid, trade_dir in flatten_jobs:
        if flatten_count >= max_flatten_per_tick or budget.total >= max_total_instructions:
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
        flatten_count += 1
        direction[book_id] = (
            OrderDirection.SELL if trade_dir == OrderDirection.BUY else OrderDirection.BUY
        )

    # --- 3. Two-sided on wide spreads ---
    two_sided: list[tuple[float, int, float, float, float]] = []
    for book_id, (_book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_two_sided_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks + 1.0:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) > inventory_skew_soft * 0.2:
            continue
        two_sided.append((spread_ticks, book_id, best_bid, best_ask, mid))
    two_sided.sort(key=lambda x: -x[0])

    two_sided_count = 0
    max_two_sided = max(3, max_books_per_tick // 3)
    for _spread, book_id, best_bid, best_ask, mid in two_sided:
        if two_sided_count >= max_two_sided or budget.total >= max_total_instructions:
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

    # --- 4. Touch-join (primary fill-rate driver) ---
    touch_jobs: list[tuple[float, int, Book, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < touch_join_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft * 0.25:
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
            (fill_sc + spread_ticks * 0.7 + cold_bonus, book_id, book, best_bid, best_ask, mid)
        )
    touch_jobs.sort(key=lambda x: -x[0])

    touch_count = 0
    for _rank, book_id, book, best_bid, best_ask, mid in touch_jobs:
        if touch_count >= max_touch_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        trade_dir = _pick_direction(
            book, book_id, mid, accounts, rot,
            skew_soft=inventory_skew_soft,
            min_microprice_edge_ticks=min_microprice_edge_ticks,
            tick=tick,
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

    # --- 5. Inside-spread rotation quotes ---
    single_jobs: list[tuple[float, int, Book, float, float, float]] = []
    for book_id, (book, best_bid, best_ask, mid) in book_rows.items():
        if book_id in placed or _has_loan(accounts, book_id):
            continue
        if (book_id % book_rotation_groups) not in active_rots:
            continue
        spread_ticks = (best_ask - best_bid) / tick if tick > 0 else 0.0
        if spread_ticks < min_quote_spread_ticks:
            continue
        if _rt_edge_ticks(best_bid, best_ask, tick, pdec) < min_rt_edge_ticks:
            continue
        skew = inventory_skew(accounts, book_id, mid)
        if abs(skew) >= inventory_skew_soft:
            continue
        single_jobs.append((spread_ticks, book_id, book, best_bid, best_ask, mid))
    single_jobs.sort(key=lambda x: -x[0])

    quote_count = 0
    for spread_ticks, book_id, book, best_bid, best_ask, mid in single_jobs:
        if quote_count >= max_books_per_tick or budget.total >= max_total_instructions:
            break
        if book_id in placed:
            continue
        trade_dir = _pick_direction(
            book, book_id, mid, accounts, rot,
            skew_soft=inventory_skew_soft,
            min_microprice_edge_ticks=min_microprice_edge_ticks,
            tick=tick,
        )
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

    # --- 6. Bootstrap: widest book with edge ---
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
        for spread_ticks, book_id, book, best_bid, best_ask, mid in bootstrap_jobs:
            trade_dir = _pick_direction(
                book, book_id, mid, accounts, rot,
                skew_soft=inventory_skew_soft,
                min_microprice_edge_ticks=min_microprice_edge_ticks,
                tick=tick,
            )
            skew = inventory_skew(accounts, book_id, mid)
            if (
                spread_ticks >= touch_join_spread_ticks
                and abs(skew) < inventory_skew_soft * 0.4
            ):
                mode = "join_touch"
            elif spread_ticks >= min_quote_spread_ticks:
                mode = "inside"
            else:
                continue
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
