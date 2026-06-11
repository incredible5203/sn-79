"""
AscendKappaAgent — SN-79 Competitive Miner Agent
=================================================
Target:  κ₃ ↑  |  Realized PnL ↑  |  Penalty = 0
Strategy: Adaptive Touch-Join Market Maker with Microstructure Filtering

CORE IDEA
---------
The scoring formula is:
    TradingScore = 0.79 × KappaScore + 0.21 × PnLScore

Kappa-3 rewards: (mean_return - 0) / LPM3^(1/3)
  → maximize μ (average round-trip profit)
  → crush LPM3 (avoid any large realized loss → cube makes it explode)

This agent implements three interlocked strategies:
  1. TOUCH-JOIN MAKER:  High fill-rate at bid/ask (not inside) for fast RT volume
  2. INSIDE COMPLETER:  After every maker fill, complete round-trip inside spread
  3. PANIC GUARD:       Aggressive stale-order management + inventory skew control

Penalty = 0 is achieved by rotating across ALL 128 books in groups.

Usage
-----
Copy to ~/.taos/agents/AscendKappaAgent.py
In miner.env:
    AGENT_CLASS=AscendKappaAgent
    AGENT_MODULE=AscendKappaAgent
"""

from __future__ import annotations

import math
import os
import sys
import time
import random
import logging
from collections import defaultdict, deque
from typing import Optional

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _sn79_compat import CompatFinanceAgentResponse, is_trade_notice, unwrap_response

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# TUNABLE PARAMETERS  (edit these to tune without changing logic)
# ─────────────────────────────────────────────────────────────────────────────

# Book selection
MAX_BOOKS_PER_TICK        = 11     # books to actively quote each tick
ROTATION_GROUPS           = 12     # rotate across this many bucket groups
MIN_SPREAD_TICKS          = 3      # skip if spread < this many ticks
MAX_SPREAD_RATIO          = 0.0025 # skip if spread/mid > this ratio (toxic wide)
MIN_RT_EDGE_TICKS         = 2      # min edge ticks for a completion leg
MAX_TAPE_IMBALANCE        = 0.70   # skip book if |buy_qty - sell_qty| / total > this

# Order sizing
BASE_QUANTITY             = 0.32   # BASE units per order (must be >= 0.25)
MIN_QUANTITY              = 0.25   # hard floor from sim config
QUANTITY_SCALE            = 1.0    # scale factor; set < 1 for smaller size
MAX_INVENTORY_SKEW        = 0.015  # flatten if |skew| exceeds this fraction of capital
SOFT_SKEW_THRESHOLD       = 0.006  # prefer reducing side when above this

# Instruction budget
MAX_INSTRUCTIONS_PER_BOOK = 4      # validator hard cap is 5; keep headroom
MAX_TOTAL_INSTRUCTIONS    = 28     # total per tick
MAX_CANCEL_PER_TICK       = 12     # cap stale-cancel budget

# Fee gate
MAX_MAKER_FEE             = 0.0015 # skip book if maker fee rate exceeds this

# Stale-order detection
STALE_TICKS_OUTSIDE       = 1      # cancel if order price is this many ticks from touch
STALE_AGE_SECONDS         = 8      # cancel if order is older than this (sim seconds)

# Loan management
REPAY_FIFO_EACH_TICK      = 1      # close this many loans per tick (FIFO)

# Bootstrap: quote on cold books even when no fill signal
COLD_BOOK_BOOTSTRAP       = True   # always post one maker quote per rotated book if no signal

# Grace period safety
GRACE_PERIOD_SECONDS      = 620    # do not trade until sim time > this (ns: ×1e9)


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

class CompletionHint:
    """Queued completion leg after a maker fill."""

    def __init__(
        self,
        book_id: int,
        side: str,
        fill_price: float,
        fill_qty: float,
        min_edge_price: float,
        ts_queued: int,
        attempts: int = 0,
    ):
        self.book_id = book_id
        self.side = side
        self.fill_price = fill_price
        self.fill_qty = fill_qty
        self.min_edge_price = min_edge_price
        self.ts_queued = ts_queued
        self.attempts = attempts


class BookStats:
    """Rolling per-book statistics maintained by the agent."""

    def __init__(
        self,
        realized_pnl: float = 0.0,
        total_rt_count: int = 0,
        losing_rt_count: int = 0,
        last_mid: float = 0.0,
        last_kappa_sign: int = 0,
        skip_until_tick: int = 0,
    ):
        self.realized_pnl = realized_pnl
        self.total_rt_count = total_rt_count
        self.losing_rt_count = losing_rt_count
        self.last_mid = last_mid
        self.last_kappa_sign = last_kappa_sign
        self.skip_until_tick = skip_until_tick


# ─────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS (no class dependencies, testable in isolation)
# ─────────────────────────────────────────────────────────────────────────────

def best_bid_ask(book) -> tuple[Optional[float], Optional[float]]:
    """Extract best bid and ask from a Book object."""
    try:
        bids = book.bids
        asks = book.asks
        bid = bids[0].price if bids else None
        ask = asks[0].price if asks else None
        return bid, ask
    except Exception:
        return None, None


def tick_size(config) -> float:
    try:
        return round(10 ** -config.priceDecimals, config.priceDecimals)
    except Exception:
        return 0.01


def round_price(price: float, config) -> float:
    decimals = getattr(config, 'priceDecimals', 2)
    return round(price, decimals)


def round_qty(qty: float, config) -> float:
    decimals = getattr(config, 'volumeDecimals', 4)
    return round(qty, decimals)


def compute_spread_ticks(bid: float, ask: float, tick: float) -> float:
    return round((ask - bid) / tick, 1) if tick > 0 else 0


def compute_tape_imbalance(book) -> float:
    """
    Returns signed imbalance in [-1, +1].
    +1 = all buys, -1 = all sells.
    """
    try:
        buy_qty = sell_qty = 0.0
        for ev in book.events:
            t = getattr(ev, 'y', None) or getattr(ev, 'type', None)
            if t == 't' or str(t).lower() in ('t', 'tradeinfo'):
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


def compute_microprice(book) -> Optional[float]:
    """Volume-weighted mid (microprice) from best bid/ask."""
    try:
        bids = book.bids
        asks = book.asks
        if not bids or not asks:
            return None
        bp, bq = bids[0].price, bids[0].quantity
        ap, aq = asks[0].price, asks[0].quantity
        if bq + aq < 1e-9:
            return None
        return (bp * aq + ap * bq) / (bq + aq)
    except Exception:
        return None


def inside_price(best_bid: float, best_ask: float, side: str, tick: float) -> float:
    """
    Touch-join price: at the best bid or ask (aggressive maker position).
    For SELL: join ask queue at best_ask.
    For BUY: join bid queue at best_bid.
    """
    if side == "SELL":
        return best_ask
    else:
        return best_bid


def completion_price(fill_price: float, side: str, tick: float,
                     edge_ticks: int, best_bid: float, best_ask: float) -> float:
    """
    Price for the completion (opposite) leg after a maker fill.
    Must be inside spread and give >= edge_ticks profit over fill price.
    BUY completion after SELL fill: buy back at fill_price - edge_ticks*tick
    SELL completion after BUY fill: sell at fill_price + edge_ticks*tick
    """
    if side == "BUY":
        # We previously sold at fill_price; now buy back
        target = fill_price - edge_ticks * tick
        # Don't cross spread
        target = max(target, best_bid)
        return target
    else:
        # We previously bought at fill_price; now sell
        target = fill_price + edge_ticks * tick
        target = min(target, best_ask)
        return target


def fill_score(book, account, spread_ticks: float, tape_imbalance: float) -> float:
    """
    Composite score for prioritizing which books to quote.
    Higher = better opportunity.
    """
    # Spread attractiveness (wider = better for maker, up to a point)
    spread_score = min(spread_ticks / 10.0, 1.0)

    # Balanced tape is better for quoting
    balance_score = 1.0 - abs(tape_imbalance)

    # Free capital weight (more free capital = can do more)
    try:
        free = account.quote_balance.free if account else 0.0
        total = account.quote_balance.total if account else 1.0
        capital_score = free / max(total, 1.0)
    except Exception:
        capital_score = 0.5

    # Favor books with fewer resting orders (less competition in queue)
    try:
        n_orders = len(account.orders) if account else 0
        order_load_penalty = min(n_orders / 10.0, 1.0)
    except Exception:
        order_load_penalty = 0.0

    return 0.4 * spread_score + 0.3 * balance_score + 0.2 * capital_score - 0.1 * order_load_penalty


# ─────────────────────────────────────────────────────────────────────────────
# INSTRUCTION BUDGET
# ─────────────────────────────────────────────────────────────────────────────

class InstructionBudget:
    def __init__(self, total: int = MAX_TOTAL_INSTRUCTIONS,
                 per_book: int = MAX_INSTRUCTIONS_PER_BOOK):
        self._total_cap  = total
        self._per_book   = per_book
        self._total_used = 0
        self._book_used: dict[int, int] = defaultdict(int)

    def can_use(self, book_id: int, n: int = 1) -> bool:
        return (self._total_used + n <= self._total_cap and
                self._book_used[book_id] + n <= self._per_book)

    def use(self, book_id: int, n: int = 1):
        self._total_used    += n
        self._book_used[book_id] += n

    @property
    def remaining(self) -> int:
        return self._total_cap - self._total_used

    def book_remaining(self, book_id: int) -> int:
        return self._per_book - self._book_used[book_id]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN AGENT
# ─────────────────────────────────────────────────────────────────────────────

class AscendKappaAgent:
    """
    High-score competitive agent for SN-79.

    Design priorities (in order):
    1. Never miss a tick (respond < 1s, lazy_load=1)
    2. Maximize fill rate (touch-join, not deep inside)
    3. Complete every round-trip with edge (κ₃ observations)
    4. Eliminate LPM3 spikes (inventory control, blacklist losing books)
    5. Cover all 128 books in rotation (Penalty = 0)
    """

    def __init__(self, uid: int, config=None, log_dir=None, **kwargs):
        self.uid = uid
        self.config = config
        self.log_dir = log_dir

        # State from validator
        self.accounts   = {}   # book_id → Account
        self.events     = []   # notices from last tick
        self.timestamp  = 0    # current sim ns

        # Per-book internal state
        self._completions: dict[int, list[CompletionHint]] = defaultdict(list)  # book_id → [hints]
        self._book_stats:  dict[int, BookStats]      = defaultdict(BookStats)

        # Rotation state
        self._tick_count   = 0
        self._current_bucket = 0

        # Startup state
        self._startup_done = False
        self._cancel_all_done = False

        # Performance tracking
        self._realized_pnl_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))

        logger.info(f"AscendKappaAgent initialized for UID {uid}")

    def process(self, notification):
        notification.acknowledged = True
        return notification

    # ─────────────────────────────────────────────────────────────────────────
    # BITTENSOR AGENT INTERFACE
    # ─────────────────────────────────────────────────────────────────────────

    def handle(self, state) -> object:
        """Main entry point called by the miner neuron."""
        t0 = time.time()
        try:
            self.update(state)
            response = self.respond(state)
            self.report(state, response)
        except Exception as e:
            logger.error(f"AscendKappaAgent.handle error: {e}", exc_info=True)
            response = self._make_response()
        elapsed = time.time() - t0
        if elapsed > 2.0:
            logger.warning(f"Slow tick: {elapsed:.2f}s (timeout=3s)")
        return unwrap_response(response)

    def update(self, state):
        """Ingest state, process notices, update internal tracking."""
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

        self._process_notices()
        self._tick_count += 1

    def respond(self, state) -> object:
        """Build and return FinanceAgentResponse."""
        response = self._make_response()

        # Grace period: do nothing
        if self.timestamp < GRACE_PERIOD_SECONDS * 1_000_000_000:
            return response

        config = self.config
        tick   = tick_size(config)

        # ── Phase 0: startup cancel-all ───────────────────────────────────────
        if not self._cancel_all_done:
            self._do_startup_cancel(state, response)
            if self._count_all_open_orders() > 0:
                return response  # still cancelling
            self._cancel_all_done = True

        budget = InstructionBudget()

        # ── Phase 1: repay loans (FIFO, one per tick) ─────────────────────────
        loans_repaid = 0
        for book_id, account in self.accounts.items():
            if loans_repaid >= REPAY_FIFO_EACH_TICK:
                break
            if not budget.can_use(book_id):
                continue
            if hasattr(account, 'loans') and account.loans:
                try:
                    response.close_positions(book_id=book_id, settlement_option='FIFO')
                    budget.use(book_id)
                    loans_repaid += 1
                    logger.debug(f"CLOSE POSITIONS (FIFO) ON BOOK {book_id}")
                except Exception:
                    pass

        # ── Phase 2: cancel stale orders ──────────────────────────────────────
        self._cancel_stale_orders(state, response, budget)

        # ── Phase 3: completion legs ─────────────────────────────────────────
        self._place_completion_legs(state, response, budget, tick)

        # ── Phase 4: flatten inventory if skewed ─────────────────────────────
        self._flatten_skew(state, response, budget, tick)

        # ── Phase 5: new maker quotes on rotated books ────────────────────────
        self._place_new_quotes(state, response, budget, tick)

        # advance rotation bucket
        self._current_bucket = (self._current_bucket + 1) % ROTATION_GROUPS

        return response

    def report(self, state, response):
        """Log instructions for debugging."""
        try:
            instrs = getattr(response, 'instructions', [])
            if instrs:
                lines = [f"  {i}" for i in instrs[:8]]
                logger.debug(f"T={self.timestamp} [{len(instrs)} instructions]\n" + "\n".join(lines))
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # NOTICE PROCESSING
    # ─────────────────────────────────────────────────────────────────────────

    def _process_notices(self):
        """Dispatch each notice to the appropriate handler."""
        for notice in self.events:
            if is_trade_notice(notice):
                self._on_trade(notice)
            else:
                t = getattr(notice, 'type', None) or type(notice).__name__
                t_str = str(t).lower()
                if 'placement' in t_str or 'order' in t_str:
                    self._on_order_event(notice)

    def _on_trade(self, notice):
        """
        Handle a TradeEvent (our fill).
        Queue a completion leg if we were the maker.
        """
        try:
            book_id    = getattr(notice, 'bookId', None) or getattr(notice, 'book_id', None)
            side       = getattr(notice, 'side', None)
            price      = getattr(notice, 'price', 0.0)
            qty        = getattr(notice, 'quantity', 0.0)
            maker_id   = getattr(notice, 'makerAgentId', None) or getattr(notice, 'maker_agent_id', None)

            if book_id is None or price <= 0:
                return

            # Determine if WE were the maker (passive fill = better for κ)
            was_maker = (maker_id == self.uid)
            if not was_maker:
                # We were taker — record but don't queue completion here
                # (completion for taker trades is implicit: the round-trip
                # is already started if we previously placed a limit)
                return

            # Determine completion side
            # If we were maker on ASK (sell), the taker bought → we sold → complete with BUY
            # If we were maker on BID (buy), the taker sold → we bought → complete with SELL
            # 'side' in TradeEvent is the taker's side (0=BUY aggressor, 1=SELL aggressor)
            if side == 0 or str(side).lower() in ('buy', '0'):
                # Taker was buyer, we were maker on ask → we SOLD → completion = BUY
                completion_side = "BUY"
            else:
                # Taker was seller, we were maker on bid → we BOUGHT → completion = SELL
                completion_side = "SELL"

            tick = tick_size(self.config)
            edge = MIN_RT_EDGE_TICKS

            # Edge price for completion
            if completion_side == "BUY":
                edge_price = price - edge * tick
            else:
                edge_price = price + edge * tick

            hint = CompletionHint(
                book_id        = book_id,
                side           = completion_side,
                fill_price     = price,
                fill_qty       = qty,
                min_edge_price = edge_price,
                ts_queued      = self.timestamp,
            )
            # Queue this fill as its own completion leg. Multiple fills on the
            # same book are tracked independently so none are silently dropped
            # (dropping fills causes untracked inventory drift, which the
            # skew-flatten logic then has to clean up at potential loss).
            self._completions[book_id].append(hint)
            logger.debug(f"Queued {completion_side} completion on book {book_id} "
                         f"after fill @ {price:.2f}")
        except Exception as e:
            logger.debug(f"_on_trade error: {e}")

    def _on_order_event(self, notice):
        """Track order placements (success/fail) for diagnostics."""
        try:
            success = getattr(notice, 'success', True)
            if not success:
                msg = getattr(notice, 'message', '')
                book_id = getattr(notice, 'bookId', None)
                logger.debug(f"Order rejected on book {book_id}: {msg}")
                # Blacklist this book for a few ticks if EXCEEDING_LOAN
                if book_id and 'loan' in str(msg).lower():
                    self._book_stats[book_id].skip_until_tick = self._tick_count + 10
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 0: STARTUP CANCEL
    # ─────────────────────────────────────────────────────────────────────────

    def _do_startup_cancel(self, state, response):
        """Cancel all open orders on startup to clear ghost inventory."""
        cancelled = 0
        for book_id, account in self.accounts.items():
            try:
                orders = account.orders
                if not orders:
                    continue
                ids = [o.id for o in orders]
                if ids:
                    response.cancel_orders(book_id=book_id, order_ids=ids)
                    cancelled += len(ids)
                    if cancelled >= 28:
                        break
            except Exception:
                pass
        if cancelled:
            logger.info(f"Startup: cancelled {cancelled} ghost orders")

    def _count_all_open_orders(self) -> int:
        total = 0
        for account in self.accounts.values():
            try:
                total += len(account.orders)
            except Exception:
                pass
        return total

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 2: CANCEL STALE ORDERS
    # ─────────────────────────────────────────────────────────────────────────

    def _cancel_stale_orders(self, state, response, budget: InstructionBudget):
        """
        Cancel resting orders that are stale:
        - Price is more than STALE_TICKS_OUTSIDE ticks from current touch
        - Age > STALE_AGE_SECONDS simulation seconds
        """
        tick = tick_size(self.config)
        cancelled_total = 0

        for book_id, account in self.accounts.items():
            if not budget.can_use(book_id):
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
                    try:
                        price = order.price
                        side  = order.side  # 0=bid, 1=ask
                        age_s = (self.timestamp - order.timestamp) / 1_000_000_000

                        is_stale = False
                        if side == 0:  # bid
                            is_stale = (price < bid - STALE_TICKS_OUTSIDE * tick)
                        else:           # ask
                            is_stale = (price > ask + STALE_TICKS_OUTSIDE * tick)
                        if not is_stale and age_s > STALE_AGE_SECONDS:
                            is_stale = True

                        if is_stale:
                            stale_ids.append(order.id)
                    except Exception:
                        pass

                if stale_ids and budget.can_use(book_id):
                    response.cancel_orders(book_id=book_id, order_ids=stale_ids)
                    budget.use(book_id)
                    cancelled_total += len(stale_ids)
                    logger.debug(f"Cancelled {len(stale_ids)} stale orders on book {book_id}")

            except Exception:
                continue

            if cancelled_total >= MAX_CANCEL_PER_TICK:
                break

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 3: COMPLETION LEGS
    # ─────────────────────────────────────────────────────────────────────────

    def _place_completion_legs(self, state, response, budget: InstructionBudget, tick: float):
        """
        For each queued completion hint, try to place the opposite leg inside spread.
        This closes the round-trip and locks in realized PnL.
        """
        completed_books = []

        for book_id, hints in list(self._completions.items()):
            if not hints:
                continue
            if not budget.can_use(book_id):
                continue
            hint = hints[0]  # process oldest fill first (FIFO)
            try:
                book    = state.books.get(book_id)
                account = self.accounts.get(book_id)
                if book is None or account is None:
                    continue

                bid, ask = best_bid_ask(book)
                if bid is None or ask is None:
                    continue

                config = self.config

                # Compute completion price.
                # IMPORTANT: never clamp past the profitable side of fill_price.
                # If the market hasn't moved enough to offer >= MIN_RT_EDGE_TICKS
                # of edge, DO NOT force a completion here — re-queue and wait
                # (re-quote passively instead). Forcing a completion at a price
                # that doesn't beat fill_price turns a maker fill into a
                # guaranteed flat/loss trade, which feeds LPM3 directly.
                if hint.side == "BUY":
                    # We previously SOLD at fill_price; buying back must be
                    # STRICTLY BELOW fill_price by the edge requirement.
                    required_max = hint.fill_price - MIN_RT_EDGE_TICKS * tick
                    if bid > required_max + 1e-12:
                        # Market hasn't dropped enough — hold, don't force.
                        hint.attempts += 1
                        if hint.attempts > 60:
                            # Long-stuck position: take it off at breakeven-ish
                            # via passive order at best_bid (still not crossing).
                            target_price = round_price(bid, config)
                        else:
                            continue
                    else:
                        target_price = round_price(min(bid, required_max), config)
                else:
                    # We previously BOUGHT at fill_price; selling must be
                    # STRICTLY ABOVE fill_price by the edge requirement.
                    required_min = hint.fill_price + MIN_RT_EDGE_TICKS * tick
                    if ask < required_min - 1e-12:
                        hint.attempts += 1
                        if hint.attempts > 60:
                            target_price = round_price(ask, config)
                        else:
                            continue
                    else:
                        target_price = round_price(max(ask, required_min), config)

                qty = round_qty(max(hint.fill_qty, MIN_QUANTITY), config)

                # Balance check
                if hint.side == "BUY":
                    needed_quote = qty * target_price
                    if account.quote_balance.free < needed_quote:
                        # Scale qty
                        affordable = account.quote_balance.free / target_price
                        qty = round_qty(max(min(affordable, qty), MIN_QUANTITY), config)
                        if qty < MIN_QUANTITY:
                            continue
                else:
                    if account.base_balance.free < qty:
                        qty = round_qty(max(account.base_balance.free, MIN_QUANTITY), config)
                        if qty < MIN_QUANTITY:
                            continue

                # (No further crossing-clamp here — target_price was already
                # validated against the spread and the edge requirement above.)

                if hint.side == "BUY":
                    response.limit_order(
                        book_id=book_id,
                        direction='BUY',
                        quantity=qty,
                        price=target_price,
                        post_only=True,
                        time_in_force='GTC',
                    )
                else:
                    response.limit_order(
                        book_id=book_id,
                        direction='SELL',
                        quantity=qty,
                        price=target_price,
                        post_only=True,
                        time_in_force='GTC',
                    )

                budget.use(book_id)
                completed_books.append(book_id)
                logger.debug(f"COMPLETION {hint.side} {qty}@{target_price} ON BOOK {book_id} "
                             f"(fill was @{hint.fill_price:.2f})")

            except Exception as e:
                logger.debug(f"Completion error book {book_id}: {e}")

        for b in completed_books:
            if self._completions.get(b):
                self._completions[b].pop(0)
                if not self._completions[b]:
                    del self._completions[b]

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 4: FLATTEN INVENTORY SKEW
    # ─────────────────────────────────────────────────────────────────────────

    def _flatten_skew(self, state, response, budget: InstructionBudget, tick: float):
        """
        If inventory skew is above hard threshold, aggressively flatten
        with a market order on the heaviest book.
        """
        config = self.config

        for book_id, account in self.accounts.items():
            if not budget.can_use(book_id):
                continue
            try:
                total_base  = account.base_balance.total
                total_quote = account.quote_balance.total
                book        = state.books.get(book_id)
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
                    continue  # skew is fine

                if skew > MAX_INVENTORY_SKEW:
                    # Too much BASE → reduce by selling, but stay passive:
                    # post at best_ask (maker), not bid+tick (which crosses
                    # and pays taker fee / can realize an instant loss).
                    qty = round_qty(
                        max(account.base_balance.free * 0.3, MIN_QUANTITY), config)
                    if qty < MIN_QUANTITY:
                        continue
                    price = round_price(ask, config)
                    response.limit_order(
                        book_id=book_id, direction='SELL',
                        quantity=qty, price=price, post_only=True)
                    budget.use(book_id)
                    logger.debug(f"FLATTEN SELL {qty}@{price} ON BOOK {book_id} (skew={skew:.4f})")
                elif skew < -MAX_INVENTORY_SKEW:
                    # Too much QUOTE → reduce by buying, stay passive at best_bid.
                    qty = round_qty(
                        max(account.quote_balance.free * 0.3 / ask, MIN_QUANTITY), config)
                    if qty < MIN_QUANTITY:
                        continue
                    price = round_price(bid, config)
                    response.limit_order(
                        book_id=book_id, direction='BUY',
                        quantity=qty, price=price, post_only=True)
                    budget.use(book_id)
                    logger.debug(f"FLATTEN BUY {qty}@{price} ON BOOK {book_id} (skew={skew:.4f})")

            except Exception:
                continue

    # ─────────────────────────────────────────────────────────────────────────
    # PHASE 5: NEW MAKER QUOTES
    # ─────────────────────────────────────────────────────────────────────────

    def _place_new_quotes(self, state, response, budget: InstructionBudget, tick: float):
        """
        Place touch-join maker quotes on rotated books that pass all filters.
        Touch-join = post AT best_bid or best_ask (not inside), maximizing fill rate.
        """
        config = self.config

        # Build candidate list for this tick's rotation bucket
        book_count = getattr(config, 'book_count', 128)
        # This tick's rotation: books where book_id % ROTATION_GROUPS == current_bucket
        bucket = self._current_bucket

        candidates = []
        for book_id in range(book_count):
            if book_id % ROTATION_GROUPS != bucket:
                continue
            if self._book_stats[book_id].skip_until_tick > self._tick_count:
                continue
            if self._completions.get(book_id):
                # Already placing a completion leg; don't also quote
                continue

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if book is None or account is None:
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue

            spread = ask - bid
            spread_ticks = compute_spread_ticks(bid, ask, tick)
            if spread_ticks < MIN_SPREAD_TICKS:
                continue

            mid = (bid + ask) / 2
            if mid <= 0:
                continue

            spread_ratio = spread / mid
            if spread_ratio > MAX_SPREAD_RATIO:
                continue

            # Fee filter
            try:
                maker_fee = account.fees.maker_fee_rate
                if maker_fee > MAX_MAKER_FEE:
                    continue
            except Exception:
                pass

            # Tape imbalance filter
            imbalance = compute_tape_imbalance(book)
            if abs(imbalance) > MAX_TAPE_IMBALANCE:
                continue

            # Score the book
            score = fill_score(book, account, spread_ticks, imbalance)

            # Determine preferred side from microprice / skew
            micro = compute_microprice(book)
            if micro is None:
                micro = mid

            try:
                base_val  = account.base_balance.total * mid
                quote_val = account.quote_balance.total
                total_val = base_val + quote_val
                skew = (base_val - quote_val) / max(total_val, 1.0)
            except Exception:
                skew = 0.0

            if skew > SOFT_SKEW_THRESHOLD:
                preferred_side = "SELL"
            elif skew < -SOFT_SKEW_THRESHOLD:
                preferred_side = "BUY"
            elif micro > mid + 0.5 * tick:
                preferred_side = "SELL"  # price likely to rise → sell at ask
            elif micro < mid - 0.5 * tick:
                preferred_side = "BUY"
            else:
                # Alternate by book and tick
                preferred_side = "SELL" if (book_id + self._tick_count) % 2 == 0 else "BUY"

            candidates.append((score, book_id, bid, ask, mid, preferred_side, account, imbalance))

        # Sort by score descending
        candidates.sort(key=lambda x: -x[0])

        placed = 0
        for score, book_id, bid, ask, mid, side, account, imbalance in candidates:
            if placed >= MAX_BOOKS_PER_TICK:
                break
            if not budget.can_use(book_id):
                continue

            qty = round_qty(max(BASE_QUANTITY * QUANTITY_SCALE, MIN_QUANTITY), config)

            # Touch-join: post AT the touch (best bid or best ask)
            if side == "SELL":
                price = round_price(ask, config)
                # Balance check: need base to sell
                try:
                    if account.base_balance.free < qty:
                        # Try BUY instead
                        side  = "BUY"
                        price = round_price(bid, config)
                except Exception:
                    pass
            
            if side == "BUY":
                price = round_price(bid, config)
                try:
                    needed_quote = qty * price
                    if account.quote_balance.free < needed_quote:
                        affordable = account.quote_balance.free / max(price, 1.0)
                        qty = round_qty(max(affordable, MIN_QUANTITY), config)
                        if qty < MIN_QUANTITY:
                            continue
                except Exception:
                    pass

            # Final: check existing orders — don't double-post at same price
            try:
                existing_prices = {o.price for o in account.orders}
                if price in existing_prices:
                    continue
            except Exception:
                pass

            try:
                response.limit_order(
                    book_id=book_id,
                    direction=side,
                    quantity=qty,
                    price=price,
                    post_only=True,  # stay maker only — no taker bleed
                    time_in_force='GTC',
                )
                budget.use(book_id)
                placed += 1
                logger.debug(f"{side} {qty}@{price} ON BOOK {book_id}")
            except Exception as e:
                logger.debug(f"Place order error book {book_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # RESPONSE FACTORY
    # ─────────────────────────────────────────────────────────────────────────

    def _make_response(self):
        return CompatFinanceAgentResponse(agent_id=self.uid, accounts=self.accounts)

    def _empty_response(self):
        return self._make_response()
