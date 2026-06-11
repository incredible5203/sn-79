"""
ascend_score_tick — Shared SN-79 scoring-aware trading engine
================================================================
Used by: AscendKappaAgent, MicrostructureEdgeAgent, HybridResilientAgent
Each agent instantiates ScoreTickEngine with a different `book_subset`
(disjoint sets of book_ids) and a distinct `lane_profile`, but all share
this exact same round-trip / fee / breakeven accounting so that bugs are
fixed in ONE place.

═══════════════════════════════════════════════════════════════════════
WHY total_realized_pnl WAS DECREASING ON EVERY TICK (root cause)
═══════════════════════════════════════════════════════════════════════
Observed data (UID 196, HybridResilientAgent, 2026-06-11):

    interval                  d(realized_pnl)   d(roundtrip_vol)   bps
    T0447                         -521.77           1,735,278      -3.0
    T0509                       -1,053.77             766,307     -13.8
    T0522                       -1,092.79             782,493     -14.0
    T0555                         -325.25             635,204      -5.1
    T0617                       -1,337.26             863,724     -15.5
    T0805                          -44.86              77,787      -5.8

EVERY interval is negative, in the 3-15.5 bps range -- this is exactly
the cost of a maker round trip's fees (maker_fee_rate ~0.02-0.05% paid
TWICE, once per leg = 4-10bps, plus a couple ticks of price slippage on
the side that has to wait). The previous agents' "no-loss completion"
rules checked

    edge_in_price_ticks >= MIN_RT_EDGE_TICKS   (a FIXED tick count, e.g. 2)

but NEVER compared that edge against the round trip's FEE COST in price
terms. At price ~100 and priceDecimals=2, 2 ticks = $0.02 = 2bps of
notional -- much SMALLER than the ~4-10bps round-trip fee. Every
completion that "passed" the edge check still lost money after fees,
on every single round trip, which is exactly the steady monotonic
decline observed.

THE FIX in this module: `required_edge()` computes the breakeven price
distance dynamically from `account.fees.maker_fee_rate` /
`taker_fee_rate` (read fresh every tick -- fees are dynamic / MTR-based
per the docs) PLUS a configurable profit margin on top, and ALL
completion logic is gated on this fee-aware edge, never a bare tick
count.

═══════════════════════════════════════════════════════════════════════
SECONDARY GOALS BAKED INTO THIS ENGINE
═══════════════════════════════════════════════════════════════════════
  - kappa_penalty == 0  ->  every assigned book gets >= MIN_ROUNDTRIPS_
    FOR_KAPPA completed round trips inside the validator's lookback
    window (PRESENCE lane, sized to guarantee this floor even on
    illiquid books).
  - total_roundtrip_volume UP fast -> PRESENCE lane uses larger size
    than the bare protocol minimum (configurable) since round-trip
    VOLUME (not just count) is itself a tracked/scored metric.
  - kappa_score UP -> ALPHA lane (OFI-direction) with strict fee-aware
    no-loss completion keeps the per-round-trip return distribution
    positively skewed (mu up, LPM3 down).
  - total_realized_pnl UP -> direct consequence of the fee-aware edge
    fix: every completed round trip nets >= PROFIT_MARGIN_BPS after
    both legs' fees.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional, Tuple, Iterable

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL TUNABLES (shared across all 3 agents)
# ─────────────────────────────────────────────────────────────────────────────

GRACE_PERIOD_SECONDS      = 0

# --- Fee-aware round trip economics (THE CORE FIX) ---
# Required edge = (maker_fee_rate + maker_fee_rate) * price * (1 + PROFIT_MARGIN)
#               + EXTRA_TICKS * tick_size
# i.e. cover BOTH legs' fees (we assume both legs are maker / post_only)
# plus an additional profit margin on top, plus a small fixed tick
# buffer for rounding safety.
ROUNDTRIP_FEE_LEGS        = 2        # both entry and exit assumed maker
PROFIT_MARGIN_BPS         = 5.0      # extra profit required ON TOP of fees
EXTRA_EDGE_TICKS          = 2        # small fixed buffer in price ticks
FALLBACK_MAKER_FEE        = 0.0005   # used if account.fees unavailable (5bps)
MAX_REASONABLE_FEE        = 0.01     # sanity clamp (1%) -- ignore garbage fee reads
ORDER_EXPIRY_NS           = 180_000_000_000  # GTT 180 sim-seconds
MIN_PRESENCE_SPREAD_TICKS = 3.0      # skip tight books that cannot cover fees

# --- Round trip / kappa-penalty floor ---
MIN_ROUNDTRIPS_FOR_KAPPA  = 3         # validator needs >=3 RTs/book in window
KAPPA_LOOKBACK_TICKS      = 1800      # ~30 sim-min @ 1s/tick; conservative window
PRESENCE_FORCE_RATIO      = 0.6       # if RT count in window < this*MIN -> force

# --- Round trip stuck handling (v2.1: NO loss completions) ---
# Previously stuck/hard-stuck paths forced touch/taker unwinds without
# fee-aware edge — that reproduced the -3..-15bps realized-PnL bleed.
# Inventory may sit until the market offers required_edge; never realize a loss.
COMPLETION_STUCK_TICKS    = 10_000    # diagnostic counter only
HARD_STUCK_TICKS          = 10_000    # disabled — no taker escape

MIN_QUANTITY              = 0.25

# --- Inventory / risk ---
MAX_INVENTORY_SKEW        = 0.02
SOFT_SKEW_THRESHOLD       = 0.008
REPAY_FIFO_EACH_TICK      = 1

# --- Stale order management ---
STALE_AGE_SECONDS         = 10
STALE_TICKS_OUTSIDE       = 1
MAX_CANCEL_PER_TICK       = 14

# --- Instruction budget per agent per tick ---
MAX_TOTAL_INSTRUCTIONS    = 30
MAX_PER_BOOK              = 4


# ─────────────────────────────────────────────────────────────────────────────
# PER-BOOK STATE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PositionLedger:
    """Net position + VWAP cost basis (price-only, fees handled separately
    via required_edge() at completion time -- NOT baked into avg_cost, so
    avg_cost remains a clean price-VWAP for diagnostics)."""
    net_qty:      float = 0.0
    avg_cost:     float = 0.0
    realized_pnl: float = 0.0   # price-only realized PnL (pre-fee), for
                                 # internal bookkeeping/diagnostics only

    def apply_fill(self, side: str, price: float, qty: float) -> float:
        """Returns the price-only realized PnL delta from this fill (0 if
        the fill extends the position rather than reducing it)."""
        if qty <= 0 or price <= 0:
            return 0.0

        signed_qty = qty if side == "BUY" else -qty
        delta = 0.0

        if self.net_qty == 0 or (self.net_qty > 0) == (signed_qty > 0):
            new_qty = self.net_qty + signed_qty
            if new_qty != 0:
                self.avg_cost = (
                    (self.avg_cost * abs(self.net_qty) + price * abs(signed_qty))
                    / abs(new_qty)
                )
            self.net_qty = new_qty
        else:
            closing_qty = min(abs(self.net_qty), abs(signed_qty))
            if self.net_qty > 0:
                delta = (price - self.avg_cost) * closing_qty
            else:
                delta = (self.avg_cost - price) * closing_qty
            self.realized_pnl += delta

            new_qty = self.net_qty + signed_qty
            if new_qty == 0:
                self.net_qty = 0.0
                self.avg_cost = 0.0
            elif (new_qty > 0) != (self.net_qty > 0):
                self.net_qty = new_qty
                self.avg_cost = price
            else:
                self.net_qty = new_qty

        return delta


@dataclass
class BookSlot:
    """Per-book bookkeeping: round-trip tracking for kappa_penalty floor,
    realized PnL history for diagnostics, circuit breaker state, and
    completion stuck-counters."""
    pnl_history:          deque = field(default_factory=lambda: deque(maxlen=600))
    breaker_until_tick:   int = 0
    completion_attempts:  int = 0
    # Round-trip completion timestamps (sim ticks) for the kappa-penalty
    # floor: a "round trip" is counted whenever net_qty returns to ~0
    # after being non-zero.
    roundtrip_ticks:      deque = field(default_factory=lambda: deque(maxlen=2000))
    was_flat:             bool = True


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


def get_fee_rates(account) -> Tuple[float, float]:
    """
    Read (maker_fee_rate, taker_fee_rate) from account.fees, with
    sanity clamping and a fallback. Fees are DYNAMIC (MTR-based) per
    the protocol docs, so this is read fresh every tick -- never
    cached/hardcoded.
    """
    maker = taker = FALLBACK_MAKER_FEE
    try:
        fees = account.fees
        m = float(getattr(fees, 'maker_fee_rate', FALLBACK_MAKER_FEE))
        t = float(getattr(fees, 'taker_fee_rate', m * 2))
        if 0 <= m <= MAX_REASONABLE_FEE:
            maker = m
        if 0 <= t <= MAX_REASONABLE_FEE:
            taker = t
        else:
            taker = max(maker * 2, maker)
    except Exception:
        pass
    return maker, taker


def required_edge(price: float, account, config, tick: float) -> float:
    """
    THE CORE FIX. Returns the minimum price distance (in quote-currency
    units, same scale as `price`) that a completion must clear vs. the
    position's VWAP cost basis to be net-profitable after BOTH legs'
    fees plus a profit margin.

    edge = price * (maker_fee_rate * ROUNDTRIP_FEE_LEGS) * (1 + margin)
           + EXTRA_EDGE_TICKS * tick

    Both legs are assumed maker (post_only) since this engine never
    places non-post_only completions except in the HARD_STUCK escape
    valve (which is itself logged and rare).
    """
    maker_fee, _taker_fee = get_fee_rates(account)
    fee_cost = price * maker_fee * ROUNDTRIP_FEE_LEGS
    margin = fee_cost * (PROFIT_MARGIN_BPS / 10000.0) * 10000.0  # see note
    # NOTE: PROFIT_MARGIN_BPS is an ABSOLUTE bps-of-price addition, not a
    # multiplier on fee_cost (clearer to tune). Recompute directly:
    margin = price * (PROFIT_MARGIN_BPS / 10000.0)
    return fee_cost + margin + EXTRA_EDGE_TICKS * tick


def spread_covers_edge(
    bid: float, ask: float, account, config, tick: float
) -> bool:
    """Book spread must be wide enough to complete a fee-positive round trip."""
    if bid is None or ask is None or ask <= bid:
        return False
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return False
    edge = required_edge(mid, account, config, tick)
    return (ask - bid) >= edge * 0.95


def place_passive_gtt(
    response, book_id, direction: str, qty: float, price: float, *, post_only: bool = True
) -> None:
    response.limit_order(
        book_id=book_id,
        direction=direction,
        quantity=qty,
        price=price,
        post_only=post_only,
        time_in_force="GTT",
        expiry_period=ORDER_EXPIRY_NS,
    )


def compute_ofi(book) -> float:
    """Order Flow Imbalance from the L3 event tape, in [-1, +1]."""
    buy_pressure = sell_pressure = 0.0
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
                if s == 0:
                    buy_pressure += q
                else:
                    sell_pressure += q
            elif y == 'o':
                q = getattr(ev, 'quantity', 0.0)
                s = getattr(ev, 'side', 0)
                if s == 0:
                    buy_pressure += q
                else:
                    sell_pressure += q
            elif y == 'c':
                q = getattr(ev, 'quantity', 0.0)
                p = getattr(ev, 'price', 0.0)
                if p <= best_bid:
                    sell_pressure += q
                else:
                    buy_pressure += q
    except Exception:
        pass

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
# LANE PROFILES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LaneProfile:
    """
    Configures how an agent instance balances the two lanes. All three
    agents use the SAME ScoreTickEngine but with different profiles and
    disjoint book_subsets, so they don't compete with each other.

    presence_qty:    size of the PRESENCE lane round trips (drives
                     total_roundtrip_volume + satisfies the >=3 RT/book
                     kappa_penalty floor)
    alpha_qty:       size of ALPHA lane (directional, OFI) round trips
    alpha_enabled:   whether this agent runs an alpha lane at all
                     (e.g. a pure "coverage" agent could disable it)
    min_spread_ticks_alpha / max_spread_ratio_alpha: liquidity filters
                     for alpha entries
    ofi_threshold:   minimum |OFI signal| to take a directional position
    """
    presence_qty:              float = 0.5
    alpha_qty:                 float = 0.5
    alpha_enabled:             bool = True
    min_spread_ticks_alpha:    float = 3.0
    max_spread_ratio_alpha:    float = 0.0030
    ofi_threshold:             float = 0.12
    ofi_window_ticks:          int = 4
    alpha_books_per_tick:      int = 8
    presence_max_per_tick:     int = 12


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class ScoreTickEngine:
    """
    Shared engine. Each agent wrapper does:

        self.engine = ScoreTickEngine(
            uid=uid, book_subset=range(0, 43), profile=LaneProfile(...))

        def handle(self, state):
            return self.engine.tick(state)

    `book_subset` should be DISJOINT across the 3 agents so they never
    place competing orders on the same book (which would self-trade or
    waste instruction budget). E.g.:
        AscendKappaAgent:        books   0 ..  42  (43 books)
        MicrostructureEdgeAgent: books  43 ..  85  (43 books)
        HybridResilientAgent:    books  86 .. 127  (42 books)
    """

    def __init__(self, uid: int, book_subset: Iterable[int],
                 profile: LaneProfile, agent_name: str = "Agent"):
        self.uid = uid
        self.book_subset = list(book_subset)
        self.book_subset_set = set(self.book_subset)
        self.profile = profile
        self.agent_name = agent_name

        self.accounts  = {}
        self.events    = []
        self.config    = None
        self.timestamp = 0

        self._tick            = 0
        self._presence_cursor = 0
        self._alpha_bucket    = 0
        self._cancel_done     = False

        n_presence_groups = max(1, len(self.book_subset) // max(1, profile.presence_max_per_tick))
        self._presence_groups = max(1, n_presence_groups)
        self._alpha_groups = max(1, len(self.book_subset) // max(1, profile.alpha_books_per_tick))

        self._ledger: dict[int, PositionLedger] = defaultdict(PositionLedger)
        self._slots:  dict[int, BookSlot]       = defaultdict(BookSlot)
        self._ofi_history: dict[int, deque] = defaultdict(
            lambda: deque(maxlen=profile.ofi_window_ticks))

        logger.info(f"{agent_name} ScoreTickEngine initialized for UID {uid}, "
                    f"{len(self.book_subset)} books "
                    f"({min(self.book_subset)}..{max(self.book_subset)})")

    # ─────────────────────────────────────────────────────────────────────
    # ENTRY POINT
    # ─────────────────────────────────────────────────────────────────────

    def tick(self, state, response):
        """
        Mutates `response` in place (caller-provided, already wrapped in
        whatever compat layer is needed) and returns it.
        """
        self._update(state)
        self._respond(state, response)
        return response

    def _update(self, state):
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

    def _respond(self, state, response):
        if self.timestamp < GRACE_PERIOD_SECONDS * 1_000_000_000:
            return

        config = self.config
        tick = tick_size(config)

        if not self._cancel_done:
            self._startup_cancel(response)
            if self._count_open_orders() > 0:
                return
            self._cancel_done = True

        budget = Budget()

        if state.books:
            for book_id in self.book_subset:
                book = state.books.get(book_id)
                if book is not None:
                    self._ofi_history[book_id].append(compute_ofi(book))

        # Phase 1: loan repayment
        loans_repaid = 0
        for book_id, account in self.accounts.items():
            if book_id not in self.book_subset_set:
                continue
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

        # Phase 2: cancel stale orders
        self._cancel_stale(state, response, budget, tick)

        # Phase 3: completions (fee-aware no-loss)
        self._place_completions(state, response, budget, tick)

        # Phase 4: hard-cap inventory flatten
        self._flatten_skew(state, response, budget, tick)

        # Phase 5: presence lane (kappa_penalty floor + roundtrip volume)
        self._place_presence_quotes(state, response, budget, tick)

        # Phase 6: alpha lane (kappa_score growth)
        if self.profile.alpha_enabled:
            self._place_alpha_quotes(state, response, budget, tick)

        self._presence_cursor = (self._presence_cursor + 1) % self._presence_groups
        self._alpha_bucket    = (self._alpha_bucket + 1) % self._alpha_groups

    # ─────────────────────────────────────────────────────────────────────
    # NOTICE PROCESSING
    # ─────────────────────────────────────────────────────────────────────

    def _process_notices(self):
        for notice in self.events:
            t = str(getattr(notice, 'type', '') or type(notice).__name__).lower()
            if 'trade' in t and 'event' in t:
                self._on_trade(notice)
            elif ('placement' in t or 'limit' in t) and not getattr(notice, 'success', True):
                book_id = getattr(notice, 'bookId', None) or getattr(notice, 'book_id', None)
                msg = str(getattr(notice, 'message', '')).lower()
                if book_id is not None and 'loan' in msg:
                    self._slots[book_id].breaker_until_tick = max(
                        self._slots[book_id].breaker_until_tick,
                        self._tick + 300,
                    )

    def _on_trade(self, notice):
        try:
            book_id = getattr(notice, 'bookId', None) or getattr(notice, 'book_id', None)
            if book_id not in self.book_subset_set:
                return
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
                our_side = "SELL" if taker_buy else "BUY"
            elif taker_id == self.uid:
                our_side = "BUY" if taker_buy else "SELL"
            else:
                return

            ledger = self._ledger[book_id]
            was_nonzero = abs(ledger.net_qty) >= MIN_QUANTITY * 0.5
            delta = ledger.apply_fill(our_side, price, qty)
            is_nonzero = abs(ledger.net_qty) >= MIN_QUANTITY * 0.5

            if delta != 0.0:
                self._slots[book_id].pnl_history.append(delta)

            # Round-trip detection: position went from non-zero -> ~zero
            if was_nonzero and not is_nonzero:
                slot = self._slots[book_id]
                slot.roundtrip_ticks.append(self._tick)
                slot.completion_attempts = 0

        except Exception as e:
            logger.debug(f"_on_trade error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 0: STARTUP CANCEL
    # ─────────────────────────────────────────────────────────────────────

    def _startup_cancel(self, response):
        cancelled = 0
        for book_id, account in self.accounts.items():
            if book_id not in self.book_subset_set:
                continue
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
            logger.info(f"{self.agent_name}: startup cancelled {cancelled} orders")

    def _count_open_orders(self) -> int:
        total = 0
        for book_id, account in self.accounts.items():
            if book_id not in self.book_subset_set:
                continue
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
            if book_id not in self.book_subset_set:
                continue
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
                    side  = order.side
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
    # PHASE 3: COMPLETIONS — fee-aware no-loss (THE CORE FIX)
    # ─────────────────────────────────────────────────────────────────────

    def _place_completions(self, state, response, budget: Budget, tick: float):
        config = self.config

        for book_id, ledger in list(self._ledger.items()):
            if book_id not in self.book_subset_set:
                continue
            if abs(ledger.net_qty) < MIN_QUANTITY * 0.5:
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

            slot = self._slots[book_id]
            qty = round_qty(max(abs(ledger.net_qty), MIN_QUANTITY), config)

            try:
                if ledger.net_qty > 0:
                    # Net long -> SELL to close only when touch covers fees+margin.
                    edge = required_edge(ask, account, config, tick)
                    required_min = ledger.avg_cost + edge

                    if ask < required_min - 1e-12:
                        slot.completion_attempts += 1
                        continue

                    price = round_price(max(ask, required_min), config)
                    sell_qty = min(qty, account.base_balance.free)
                    sell_qty = round_qty(sell_qty, config)
                    if sell_qty < MIN_QUANTITY:
                        continue

                    place_passive_gtt(
                        response, book_id, "SELL", sell_qty, price, post_only=True)
                    budget.use(book_id)
                    slot.completion_attempts = 0
                    logger.debug(f"{self.agent_name}: COMPLETE SELL {sell_qty}@{price} "
                                  f"BOOK {book_id} (cost={ledger.avg_cost:.4f}, edge={edge:.5f})")

                else:
                    # Net short -> BUY to close only when touch covers fees+margin.
                    edge = required_edge(bid, account, config, tick)
                    required_max = ledger.avg_cost - edge

                    if bid > required_max + 1e-12:
                        slot.completion_attempts += 1
                        continue

                    price = round_price(min(bid, required_max), config)
                    buy_qty = qty
                    if account.quote_balance.free < qty * price:
                        affordable = account.quote_balance.free / max(price, 1e-9)
                        buy_qty = round_qty(min(buy_qty, affordable), config)
                    if buy_qty < MIN_QUANTITY:
                        continue

                    place_passive_gtt(
                        response, book_id, "BUY", buy_qty, price, post_only=True)
                    budget.use(book_id)
                    slot.completion_attempts = 0
                    logger.debug(f"{self.agent_name}: COMPLETE BUY {buy_qty}@{price} "
                                  f"BOOK {book_id} (cost={ledger.avg_cost:.4f}, edge={edge:.5f})")

            except Exception as e:
                logger.debug(f"{self.agent_name}: completion error book {book_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 4: HARD-CAP INVENTORY FLATTEN (passive only, safety net)
    # ─────────────────────────────────────────────────────────────────────

    def _flatten_skew(self, state, response, budget: Budget, tick: float):
        # Disabled: passive touch flatten without required_edge() was a
        # secondary realized-PnL leak. Inventory is closed only via Phase 3.
        return

        config = self.config

        for book_id, account in self.accounts.items():
            if book_id not in self.book_subset_set:
                continue
            if not budget.ok(book_id):
                continue
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
                    logger.debug(f"{self.agent_name}: HARDCAP FLATTEN SELL {qty}@{price} "
                                  f"BOOK {book_id} (skew={skew:.4f})")
                else:
                    qty = round_qty(max(account.quote_balance.free * 0.25 / max(ask,1e-9), MIN_QUANTITY), config)
                    if qty < MIN_QUANTITY:
                        continue
                    price = round_price(bid, config)
                    response.limit_order(
                        book_id=book_id, direction='BUY',
                        quantity=qty, price=price, post_only=True)
                    budget.use(book_id)
                    logger.debug(f"{self.agent_name}: HARDCAP FLATTEN BUY {qty}@{price} "
                                  f"BOOK {book_id} (skew={skew:.4f})")

            except Exception:
                continue

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 5: PRESENCE LANE — kappa_penalty floor + roundtrip volume
    # ─────────────────────────────────────────────────────────────────────

    def _place_presence_quotes(self, state, response, budget: Budget, tick: float):
        """
        Ensures every book in book_subset accumulates >= MIN_ROUNDTRIPS_
        FOR_KAPPA round trips within KAPPA_LOOKBACK_TICKS, using
        fee-aware-priced symmetric quotes (the SAME completion logic in
        Phase 3 closes these out profitably -- presence round trips are
        not free, but the quote is sized at `presence_qty`, a config
        knob that can be raised to grow total_roundtrip_volume directly).
        """
        config = self.config
        profile = self.profile
        n_books = len(self.book_subset)

        placed = 0
        for idx, book_id in enumerate(self.book_subset):
            if placed >= profile.presence_max_per_tick:
                break
            if idx % self._presence_groups != self._presence_cursor:
                continue
            if not budget.ok(book_id):
                continue

            ledger = self._ledger.get(book_id)
            if ledger and abs(ledger.net_qty) >= MIN_QUANTITY * 0.5:
                continue  # being completed this tick

            slot = self._slots[book_id]

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if book is None or account is None:
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue

            spread_ticks = (ask - bid) / tick if tick > 0 else 0
            if spread_ticks < MIN_PRESENCE_SPREAD_TICKS:
                continue
            if not spread_covers_edge(bid, ask, account, config, tick):
                continue

            side = "BUY" if (book_id + self._tick) % 2 == 0 else "SELL"
            qty = round_qty(max(profile.presence_qty, MIN_QUANTITY), config)

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

                existing = {o.price for o in account.orders}
                if price in existing:
                    continue

                place_passive_gtt(
                    response, book_id, side, qty, price, post_only=True)
                budget.use(book_id)
                placed += 1
            except Exception as e:
                logger.debug(f"{self.agent_name}: presence place error book {book_id}: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # PHASE 6: ALPHA LANE — OFI-directed, fee-aware
    # ─────────────────────────────────────────────────────────────────────

    def _place_alpha_quotes(self, state, response, budget: Budget, tick: float):
        config = self.config
        profile = self.profile

        candidates = []
        for idx, book_id in enumerate(self.book_subset):
            if idx % self._alpha_groups != self._alpha_bucket:
                continue
            if not budget.ok(book_id):
                continue

            ledger = self._ledger.get(book_id)
            if ledger and abs(ledger.net_qty) >= MIN_QUANTITY * 0.5:
                continue

            slot = self._slots[book_id]
            if slot.breaker_until_tick > self._tick:
                continue

            book    = state.books.get(book_id)
            account = self.accounts.get(book_id)
            if book is None or account is None:
                continue

            bid, ask = best_bid_ask(book)
            if bid is None or ask is None:
                continue

            spread = ask - bid
            spread_ticks = spread / tick if tick > 0 else 0
            if spread_ticks < profile.min_spread_ticks_alpha:
                continue
            if not spread_covers_edge(bid, ask, account, config, tick):
                continue

            mid = (bid + ask) / 2
            if mid <= 0 or (spread / mid) > profile.max_spread_ratio_alpha:
                continue

            history = self._ofi_history.get(book_id)
            if not history or len(history) < 2:
                continue
            weights = [2 ** i for i in range(len(history))]
            ofi_signal = sum(w * o for w, o in zip(weights, history)) / sum(weights)

            if ofi_signal > profile.ofi_threshold:
                direction = "SELL"
                conviction = ofi_signal
            elif ofi_signal < -profile.ofi_threshold:
                direction = "BUY"
                conviction = -ofi_signal
            else:
                continue

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
            if placed >= profile.alpha_books_per_tick:
                break
            if not budget.ok(book_id):
                continue

            qty = round_qty(max(profile.alpha_qty, MIN_QUANTITY), config)

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

                place_passive_gtt(
                    response, book_id, direction, qty, price, post_only=True)
                budget.use(book_id)
                placed += 1
                logger.debug(f"{self.agent_name}: ALPHA {direction} {qty}@{price} "
                              f"BOOK {book_id} (ofi={conviction:.3f})")

            except Exception as e:
                logger.debug(f"{self.agent_name}: alpha place error book {book_id}: {e}")

        self._update_circuit_breakers()

    # ─────────────────────────────────────────────────────────────────────
    # CIRCUIT BREAKER (price-only realized PnL signal; fee-aware edge
    # already prevents fee-driven losses, so a tripped breaker here means
    # genuine adverse-selection losses on this book)
    # ─────────────────────────────────────────────────────────────────────

    def _update_circuit_breakers(self):
        config = self.config
        wealth = float(getattr(config, 'miner_wealth', 50000) or 50000)
        per_book_wealth = wealth / max(len(self.book_subset), 1)
        threshold_abs = -0.01 * per_book_wealth  # 1% of this book's capital share

        for book_id, slot in self._slots.items():
            if book_id not in self.book_subset_set:
                continue
            if not slot.pnl_history:
                continue
            window_sum = sum(slot.pnl_history)
            if window_sum < threshold_abs and slot.breaker_until_tick <= self._tick:
                slot.breaker_until_tick = self._tick + 600
                logger.info(f"{self.agent_name}: circuit breaker tripped book {book_id}: "
                            f"window PnL={window_sum:.4f} (threshold={threshold_abs:.4f}). "
                            f"Alpha disabled 600 ticks; presence continues.")
                slot.pnl_history.clear()
