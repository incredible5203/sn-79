"""
MicrostructureEdgeAgent — SN-79 Competitive Miner Agent
=========================================================
Concept: Orderflow Imbalance + Queue-Position Aware Market Making

STRATEGY OVERVIEW
-----------------
While AscendKappaAgent uses touch-join for high fill rate, this agent is
more selective: it reads the L3 event tape each tick, computes:

  1. ORDER FLOW IMBALANCE (OFI): net signed volume in the event tape
  2. QUEUE DEPTH SIGNAL: position in the visible order queue (top 5 levels)
  3. CROSS-BOOK MEDIAN: mid price deviation across all books (relative value)

It then makes directional bets — quote only on the side that flow favors,
skip when tape is toxic, and aim for higher per-trip profit at the cost of
lower fill frequency.

WHY THIS HELPS κ₃
-----------------
κ₃ = μ / LPM3^(1/3)
This agent sacrifices some μ (fewer trades) to guarantee LPM3 ≈ 0
(almost no losing round-trips). That ratio can beat a high-volume,
moderate-loss agent in κ₃ terms.

COMPLEMENTARY USE
-----------------
Deploy both AscendKappaAgent AND MicrostructureEdgeAgent on different
book ranges to avoid self-trading and maximize book coverage.

Usage
-----
AGENT_CLASS=MicrostructureEdgeAgent
AGENT_MODULE=MicrostructureEdgeAgent
"""

from __future__ import annotations

import os
import sys
import time
import math
import logging
from collections import defaultdict, deque
from typing import Optional, List, Tuple

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _sn79_compat import CompatFinanceAgentResponse, is_trade_notice, unwrap_response

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────

# Selectivity (higher = more selective, fewer fills but cleaner κ)
MIN_SPREAD_TICKS         = 4       # skip tighter books entirely
MIN_DIRECTIONAL_EDGE     = 0.15    # OFI signal threshold to quote
MIN_RT_EDGE_TICKS        = 3       # minimum edge ticks to accept a round-trip
MAX_MAKER_FEE            = 0.0013

# Order sizing
BASE_QUANTITY            = 0.30
MIN_QUANTITY             = 0.25
MAX_INVENTORY_SKEW       = 0.012

# Rotation (cover a DIFFERENT set of books vs AscendKappaAgent)
ROTATION_GROUPS          = 12
ROTATION_OFFSET          = 6      # set to half ROTATION_GROUPS to interleave
MAX_BOOKS_PER_TICK       = 9

# Instruction budget
MAX_TOTAL_INSTRUCTIONS   = 26
MAX_PER_BOOK             = 3

# Signal window
OFI_WINDOW_TICKS         = 4      # ticks to accumulate OFI before acting
STALE_AGE_SECONDS        = 6      # cancel stale orders older than this
STALE_TICKS_OUTSIDE      = 2

GRACE_PERIOD_SECONDS     = 620


# ─────────────────────────────────────────────────────────────────────────────
# MICROSTRUCTURE SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

class OFIState:
    """Accumulated Order Flow Imbalance per book."""

    def __init__(
        self,
        bid_volume_arrived: float = 0.0,
        ask_volume_arrived: float = 0.0,
        bid_volume_cancelled: float = 0.0,
        ask_volume_cancelled: float = 0.0,
        trade_buy_volume: float = 0.0,
        trade_sell_volume: float = 0.0,
        ticks_accumulated: int = 0,
    ):
        self.bid_volume_arrived = bid_volume_arrived
        self.ask_volume_arrived = ask_volume_arrived
        self.bid_volume_cancelled = bid_volume_cancelled
        self.ask_volume_cancelled = ask_volume_cancelled
        self.trade_buy_volume = trade_buy_volume
        self.trade_sell_volume = trade_sell_volume
        self.ticks_accumulated = ticks_accumulated

    def ofi(self) -> float:
        """
        Order Flow Imbalance: signed flow pressure.
        Positive = buy pressure, Negative = sell pressure.
        Range roughly [-1, +1] when normalized.
        """
        buy  = self.trade_buy_volume  + self.bid_volume_arrived  - self.ask_volume_cancelled
        sell = self.trade_sell_volume + self.ask_volume_arrived  - self.bid_volume_cancelled
        total = buy + sell
        if total < 1e-9:
            return 0.0
        return (buy - sell) / total

    def reset(self):
        self.bid_volume_arrived   = 0.0
        self.ask_volume_arrived   = 0.0
        self.bid_volume_cancelled = 0.0
        self.ask_volume_cancelled = 0.0
        self.trade_buy_volume     = 0.0
        self.trade_sell_volume    = 0.0
        self.ticks_accumulated    = 0


def compute_ofi_from_events(book) -> Tuple[float, float, float]:
    """
    Parse book.events and return (ofi, buy_vol, sell_vol).
    OFI in [-1, +1]; buy_vol and sell_vol are raw volumes.
    """
    buy_vol = sell_vol = 0.0
    new_bid_vol = new_ask_vol = 0.0
    cancel_bid_vol = cancel_ask_vol = 0.0

    try:
        for ev in book.events:
            y = getattr(ev, 'y', None)
            if y is None:
                y = type(ev).__name__[0].lower()

            if y == 't':  # TradeInfo
                q = getattr(ev, 'quantity', 0.0)
                s = getattr(ev, 'side', 0)
                if s == 0:
                    buy_vol += q
                else:
                    sell_vol += q

            elif y == 'o':  # Order (new resting order)
                q = getattr(ev, 'quantity', 0.0)
                s = getattr(ev, 'side', 0)
                if s == 0:
                    new_bid_vol += q
                else:
                    new_ask_vol += q

            elif y == 'c':  # Cancellation
                q = getattr(ev, 'quantity', 0.0)
                p = getattr(ev, 'price', 0.0)
                try:
                    best_bid = book.bids[0].price if book.bids else 0.0
                    best_ask = book.asks[0].price if book.asks else float('inf')
                    if p <= best_bid:
                        cancel_bid_vol += q
                    else:
                        cancel_ask_vol += q
                except Exception:
                    cancel_ask_vol += q

    except Exception:
        pass

    total_signed_buy  = buy_vol + new_bid_vol - cancel_ask_vol
    total_signed_sell = sell_vol + new_ask_vol - cancel_bid_vol
    total = total_signed_buy + total_signed_sell
    ofi = (total_signed_buy - total_signed_sell) / max(total, 1e-9)
    return ofi, buy_vol, sell_vol


def queue_position_signal(book, our_orders: list) -> dict:
    """
    For each of our resting orders in the top 5 levels, compute:
    - queue_ahead: volume ahead of us in the queue
    - level_total: total volume at that level
    Returns dict: order_id → {'queue_ahead': float, 'level_total': float}
    """
    result = {}
    try:
        our_prices = {o.price: o.id for o in our_orders}

        for level_list, side in [(book.bids, 'bid'), (book.asks, 'ask')]:
            for level in level_list[:5]:  # only top 5 have order detail
                if level.price not in our_prices:
                    continue
                oid = our_prices[level.price]
                ahead = 0.0
                found = False
                for order in level.orders:
                    if order.id == oid:
                        found = True
                        break
                    ahead += order.quantity
                if found:
                    result[oid] = {
                        'queue_ahead':  ahead,
                        'level_total':  level.quantity,
                        'price':        level.price,
                        'side':         side,
                    }
    except Exception:
        pass
    return result


def cross_book_median_mid(books: dict, book_ids: list) -> Optional[float]:
    """Compute median mid price across a set of books."""
    mids = []
    for bid in book_ids:
        b = books.get(bid)
        if b is None:
            continue
        try:
            bids = b.bids
            asks = b.asks
            if bids and asks:
                mids.append((bids[0].price + asks[0].price) / 2)
        except Exception:
            pass
    if not mids:
        return None
    mids.sort()
    n = len(mids)
    if n % 2 == 1:
        return mids[n // 2]
    return (mids[n // 2 - 1] + mids[n // 2]) / 2


# ─────────────────────────────────────────────────────────────────────────────
# INSTRUCTION BUDGET
# ─────────────────────────────────────────────────────────────────────────────

class Budget:
    def __init__(self, total=MAX_TOTAL_INSTRUCTIONS, per_book=MAX_PER_BOOK):
        self.total_cap = total
        self.per_book_cap = per_book
        self._total = 0
        self._book = defaultdict(int)

    def ok(self, book_id: int, n=1) -> bool:
        return self._total + n <= self.total_cap and self._book[book_id] + n <= self.per_book_cap

    def use(self, book_id: int, n=1):
        self._total += n
        self._book[book_id] += n


# ─────────────────────────────────────────────────────────────────────────────
# AGENT
# ─────────────────────────────────────────────────────────────────────────────

class MicrostructureEdgeAgent:
    """
    Selective directional market maker using OFI + queue signals.

    Key difference from AscendKappaAgent:
    - Only quotes when OFI signal is clear (directional conviction)
    - Monitors queue position; cancel and reprice if buried too deep
    - Uses cross-book relative value to catch mean-reverting books
    """

    def __init__(self, uid: int, config=None, log_dir=None, **kwargs):
        self.uid        = uid
        self.config     = config
        self.log_dir    = log_dir
        self.accounts   = {}
        self.events     = []
        self.timestamp  = 0

        self._tick             = 0
        self._bucket           = 0
        self._cancel_done      = False

        # Completion tracking
        self._completions: dict[int, list] = defaultdict(list)

        # OFI accumulators per book
        self._ofi_state: dict[int, OFIState] = defaultdict(OFIState)

        # Rolling OFI per book (last N ticks)
        self._ofi_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=OFI_WINDOW_TICKS))

        # Blacklist (books to skip temporarily)
        self._skip_until: dict[int, int] = defaultdict(int)

    def process(self, notification):
        notification.acknowledged = True
        return notification

    # ─────────────────────────────────────────────────────────────────────────

    def handle(self, state) -> object:
        t0 = time.time()
        try:
            self.update(state)
            resp = self.respond(state)
        except Exception as e:
            logger.error(f"MicrostructureEdgeAgent error: {e}", exc_info=True)
            resp = self._make_response()
        elapsed = time.time() - t0
        if elapsed > 2.0:
            logger.warning(f"Slow tick {elapsed:.2f}s")
        return unwrap_response(resp)

    def update(self, state):
        self.config    = state.config
        self.timestamp = state.timestamp
        uid = self.uid

        self.accounts = {}
        try:
            self.accounts = state.accounts.get(uid, {})
        except Exception:
            pass

        self.events = []
        try:
            self.events = state.notices.get(uid, [])
        except Exception:
            pass

        self._tick += 1
        self._process_notices()

    def respond(self, state) -> object:
        if self.timestamp < GRACE_PERIOD_SECONDS * 1_000_000_000:
            return self._make_response()

        resp   = self._make_response()
        config = self.config
        tick   = self._tick_size()

        # Phase 0: startup cancel
        if not self._cancel_done:
            self._startup_cancel(resp)
            if self._count_open_orders() > 0:
                return resp
            self._cancel_done = True

        budget = Budget()

        # Pre-compute cross-book median for relative-value signal
        all_book_ids = list(state.books.keys()) if state.books else []
        cross_mid = cross_book_median_mid(state.books, all_book_ids)

        # Update OFI for all visible books
        if state.books:
            for book_id, book in state.books.items():
                ofi, bv, sv = compute_ofi_from_events(book)
                self._ofi_history[book_id].append(ofi)

        # Phase 1: loan repay
        for book_id, account in self.accounts.items():
            if not budget.ok(book_id):
                continue
            if getattr(account, 'loans', None):
                try:
                    resp.close_positions(book_id=book_id, settlement_option='FIFO')
                    budget.use(book_id)
                    break
                except Exception:
                    pass

        # Phase 2: cancel stale
        self._cancel_stale(state, resp, budget, tick)

        # Phase 3: completions
        self._place_completions(state, resp, budget, tick)

        # Phase 4: new quotes (OFI-directed)
        self._place_ofi_quotes(state, resp, budget, tick, cross_mid)

        self._bucket = (self._bucket + 1) % ROTATION_GROUPS
        return resp

    # ─────────────────────────────────────────────────────────────────────────
    # NOTICE PROCESSING
    # ─────────────────────────────────────────────────────────────────────────

    def _process_notices(self):
        for notice in self.events:
            if is_trade_notice(notice):
                self._on_trade(notice)
            else:
                t = str(getattr(notice, 'type', '') or type(notice).__name__).lower()
                if ('placement' in t or 'limit' in t) and not getattr(notice, 'success', True):
                    book_id = getattr(notice, 'bookId', None)
                    msg     = str(getattr(notice, 'message', '')).lower()
                    if book_id and 'loan' in msg:
                        self._skip_until[book_id] = self._tick + 15

    def _on_trade(self, notice):
        try:
            book_id  = getattr(notice, 'bookId', None)
            side     = getattr(notice, 'side', None)
            price    = getattr(notice, 'price', 0.0)
            qty      = getattr(notice, 'quantity', 0.0)
            maker_id = getattr(notice, 'makerAgentId', None) or getattr(notice, 'maker_agent_id', None)

            if book_id is None or price <= 0 or maker_id != self.uid:
                return

            # side is taker's side
            comp_side = "BUY" if (side == 0 or str(side).lower() in ('0', 'buy')) else "SELL"
            self._completions[book_id].append({
                'side': comp_side, 'fill_price': price, 'qty': qty, 'attempts': 0
            })
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # STARTUP CANCEL
    # ─────────────────────────────────────────────────────────────────────────

    def _startup_cancel(self, resp):
        n = 0
        for book_id, account in self.accounts.items():
            try:
                ids = [o.id for o in account.orders]
                if ids:
                    resp.cancel_orders(book_id=book_id, order_ids=ids)
                    n += len(ids)
                    if n >= 28:
                        break
            except Exception:
                pass

    def _count_open_orders(self) -> int:
        return sum(len(getattr(a, 'orders', [])) for a in self.accounts.values())

    # ─────────────────────────────────────────────────────────────────────────
    # STALE CANCEL
    # ─────────────────────────────────────────────────────────────────────────

    def _cancel_stale(self, state, resp, budget: Budget, tick: float):
        cancelled = 0
        for book_id, account in self.accounts.items():
            if not budget.ok(book_id):
                continue
            try:
                orders = account.orders
                if not orders:
                    continue
                book = state.books.get(book_id)
                if not book:
                    continue
                bid_p = book.bids[0].price if book.bids else None
                ask_p = book.asks[0].price if book.asks else None
                if not bid_p or not ask_p:
                    continue

                stale = []
                for o in orders:
                    age = (self.timestamp - o.timestamp) / 1_000_000_000
                    if o.side == 0:  # bid
                        stale_price = o.price < bid_p - STALE_TICKS_OUTSIDE * tick
                    else:            # ask
                        stale_price = o.price > ask_p + STALE_TICKS_OUTSIDE * tick
                    if stale_price or age > STALE_AGE_SECONDS:
                        stale.append(o.id)

                if stale:
                    resp.cancel_orders(book_id=book_id, order_ids=stale)
                    budget.use(book_id)
                    cancelled += len(stale)
                    if cancelled >= 10:
                        break
            except Exception:
                continue

    # ─────────────────────────────────────────────────────────────────────────
    # COMPLETION LEGS
    # ─────────────────────────────────────────────────────────────────────────

    def _place_completions(self, state, resp, budget: Budget, tick: float):
        done = []
        for book_id, hints in list(self._completions.items()):
            if not hints:
                continue
            if not budget.ok(book_id):
                continue
            hint = hints[0]
            try:
                book    = state.books.get(book_id)
                account = self.accounts.get(book_id)
                if not book or not account:
                    continue
                bid_p = book.bids[0].price if book.bids else None
                ask_p = book.asks[0].price if book.asks else None
                if not bid_p or not ask_p:
                    continue

                side  = hint['side']
                fpx   = hint['fill_price']
                qty   = round(max(hint['qty'], MIN_QUANTITY),
                              getattr(self.config, 'volumeDecimals', 4))
                p_dec = getattr(self.config, 'priceDecimals', 2)

                # Same no-loss-clamp principle as AscendKappaAgent: only
                # complete when the market offers >= MIN_RT_EDGE_TICKS edge
                # vs the original fill price; otherwise hold (re-quote later).
                if side == "BUY":
                    required_max = fpx - MIN_RT_EDGE_TICKS * tick
                    if bid_p > required_max + 1e-12:
                        hint['attempts'] += 1
                        if hint['attempts'] > 60:
                            price = round(bid_p, p_dec)
                        else:
                            continue
                    else:
                        price = round(min(bid_p, required_max), p_dec)

                    needed = qty * price
                    if account.quote_balance.free < needed:
                        qty = round(max(account.quote_balance.free / max(price,1) * 0.9,
                                        MIN_QUANTITY),
                                    getattr(self.config, 'volumeDecimals', 4))
                    if qty < MIN_QUANTITY:
                        continue
                    resp.limit_order(book_id=book_id, direction='BUY', quantity=qty,
                                     price=price, post_only=True)
                else:
                    required_min = fpx + MIN_RT_EDGE_TICKS * tick
                    if ask_p < required_min - 1e-12:
                        hint['attempts'] += 1
                        if hint['attempts'] > 60:
                            price = round(ask_p, p_dec)
                        else:
                            continue
                    else:
                        price = round(max(ask_p, required_min), p_dec)

                    if account.base_balance.free < qty:
                        qty = round(max(account.base_balance.free * 0.9, MIN_QUANTITY),
                                    getattr(self.config, 'volumeDecimals', 4))
                    if qty < MIN_QUANTITY:
                        continue
                    resp.limit_order(book_id=book_id, direction='SELL', quantity=qty,
                                     price=price, post_only=True)

                budget.use(book_id)
                done.append(book_id)
                logger.debug(f"COMPLETION {side} {qty}@{price} BOOK {book_id}")
            except Exception as e:
                logger.debug(f"Completion error {book_id}: {e}")

        for b in done:
            if self._completions.get(b):
                self._completions[b].pop(0)
                if not self._completions[b]:
                    del self._completions[b]

    # ─────────────────────────────────────────────────────────────────────────
    # OFI-DIRECTED QUOTES
    # ─────────────────────────────────────────────────────────────────────────

    def _place_ofi_quotes(self, state, resp, budget: Budget, tick: float,
                          cross_mid: Optional[float]):
        """Quote only when OFI gives directional conviction."""
        config     = self.config
        p_dec      = getattr(config, 'priceDecimals', 2)
        v_dec      = getattr(config, 'volumeDecimals', 4)
        book_count = getattr(config, 'book_count', 128)

        # Bucket for this tick (offset to cover different books than AscendKappa)
        bucket = (self._bucket + ROTATION_OFFSET) % ROTATION_GROUPS

        candidates = []
        for book_id in range(book_count):
            if book_id % ROTATION_GROUPS != bucket:
                continue
            if self._skip_until[book_id] > self._tick:
                continue
            if self._completions.get(book_id):
                continue

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if not book or not account:
                continue

            try:
                bid_p = book.bids[0].price if book.bids else None
                ask_p = book.asks[0].price if book.asks else None
            except Exception:
                continue

            if not bid_p or not ask_p:
                continue

            spread_ticks = (ask_p - bid_p) / tick if tick > 0 else 0
            if spread_ticks < MIN_SPREAD_TICKS:
                continue

            mid = (bid_p + ask_p) / 2

            # Fee filter
            try:
                if account.fees.maker_fee_rate > MAX_MAKER_FEE:
                    continue
            except Exception:
                pass

            # Get rolling OFI signal
            history = list(self._ofi_history.get(book_id, deque()))
            if not history:
                ofi_signal = 0.0
            else:
                # Exponentially weighted average (recent ticks matter more)
                weights = [2**(i) for i in range(len(history))]
                ofi_signal = sum(w * o for w, o in zip(weights, history)) / sum(weights)

            # Relative-value signal (is this book cheap or expensive vs others?)
            rv_signal = 0.0
            if cross_mid and cross_mid > 0:
                rv_signal = (mid - cross_mid) / cross_mid  # positive = expensive

            # Combined directional signal
            # OFI > MIN_DIRECTIONAL_EDGE → buy pressure → quote BID (get filled as maker)
            # OFI < -MIN_DIRECTIONAL_EDGE → sell pressure → quote ASK

            # When buying pressure, place ask quote (sell into the buying flow)
            # When selling pressure, place bid quote (buy from the selling flow)
            if ofi_signal > MIN_DIRECTIONAL_EDGE:
                direction = "SELL"   # buyers coming → sit on ask
                conviction = ofi_signal
            elif ofi_signal < -MIN_DIRECTIONAL_EDGE:
                direction = "BUY"    # sellers coming → sit on bid
                conviction = -ofi_signal
            else:
                # Weak signal — skip unless RV is interesting
                if abs(rv_signal) > 0.001:
                    direction = "BUY" if rv_signal < 0 else "SELL"
                    conviction = abs(rv_signal)
                else:
                    continue

            # Inventory skew override
            try:
                bv = account.base_balance.total * mid
                qv = account.quote_balance.total
                tv = bv + qv
                skew = (bv - qv) / max(tv, 1.0)
            except Exception:
                skew = 0.0

            if skew > MAX_INVENTORY_SKEW and direction == "BUY":
                direction = "SELL"
            elif skew < -MAX_INVENTORY_SKEW and direction == "SELL":
                direction = "BUY"

            candidates.append((conviction, book_id, direction, bid_p, ask_p, account))

        candidates.sort(key=lambda x: -x[0])

        placed = 0
        for conviction, book_id, direction, bid_p, ask_p, account in candidates:
            if placed >= MAX_BOOKS_PER_TICK:
                break
            if not budget.ok(book_id):
                continue

            qty = round(max(BASE_QUANTITY, MIN_QUANTITY), v_dec)

            if direction == "SELL":
                price = round(ask_p, p_dec)
                try:
                    if account.base_balance.free < qty:
                        qty = round(max(account.base_balance.free, MIN_QUANTITY), v_dec)
                except Exception:
                    pass
            else:
                price = round(bid_p, p_dec)
                try:
                    needed = qty * price
                    if account.quote_balance.free < needed:
                        qty = round(max(account.quote_balance.free / max(price, 1),
                                       MIN_QUANTITY), v_dec)
                except Exception:
                    pass

            if qty < MIN_QUANTITY:
                continue

            # Don't double-post at same price
            try:
                existing = {o.price for o in account.orders}
                if price in existing:
                    continue
            except Exception:
                pass

            try:
                resp.limit_order(
                    book_id=book_id, direction=direction,
                    quantity=qty, price=price,
                    post_only=True, time_in_force='GTC')
                budget.use(book_id)
                placed += 1
                logger.debug(f"OFI {direction} {qty}@{price} BOOK {book_id} (ofi={conviction:.3f})")
            except Exception as e:
                logger.debug(f"OFI place error {book_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────────

    def _tick_size(self) -> float:
        try:
            return round(10 ** -self.config.priceDecimals, self.config.priceDecimals)
        except Exception:
            return 0.01

    def _make_response(self):
        return CompatFinanceAgentResponse(agent_id=self.uid, accounts=self.accounts)
