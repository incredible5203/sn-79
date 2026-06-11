"""
HybridResilientAgent — SN-79 Competitive Miner Agent
=====================================================
Target:  Kappa-3 (kappa score) UP, Realized PnL UP, Penalty == 0, FAST.

DESIGN PHILOSOPHY
------------------
TradingScore = 0.79 * KappaScore + 0.21 * PnLScore
KappaScore is driven per-book by Kappa-3 = mean_return / LPM3^(1/3),
then aggregated with a median + outlier PENALTY for books that lag
badly behind the median. This means there are really TWO separate
jobs, and they should not be done by the same logic:

  JOB A — "BE EVERYWHERE" (drives Penalty -> 0)
    Every one of the 128 books needs SOME round-trip activity so it
    doesn't become a statistical outlier dragging the median down.
    This needs to be cheap, safe, minimal-size, symmetric, and
    touch every book on a short cycle.

  JOB B — "MAKE MONEY WHERE IT'S GOOD" (drives mu UP, LPM3 DOWN)
    A smaller rotating set of books gets larger, directionally
    informed quoting (OFI + relative value), with strict
    profit-or-hold completion logic so realized losses are rare
    and small (crushes LPM3, the cube-root term that dominates
    Kappa-3's denominator).

KEY MECHANISMS THAT DIFFER FROM SIMPLER AGENTS
-----------------------------------------------
1. PER-BOOK POSITION LEDGER (VWAP cost basis)
   Every fill (maker or taker, buy or sell) updates a running
   net position and volume-weighted average cost for that book.
   Completion targets are computed against this VWAP, not against
   the last individual fill price -- so partial fills, multiple
   fills per tick, and opposite-direction fills all net out
   correctly instead of leaking untracked inventory.

2. NO-LOSS COMPLETION RULE
   A completion (the leg that closes out existing inventory) is
   ONLY placed at a price that gives >= MIN_RT_EDGE_TICKS profit
   vs the VWAP cost basis. If the market hasn't moved enough yet,
   the agent does NOT force a crossing/loss trade -- it just
   continues to hold and re-quote passively. This directly targets
   LPM3 (the cube-root downside term): round trips that DO
   complete are virtually never losers.
   A "long-stuck" escape valve (after N ticks) unwinds at
   breakeven-ish (best bid/ask, still passive, never crossing)
   rather than forcing a loss or a taker order.

3. PER-BOOK CIRCUIT BREAKER
   A rolling window of realized PnL per book is tracked. If a
   book's realized PnL over the window goes meaningfully negative,
   that book is demoted to PRESENCE-ONLY mode (minimal symmetric
   quotes, no alpha-directional quoting) for a cooldown period.
   This caps how much any single book can contribute to LPM3 and
   to the Penalty outlier metric.

4. NEVER CROSS THE BOOK FOR RISK MANAGEMENT
   Inventory reduction (skew flattening) is done via passive
   limit orders at the touch (post_only=True) or via
   close_positions(FIFO) for leveraged positions -- never via
   limit orders priced to guarantee a fill (which pays taker fees
   AND can realize an instant loss, hurting both PnL and LPM3).

5. TWO-LANE BOOK COVERAGE
   Lane 1 (every tick, all 128 books on a fast round-robin):
     minimal-size symmetric touch-join quotes, just enough
     round-trip volume to keep every book "alive" for Penalty=0.
   Lane 2 (rotating subset each tick, ~10-12 books):
     larger OFI-directed quotes for PnL growth, skipped entirely
     for circuit-broken books.

USAGE
-----
Copy to ~/.taos/agents/HybridResilientAgent.py
In miner.env:
    AGENT_CLASS=HybridResilientAgent
    AGENT_MODULE=HybridResilientAgent
"""

from __future__ import annotations

import os
import sys
import time
import logging
import bittensor as bt
from collections import defaultdict, deque
from typing import Optional, Tuple

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _sn79_compat import CompatFinanceAgentResponse, is_trade_notice, log_agent_tick, unwrap_response

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# TUNABLE PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────

GRACE_PERIOD_SECONDS      = 620     # don't trade before this (sim seconds)

# --- Lane 1: presence coverage (Penalty -> 0) ---
PRESENCE_GROUPS           = 16      # all 128 books cycle through 16 buckets
PRESENCE_QTY              = 0.25    # MIN_QUANTITY -- smallest legal order
PRESENCE_MIN_SPREAD_TICKS = 1       # almost any book qualifies for presence
PRESENCE_MAX_PER_TICK     = 12      # cap presence orders placed per tick

# --- Lane 2: alpha quoting (PnL / Kappa growth) ---
ALPHA_ROTATION_GROUPS     = 12
ALPHA_BOOKS_PER_TICK      = 8
ALPHA_QTY                 = 0.35
MIN_SPREAD_TICKS          = 4
MAX_SPREAD_RATIO          = 0.0025
MAX_MAKER_FEE             = 0.0014
OFI_WINDOW_TICKS          = 4
MIN_DIRECTIONAL_EDGE      = 0.15
MAX_TAPE_IMBALANCE        = 0.75

# --- Completion / round-trip rules ---
MIN_RT_EDGE_TICKS         = 2       # required edge vs VWAP cost basis
COMPLETION_STUCK_TICKS    = 80      # escape-valve: unwind passively after this
MIN_QUANTITY              = 0.25

# --- Inventory / risk ---
MAX_INVENTORY_SKEW        = 0.015   # hard cap fraction of capital
SOFT_SKEW_THRESHOLD       = 0.006   # bias quote side before hitting hard cap
REPAY_FIFO_EACH_TICK      = 1

# --- Circuit breaker ---
PNL_WINDOW_TICKS          = 300     # rolling window for per-book realized PnL
PNL_LOSS_THRESHOLD        = -0.002  # fraction of miner_wealth; trip breaker
BREAKER_COOLDOWN_TICKS    = 600     # ticks before re-enabling alpha on a book

# --- Stale order management ---
STALE_AGE_SECONDS         = 8
STALE_TICKS_OUTSIDE       = 1
MAX_CANCEL_PER_TICK       = 14

# --- Instruction budget ---
MAX_TOTAL_INSTRUCTIONS    = 30
MAX_PER_BOOK              = 4


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

class PositionLedger:
    """
    Tracks net position and VWAP cost basis for one book, updated from
    TradeEvent notices (both maker and taker fills, both directions).

    net_qty > 0  => net long BASE (bought more than sold)
    net_qty < 0  => net short BASE (sold more than bought)
    avg_cost     => VWAP price of the current net position (only meaningful
                    while net_qty != 0)
    """

    def __init__(self, net_qty: float = 0.0, avg_cost: float = 0.0, realized_pnl: float = 0.0):
        self.net_qty = net_qty
        self.avg_cost = avg_cost
        self.realized_pnl = realized_pnl

    def apply_fill(self, side: str, price: float, qty: float):
        """
        side: "BUY" or "SELL" from OUR perspective for this fill.
        Updates net position / VWAP and realizes PnL on any reduction.
        """
        if qty <= 0 or price <= 0:
            return

        signed_qty = qty if side == "BUY" else -qty

        if self.net_qty == 0 or (self.net_qty > 0) == (signed_qty > 0):
            # Same-direction fill (or starting from flat): extend position,
            # update VWAP cost basis.
            new_qty = self.net_qty + signed_qty
            if new_qty != 0:
                self.avg_cost = (
                    (self.avg_cost * abs(self.net_qty) + price * abs(signed_qty))
                    / abs(new_qty)
                )
            self.net_qty = new_qty
        else:
            # Opposite-direction fill: this reduces (or flips) the position.
            # The portion up to min(|net_qty|, |signed_qty|) realizes PnL
            # against avg_cost.
            closing_qty = min(abs(self.net_qty), abs(signed_qty))
            if self.net_qty > 0:
                # We were long, now selling: profit = (sell_price - cost) * qty
                pnl = (price - self.avg_cost) * closing_qty
            else:
                # We were short, now buying: profit = (cost - buy_price) * qty
                pnl = (self.avg_cost - price) * closing_qty
            self.realized_pnl += pnl

            new_qty = self.net_qty + signed_qty
            if new_qty == 0:
                self.net_qty = 0.0
                self.avg_cost = 0.0
            elif (new_qty > 0) != (self.net_qty > 0):
                # Flipped sign: remaining quantity establishes a fresh
                # position at this fill's price.
                self.net_qty = new_qty
                self.avg_cost = price
            else:
                # Reduced but same sign (shouldn't normally happen given the
                # opposite-direction branch, but guard anyway).
                self.net_qty = new_qty


class BookSlot:
    """Per-book bookkeeping for circuit breaker + cooldown."""

    def __init__(
        self,
        pnl_history: deque | None = None,
        breaker_until_tick: int = 0,
        last_realized_pnl: float = 0.0,
    ):
        self.pnl_history = pnl_history if pnl_history is not None else deque(maxlen=PNL_WINDOW_TICKS)
        self.breaker_until_tick = breaker_until_tick
        self.last_realized_pnl = last_realized_pnl


# ─────────────────────────────────────────────────────────────────────────────
# GENERIC HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def tick_size(config) -> float:
    try:
        return round(10 ** -config.priceDecimals, config.priceDecimals)
    except Exception:
        return 0.01


def round_price(price: float, config) -> float:
    return round(price, getattr(config, 'priceDecimals', 2))


def round_qty(qty: float, config) -> float:
    return round(qty, getattr(config, 'volumeDecimals', 4))


def best_bid_ask(book) -> Tuple[Optional[float], Optional[float]]:
    try:
        bids = book.bids
        asks = book.asks
        bid = bids[0].price if bids else None
        ask = asks[0].price if asks else None
        return bid, ask
    except Exception:
        return None, None


def compute_ofi(book) -> float:
    """
    Order Flow Imbalance from the L3 event tape, in [-1, +1].
    Positive = net buy pressure, negative = net sell pressure.
    """
    buy_vol = sell_vol = 0.0
    new_bid = new_ask = 0.0
    cancel_bid = cancel_ask = 0.0
    try:
        best_bid = book.bids[0].price if book.bids else 0.0
        best_ask = book.asks[0].price if book.asks else float('inf')
        for ev in book.events:
            y = getattr(ev, 'y', None)
            if y is None:
                y = type(ev).__name__[:1].lower()
            if y == 't':
                q = getattr(ev, 'quantity', 0.0)
                s = getattr(ev, 'side', 0)
                (buy_vol if s == 0 else sell_vol)
                if s == 0:
                    buy_vol += q
                else:
                    sell_vol += q
            elif y == 'o':
                q = getattr(ev, 'quantity', 0.0)
                s = getattr(ev, 'side', 0)
                if s == 0:
                    new_bid += q
                else:
                    new_ask += q
            elif y == 'c':
                q = getattr(ev, 'quantity', 0.0)
                p = getattr(ev, 'price', 0.0)
                if p <= best_bid:
                    cancel_bid += q
                else:
                    cancel_ask += q
    except Exception:
        pass

    buy_pressure  = buy_vol + new_bid - cancel_ask
    sell_pressure = sell_vol + new_ask - cancel_bid
    total = buy_pressure + sell_pressure
    if total < 1e-9:
        return 0.0
    return (buy_pressure - sell_pressure) / total


def compute_tape_imbalance(book) -> float:
    try:
        buy_qty = sell_qty = 0.0
        for ev in book.events:
            y = getattr(ev, 'y', None) or getattr(ev, 'type', None)
            if str(y).lower() in ('t', 'tradeinfo'):
                q = getattr(ev, 'quantity', 0.0)
                s = getattr(ev, 'side', 0)
                if s == 0:
                    buy_qty += q
                else:
                    sell_qty += q
        total = buy_qty + sell_qty
        if total < 1e-9:
            return 0.0
        return (buy_qty - sell_qty) / total
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# INSTRUCTION BUDGET
# ─────────────────────────────────────────────────────────────────────────────

class Budget:
    def __init__(self, total=MAX_TOTAL_INSTRUCTIONS, per_book=MAX_PER_BOOK):
        self.total_cap = total
        self.per_book_cap = per_book
        self._total = 0
        self._book = defaultdict(int)

    def ok(self, book_id: int, n: int = 1) -> bool:
        return (self._total + n <= self.total_cap and
                self._book[book_id] + n <= self.per_book_cap)

    def use(self, book_id: int, n: int = 1):
        self._total += n
        self._book[book_id] += n


# ─────────────────────────────────────────────────────────────────────────────
# MAIN AGENT
# ─────────────────────────────────────────────────────────────────────────────

class HybridResilientAgent:
    """
    Two-lane SN-79 agent:
      Lane 1 -- minimal symmetric presence on ALL 128 books (Penalty -> 0)
      Lane 2 -- OFI-directed alpha quoting on a rotating subset (Kappa/PnL up)

    Round trips only complete at a profit vs VWAP cost basis (LPM3 -> 0),
    inventory reduction is always passive (no taker fees, no forced losses),
    and any book with deteriorating realized PnL is temporarily demoted to
    presence-only mode (circuit breaker).
    """

    def __init__(self, uid: int, config=None, log_dir=None, **kwargs):
        self.uid = uid
        self.config = config
        self.log_dir = log_dir

        self.accounts  = {}
        self.events    = []
        self.timestamp = 0

        self._tick               = 0
        self._presence_bucket    = 0
        self._alpha_bucket       = 0
        self._cancel_done        = False

        self._ledger:  dict[int, PositionLedger] = defaultdict(PositionLedger)
        self._slots:   dict[int, BookSlot]       = defaultdict(BookSlot)
        self._ofi_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=OFI_WINDOW_TICKS))
        self._completion_attempts: dict[int, int] = defaultdict(int)

        logger.info(f"HybridResilientAgent initialized for UID {uid}")

    def process(self, notification):
        notification.acknowledged = True
        return notification

    # ─────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────

    def handle(self, state) -> object:
        t0 = time.time()
        try:
            self.update(state)
            response = self.respond(state)
        except Exception as e:
            bt.logging.error(f"HybridResilientAgent error: {e}")
            response = self._make_response()
        elapsed = time.time() - t0
        if elapsed > 2.0:
            bt.logging.warning(f"Slow tick: {elapsed:.2f}s (timeout ~3s)")
        inner = unwrap_response(response)
        log_agent_tick(self.uid, self.events, inner)
        return inner

    def update(self, state):
        self.config    = state.config
        self.timestamp = state.timestamp
        uid = self.uid

        try:
            self.accounts = state.accounts.get(uid, {})
        except Exception:
            self.accounts = {}

        try:
            self.events = state.notices.get(uid, [])
        except Exception:
            self.events = []

        self._tick += 1
        self._process_notices()

    def respond(self, state) -> object:
        response = self._make_response()

        if self.timestamp < GRACE_PERIOD_SECONDS * 1_000_000_000:
            return response

        config = self.config
        tick   = tick_size(config)

        # Phase 0: startup cancel-all
        if not self._cancel_done:
            self._startup_cancel(response)
            if self._count_open_orders() > 0:
                return response
            self._cancel_done = True

        budget = Budget()

        # Refresh OFI history for visible books (cheap, used by Lane 2)
        if state.books:
            for book_id, book in state.books.items():
                self._ofi_history[book_id].append(compute_ofi(book))

        # Phase 1: loan repayment (FIFO, passive)
        loans_repaid = 0
        for book_id, account in self.accounts.items():
            if loans_repaid >= REPAY_FIFO_EACH_TICK:
                break
            if not budget.ok(book_id):
                continue
            if getattr(account, 'loans', None):
                try:
                    response.close_positions(book_id=book_id, settlement_option='FIFO')
                    budget.use(book_id)
                    loans_repaid += 1
                except Exception:
                    pass

        # Phase 2: cancel stale resting orders
        self._cancel_stale(state, response, budget, tick)

        # Phase 3: completion legs (no-loss, VWAP-based)
        self._place_completions(state, response, budget, tick)

        # Phase 4: hard-cap inventory flatten (passive only)
        self._flatten_skew(state, response, budget, tick)

        # Phase 5: Lane 1 -- presence coverage on all 128 books
        self._place_presence_quotes(state, response, budget, tick)

        # Phase 6: Lane 2 -- alpha-directed quoting on rotating subset
        self._place_alpha_quotes(state, response, budget, tick)

        self._presence_bucket = (self._presence_bucket + 1) % PRESENCE_GROUPS
        self._alpha_bucket    = (self._alpha_bucket + 1) % ALPHA_ROTATION_GROUPS

        return response

    # ─────────────────────────────────────────────────────────────────────
    # NOTICE PROCESSING -> POSITION LEDGER
    # ─────────────────────────────────────────────────────────────────────

    def _process_notices(self):
        for notice in self.events:
            if is_trade_notice(notice):
                self._on_trade(notice)
            else:
                t = str(getattr(notice, 'type', '') or type(notice).__name__).lower()
                if ('placement' in t or 'limit' in t) and not getattr(notice, 'success', True):
                    book_id = getattr(notice, 'bookId', None) or getattr(notice, 'book_id', None)
                    msg = str(getattr(notice, 'message', '')).lower()
                    if book_id is not None and 'loan' in msg:
                        # Don't add alpha exposure on a book that's hitting loan
                        # limits; presence lane still keeps it covered.
                        self._slots[book_id].breaker_until_tick = max(
                            self._slots[book_id].breaker_until_tick,
                            self._tick + BREAKER_COOLDOWN_TICKS // 2,
                        )

    def _on_trade(self, notice):
        """
        Update the per-book position ledger for OUR fills (maker or taker).
        TradeEvent.side is the TAKER's side (0 = buy aggressor, 1 = sell
        aggressor). We determine our own side based on whether we were
        maker or taker.
        """
        try:
            book_id = getattr(notice, 'bookId', None) or getattr(notice, 'book_id', None)
            price   = getattr(notice, 'price', 0.0)
            qty     = getattr(notice, 'quantity', 0.0)
            taker_side = getattr(notice, 'side', None)

            maker_id = (getattr(notice, 'makerAgentId', None)
                        or getattr(notice, 'maker_agent_id', None))
            taker_id = (getattr(notice, 'takerAgentId', None)
                        or getattr(notice, 'taker_agent_id', None))

            if book_id is None or price <= 0 or qty <= 0:
                return

            taker_buy = taker_side == 0 or str(taker_side).lower() in ('0', 'buy')

            our_side = None
            if maker_id == self.uid:
                # We were the resting (maker) order. If taker bought, the
                # maker side was the ASK -> we SOLD. If taker sold, maker
                # side was the BID -> we BOUGHT.
                our_side = "SELL" if taker_buy else "BUY"
            elif taker_id == self.uid:
                # We were the aggressor.
                our_side = "BUY" if taker_buy else "SELL"
            else:
                return  # not our trade

            ledger = self._ledger[book_id]
            pre_pnl = ledger.realized_pnl
            ledger.apply_fill(our_side, price, qty)
            delta = ledger.realized_pnl - pre_pnl
            if delta != 0.0:
                self._slots[book_id].pnl_history.append(delta)

        except Exception as e:
            logger.debug(f"_on_trade error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 0: STARTUP CANCEL
    # ─────────────────────────────────────────────────────────────────────

    def _startup_cancel(self, response):
        cancelled = 0
        for book_id, account in self.accounts.items():
            try:
                ids = [o.id for o in account.orders]
                if ids:
                    response.cancel_orders(book_id=book_id, order_ids=ids)
                    cancelled += len(ids)
                    if cancelled >= 28:
                        break
            except Exception:
                pass
        if cancelled:
            logger.info(f"Startup: cancelled {cancelled} orders")

    def _count_open_orders(self) -> int:
        total = 0
        for account in self.accounts.values():
            try:
                total += len(account.orders)
            except Exception:
                pass
        return total

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 2: STALE ORDER CANCELLATION
    # ─────────────────────────────────────────────────────────────────────

    def _cancel_stale(self, state, response, budget: Budget, tick: float):
        cancelled_total = 0
        for book_id, account in self.accounts.items():
            if cancelled_total >= MAX_CANCEL_PER_TICK:
                break
            if not budget.ok(book_id):
                continue
            try:
                orders = account.orders
                if not orders:
                    continue
                book = state.books.get(book_id)
                if book is None:
                    continue
                bid, ask = best_bid_ask(book)
                if bid is None or ask is None:
                    continue

                stale_ids = []
                for order in orders:
                    price = order.price
                    side  = order.side  # 0 = bid, 1 = ask
                    age_s = (self.timestamp - order.timestamp) / 1_000_000_000

                    is_stale = False
                    if side == 0:
                        is_stale = price < bid - STALE_TICKS_OUTSIDE * tick
                    else:
                        is_stale = price > ask + STALE_TICKS_OUTSIDE * tick
                    if not is_stale and age_s > STALE_AGE_SECONDS:
                        is_stale = True

                    if is_stale:
                        stale_ids.append(order.id)

                if stale_ids:
                    response.cancel_orders(book_id=book_id, order_ids=stale_ids)
                    budget.use(book_id)
                    cancelled_total += len(stale_ids)
            except Exception:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 3: COMPLETION LEGS (no-loss, VWAP-based)
    # ─────────────────────────────────────────────────────────────────────

    def _place_completions(self, state, response, budget: Budget, tick: float):
        """
        For any book with a non-zero net position, try to flatten it at a
        price that gives >= MIN_RT_EDGE_TICKS profit vs the VWAP cost
        basis. If the market hasn't moved enough, hold (do nothing) --
        unless the position has been stuck for COMPLETION_STUCK_TICKS,
        in which case unwind passively at the touch (not crossing).
        """
        config = self.config

        for book_id, ledger in list(self._ledger.items()):
            if abs(ledger.net_qty) < MIN_QUANTITY * 0.5:
                self._completion_attempts[book_id] = 0
                continue
            if not budget.ok(book_id):
                continue

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if book is None or account is None:
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue

            qty = round_qty(min(abs(ledger.net_qty), max(abs(ledger.net_qty), MIN_QUANTITY)), config)
            qty = round_qty(max(qty, MIN_QUANTITY), config)

            stuck = self._completion_attempts[book_id] >= COMPLETION_STUCK_TICKS

            try:
                if ledger.net_qty > 0:
                    # Net long -> need to SELL to close. Require sell price
                    # >= avg_cost + edge.
                    required_min = ledger.avg_cost + MIN_RT_EDGE_TICKS * tick
                    if ask >= required_min - 1e-12:
                        price = round_price(max(ask, required_min), config)
                    elif stuck:
                        price = round_price(ask, config)  # passive unwind
                    else:
                        self._completion_attempts[book_id] += 1
                        continue

                    sell_qty = min(qty, account.base_balance.free)
                    sell_qty = round_qty(sell_qty, config)
                    if sell_qty < MIN_QUANTITY:
                        continue

                    response.limit_order(
                        book_id=book_id, direction='SELL',
                        quantity=sell_qty, price=price,
                        post_only=True, time_in_force='GTC')
                    budget.use(book_id)
                    self._completion_attempts[book_id] = 0
                    logger.debug(f"COMPLETE SELL {sell_qty}@{price} BOOK {book_id} "
                                  f"(cost={ledger.avg_cost:.4f})")

                else:
                    # Net short -> need to BUY to close. Require buy price
                    # <= avg_cost - edge.
                    required_max = ledger.avg_cost - MIN_RT_EDGE_TICKS * tick
                    if bid <= required_max + 1e-12:
                        price = round_price(min(bid, required_max), config)
                    elif stuck:
                        price = round_price(bid, config)
                    else:
                        self._completion_attempts[book_id] += 1
                        continue

                    needed_quote = qty * price
                    buy_qty = qty
                    if account.quote_balance.free < needed_quote:
                        affordable = account.quote_balance.free / max(price, 1e-9)
                        buy_qty = round_qty(min(buy_qty, affordable), config)
                    if buy_qty < MIN_QUANTITY:
                        continue

                    response.limit_order(
                        book_id=book_id, direction='BUY',
                        quantity=buy_qty, price=price,
                        post_only=True, time_in_force='GTC')
                    budget.use(book_id)
                    self._completion_attempts[book_id] = 0
                    logger.debug(f"COMPLETE BUY {buy_qty}@{price} BOOK {book_id} "
                                  f"(cost={ledger.avg_cost:.4f})")

            except Exception as e:
                logger.debug(f"Completion error book {book_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 4: HARD-CAP INVENTORY FLATTEN (passive only, last resort)
    # ─────────────────────────────────────────────────────────────────────

    def _flatten_skew(self, state, response, budget: Budget, tick: float):
        """
        If a book's inventory skew exceeds the hard cap, post a passive
        reducing order at the touch. This is a slower, safety-net version
        of Phase 3 that triggers on overall account skew (base vs quote
        value) rather than the per-book ledger, catching any drift the
        ledger might have missed (e.g. before this agent's first run).
        Never crosses the spread, never uses market orders.
        """
        config = self.config

        for book_id, account in self.accounts.items():
            if not budget.ok(book_id):
                continue
            # Skip books already being completed this tick via Phase 3
            ledger = self._ledger.get(book_id)
            if ledger and abs(ledger.net_qty) >= MIN_QUANTITY * 0.5:
                continue

            try:
                total_base  = account.base_balance.total
                total_quote = account.quote_balance.total
                book = state.books.get(book_id)
                if book is None:
                    continue
                bid, ask = best_bid_ask(book)
                if bid is None or ask is None:
                    continue
                mid = (bid + ask) / 2

                base_in_quote = total_base * mid
                total_value   = base_in_quote + total_quote
                if total_value < 1.0:
                    continue

                skew = (base_in_quote - total_quote) / total_value
                if abs(skew) < MAX_INVENTORY_SKEW:
                    continue

                if skew > MAX_INVENTORY_SKEW:
                    qty = round_qty(max(account.base_balance.free * 0.25, MIN_QUANTITY), config)
                    if qty < MIN_QUANTITY:
                        continue
                    price = round_price(ask, config)
                    response.limit_order(
                        book_id=book_id, direction='SELL',
                        quantity=qty, price=price, post_only=True)
                    budget.use(book_id)
                    logger.debug(f"HARDCAP FLATTEN SELL {qty}@{price} BOOK {book_id} (skew={skew:.4f})")
                else:
                    qty = round_qty(max(account.quote_balance.free * 0.25 / max(ask,1e-9), MIN_QUANTITY), config)
                    if qty < MIN_QUANTITY:
                        continue
                    price = round_price(bid, config)
                    response.limit_order(
                        book_id=book_id, direction='BUY',
                        quantity=qty, price=price, post_only=True)
                    budget.use(book_id)
                    logger.debug(f"HARDCAP FLATTEN BUY {qty}@{price} BOOK {book_id} (skew={skew:.4f})")

            except Exception:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 5: LANE 1 -- PRESENCE COVERAGE (Penalty -> 0)
    # ─────────────────────────────────────────────────────────────────────

    def _place_presence_quotes(self, state, response, budget: Budget, tick: float):
        """
        Post a single small symmetric (alternating side) post_only quote
        on each book in this tick's presence bucket. Goal is simply to
        keep round-trip volume flowing on every book so none becomes a
        Penalty outlier -- not to make directional bets.

        Skips books that already have an open completion (Phase 3) or an
        active alpha quote this tick to respect the instruction budget.
        """
        config = self.config
        book_count = getattr(config, 'book_count', 128)
        bucket = self._presence_bucket

        placed = 0
        for book_id in range(book_count):
            if placed >= PRESENCE_MAX_PER_TICK:
                break
            if book_id % PRESENCE_GROUPS != bucket:
                continue
            if not budget.ok(book_id):
                continue

            ledger = self._ledger.get(book_id)
            if ledger and abs(ledger.net_qty) >= MIN_QUANTITY * 0.5:
                continue  # being completed this tick

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if book is None or account is None:
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue

            spread_ticks = (ask - bid) / tick if tick > 0 else 0
            if spread_ticks < PRESENCE_MIN_SPREAD_TICKS:
                continue

            # Alternate side by book_id and tick for natural round-trips
            # over time without persistent directional bias.
            side = "BUY" if (book_id + self._tick) % 2 == 0 else "SELL"

            qty = round_qty(max(PRESENCE_QTY, MIN_QUANTITY), config)

            try:
                if side == "SELL":
                    if account.base_balance.free < qty:
                        side = "BUY"
                if side == "BUY":
                    price = round_price(bid, config)
                    needed = qty * price
                    if account.quote_balance.free < needed:
                        continue
                else:
                    price = round_price(ask, config)

                # Avoid duplicate price
                existing = {o.price for o in account.orders}
                if price in existing:
                    continue

                response.limit_order(
                    book_id=book_id, direction=side,
                    quantity=qty, price=price,
                    post_only=True, time_in_force='GTC')
                budget.use(book_id)
                placed += 1
            except Exception as e:
                logger.debug(f"Presence place error book {book_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 6: LANE 2 -- ALPHA QUOTING (Kappa / PnL growth)
    # ─────────────────────────────────────────────────────────────────────

    def _place_alpha_quotes(self, state, response, budget: Budget, tick: float):
        """
        OFI-directed maker quoting on a rotating subset of books, larger
        size than presence quotes. Skips books currently circuit-broken
        (recent negative realized PnL) or in an active completion.
        """
        config = self.config
        book_count = getattr(config, 'book_count', 128)
        bucket = self._alpha_bucket

        candidates = []
        for book_id in range(book_count):
            if book_id % ALPHA_ROTATION_GROUPS != bucket:
                continue
            if not budget.ok(book_id):
                continue

            ledger = self._ledger.get(book_id)
            if ledger and abs(ledger.net_qty) >= MIN_QUANTITY * 0.5:
                continue  # being completed; don't add alpha exposure

            slot = self._slots[book_id]
            if slot.breaker_until_tick > self._tick:
                continue  # circuit-broken, presence-only

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if book is None or account is None:
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue

            spread = ask - bid
            spread_ticks = spread / tick if tick > 0 else 0
            if spread_ticks < MIN_SPREAD_TICKS:
                continue

            mid = (bid + ask) / 2
            if mid <= 0 or (spread / mid) > MAX_SPREAD_RATIO:
                continue

            try:
                if account.fees.maker_fee_rate > MAX_MAKER_FEE:
                    continue
            except Exception:
                pass

            imbalance = compute_tape_imbalance(book)
            if abs(imbalance) > MAX_TAPE_IMBALANCE:
                continue

            history = self._ofi_history.get(book_id)
            if not history:
                continue
            weights = [2 ** i for i in range(len(history))]
            ofi_signal = sum(w * o for w, o in zip(weights, history)) / sum(weights)

            if ofi_signal > MIN_DIRECTIONAL_EDGE:
                direction = "SELL"   # buy pressure -> sit on ask
                conviction = ofi_signal
            elif ofi_signal < -MIN_DIRECTIONAL_EDGE:
                direction = "BUY"    # sell pressure -> sit on bid
                conviction = -ofi_signal
            else:
                continue  # no conviction -> skip (presence lane still covers)

            # Inventory skew override
            try:
                bv = account.base_balance.total * mid
                qv = account.quote_balance.total
                tv = bv + qv
                skew = (bv - qv) / max(tv, 1.0)
            except Exception:
                skew = 0.0

            if skew > SOFT_SKEW_THRESHOLD and direction == "BUY":
                direction = "SELL"
            elif skew < -SOFT_SKEW_THRESHOLD and direction == "SELL":
                direction = "BUY"

            candidates.append((conviction, book_id, direction, bid, ask, account))

        candidates.sort(key=lambda x: -x[0])

        placed = 0
        for conviction, book_id, direction, bid, ask, account in candidates:
            if placed >= ALPHA_BOOKS_PER_TICK:
                break
            if not budget.ok(book_id):
                continue

            qty = round_qty(max(ALPHA_QTY, MIN_QUANTITY), config)

            try:
                if direction == "SELL":
                    price = round_price(ask, config)
                    if account.base_balance.free < qty:
                        qty = round_qty(max(account.base_balance.free, MIN_QUANTITY), config)
                else:
                    price = round_price(bid, config)
                    needed = qty * price
                    if account.quote_balance.free < needed:
                        qty = round_qty(max(account.quote_balance.free / max(price, 1e-9),
                                            MIN_QUANTITY), config)

                if qty < MIN_QUANTITY:
                    continue

                existing = {o.price for o in account.orders}
                if price in existing:
                    continue

                response.limit_order(
                    book_id=book_id, direction=direction,
                    quantity=qty, price=price,
                    post_only=True, time_in_force='GTC')
                budget.use(book_id)
                placed += 1
                logger.debug(f"ALPHA {direction} {qty}@{price} BOOK {book_id} (ofi={conviction:.3f})")

            except Exception as e:
                logger.debug(f"Alpha place error book {book_id}: {e}")

        # Update circuit breakers based on rolling realized PnL
        self._update_circuit_breakers()

    # ─────────────────────────────────────────────────────────────────────
    # CIRCUIT BREAKER MAINTENANCE
    # ─────────────────────────────────────────────────────────────────────

    def _update_circuit_breakers(self):
        config = self.config
        wealth = float(getattr(config, 'miner_wealth', 50000) or 50000)
        threshold_abs = PNL_LOSS_THRESHOLD * wealth

        for book_id, slot in self._slots.items():
            if not slot.pnl_history:
                continue
            window_sum = sum(slot.pnl_history)
            if window_sum < threshold_abs and slot.breaker_until_tick <= self._tick:
                slot.breaker_until_tick = self._tick + BREAKER_COOLDOWN_TICKS
                logger.info(f"Circuit breaker tripped on book {book_id}: "
                            f"window PnL={window_sum:.4f} (threshold={threshold_abs:.4f}). "
                            f"Demoted to presence-only for {BREAKER_COOLDOWN_TICKS} ticks.")
                slot.pnl_history.clear()

    # ─────────────────────────────────────────────────────────────────────
    # RESPONSE FACTORY
    # ─────────────────────────────────────────────────────────────────────

    def _make_response(self):
        return CompatFinanceAgentResponse(agent_id=self.uid, accounts=self.accounts)
