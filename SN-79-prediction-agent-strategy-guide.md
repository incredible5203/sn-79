# SN-79 Prediction Agent Strategy Guide
## Maximize κ₃ Fast · Penalty = 0 · Positive Realized PnL · With Price Prediction

> **Goal:** κ_score ↑ (79% of total) · total_realized_pnl > 0 always · kappa_penalty = 0  
> **Version:** τaos 0.4.5 · June 2026

---

## Table of Contents

1. [Scoring Mechanics Deep Dive](#1-scoring-mechanics-deep-dive)
2. [Why Kappa Is Hard to Raise Fast](#2-why-kappa-is-hard-to-raise-fast)
3. [The Prediction Opportunity](#3-the-prediction-opportunity)
4. [Strategy Options — Ranked by Impact](#4-strategy-options--ranked-by-impact)
   - [Option A: PredictiveMakerAgent ⭐ Recommended #1](#option-a-predictivemakeragent--recommended-1)
   - [Option B: AdaptiveSteadyMaker ⭐ Recommended #2](#option-b-adaptivesteadymaker--recommended-2)
   - [Option C: MicropriceMomentumMaker ⭐ Recommended #3](#option-c-micropricemomentummaker--recommended-3)
   - [Option D: CrossBookRelativeValueAgent](#option-d-crossbookrelativevalueagent)
   - [Option E: HybridTakerMaker (risky)](#option-e-hybridtakermaker-risky)
5. [Prediction Logic — Signals Available and How to Use Them](#5-prediction-logic--signals-available-and-how-to-use-them)
6. [Penalty = 0 Architecture](#6-penalty--0-architecture)
7. [Realized PnL Protection Rules](#7-realized-pnl-protection-rules)
8. [κ₃ Fast-Ramp Timeline](#8-κ₃-fast-ramp-timeline)
9. [Implementation Blueprint](#9-implementation-blueprint)
10. [Parameter Tuning Reference](#10-parameter-tuning-reference)

---

## 1. Scoring Mechanics Deep Dive

### The Formula

```
TradingScore = 0.79 × KappaScore + 0.21 × PnLScore
```

### What κ₃ Actually Measures

```
κ₃ = (μ - τ) / LPM₃(τ)^(1/3)

where:
  μ     = mean of MAD-normalized realized PnL per observation
  τ     = 0 (threshold — must beat breakeven)
  LPM₃  = mean(max(τ - r_t, 0)³) — CUBED downside deviations
```

**Key insight — the cube is brutal:**

| Scenario | μ | LPM₃ | κ₃ |
|---|---|---|---|
| +0.05 every tick, zero losses | 0.05 | 0 (→ uses UPM₃) | **Very high** |
| +0.10 most ticks, -0.10 once | 0.07 | 0.001 | Moderate |
| +0.05 most ticks, -0.30 once | 0.03 | 0.027 | **Near zero** (denominator explodes) |
| Breakeven average | 0.0 | Any | Zero (numerator = 0) |

**The single most dangerous thing:** one large realized loss. It cubes in the denominator and wipes out 50 ticks of wins.

### KappaScore Pipeline

```
raw κ₃ per book
    → normalize to [0, 1] using range [-2.5, 2.5]
    → × activity_factor (1.0–2.0 based on round-trip volume)
    → × optional PnL factor
    → up to 37.5% of books may be inactive (no κ data) without penalty
    → outlier penalty: books > 1.5×IQR below median are penalized
    → median across all scored books
    = KappaScore
```

**Penalty = 0 requires:** no book is an outlier. Every active book must have κ₃ roughly consistent with your median.

### PnL Score (21%)

- Sum realized PnL per book over lookback (~1 sim day)
- Normalize to daily return vs allocated capital per book (`miner_wealth / book_count ≈ 390 QUOTE`)
- Median across books → map to [-0.5, +0.5]

For PnLScore to be positive you need **cumulative realized PnL > 0** across the median book. This means your winning round-trips must outnumber and outweigh your losing ones.

---

## 2. Why Kappa Is Hard to Raise Fast

### The Three Bottlenecks

**Bottleneck 1 — Minimum observations (≥3 per book)**  
κ₃ is undefined until ≥3 non-zero realized PnL samples exist per book in the lookback window. With ~1 tick/second and round-trips taking 2–5 ticks to complete, you need ~15–30 minutes of active quoting per book just to get the first κ reading.

**Bottleneck 2 — Lookback window (1.5–3 sim hours)**  
Even after observations appear, the lookback window takes time to fill with quality data. Early bad trades poison the window for hours.

**Bottleneck 3 — EMA smoothing**  
The score EMA has α ≈ 0.0083 per scoring round (~every 5s). That means it takes ~120 scoring rounds (~10 sim minutes) to reach 63% of a step-change in performance, and ~24 sim hours to fully reflect sustained improvement.

### What This Means Practically

- The first 30–60 sim minutes should be devoted to **populating the observation buffer with profitable trades** — not maximizing volume
- Bad trades early are 3× harder to recover from than good trades of equal magnitude
- **Never place a trade you are not confident will be profitable** — the cost in κ is asymmetric

---

## 3. The Prediction Opportunity

The public source (`agents/SimpleRegressorAgent.py`, `agents/ImbalanceAgent.py`) contains prediction-relevant signals. Here's what's usable and how each helps:

### Available Prediction Signals

#### Signal 1: Order Flow Imbalance (OFI) — from `book.events`
```python
# From the L3 tape each tick
buy_volume  = sum(t.quantity for t in events if t.y == 't' and t.side == 0)
sell_volume = sum(t.quantity for t in events if t.y == 't' and t.side == 1)
ofi = (buy_volume - sell_volume) / (buy_volume + sell_volume + 1e-9)
```
**Predictive power:** OFI > 0 means buying pressure → price likely to rise next tick  
**Use in agent:** When OFI > threshold → only quote ASK (sell into buyers); skip BID quoting

#### Signal 2: Microprice — biased mid
```python
bid_price, bid_qty = book.bids[0].price, book.bids[0].quantity
ask_price, ask_qty = book.asks[0].price, book.asks[0].quantity
microprice = (bid_price * ask_qty + ask_price * bid_qty) / (bid_qty + ask_qty)
```
**Predictive power:** microprice > mid → upward short-term pressure; microprice < mid → downward  
**Use in agent:** When microprice > mid + 1 tick → prefer SELL limit; when < mid - 1 tick → prefer BUY limit

#### Signal 3: Rolling Mid Return (mean reversion)
```python
# Keep history of last N mids per book
returns = [(mid_t - mid_t1) / mid_t1 for mid_t, mid_t1 in zip(mids[1:], mids)]
ewma_return = sum(w*r for w, r in zip(weights, returns)) / sum(weights)
```
**Predictive power:** Over 3–5 ticks, extreme returns tend to mean-revert (background agents are mean-reverting)  
**Use in agent:** After a big up move (ewma_return > threshold) → prefer SELL limit; after big down → BUY

#### Signal 4: Depth Imbalance (queue pressure)
```python
# From L2 top 5 levels
bid_depth = sum(level.quantity for level in book.bids[:5])
ask_depth = sum(level.quantity for level in book.asks[:5])
depth_imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
```
**Predictive power:** High bid depth relative to ask → upward price pressure (sellers will be absorbed faster)  
**Use in agent:** depth_imbalance > 0.3 → expect ask to be lifted → sit on ask

#### Signal 5: Last Trade Momentum (tape momentum)
```python
recent_trades = [e for e in book.events if e.y == 't']
if recent_trades:
    last_side = recent_trades[-1].side  # 0=buyer aggressor, 1=seller aggressor
    trade_count_buy = sum(1 for t in recent_trades if t.side == 0)
    momentum = trade_count_buy / max(len(recent_trades), 1) - 0.5  # [-0.5, +0.5]
```
**Predictive power:** Sustained one-sided aggression → short-term continuation  
**Use in agent:** momentum > 0.3 → quote ASK only (protect against adverse BID fill)

#### Signal 6: SimpleRegressorAgent-style Linear Prediction
The public `SimpleRegressorAgent` maintains a rolling window of features and fits a linear regressor to predict next-tick mid-price direction. Features include: spread, mid return, trade imbalance, bid/ask size ratio.
```python
# Concept from agents/SimpleRegressorAgent.py
features = [spread_ratio, mid_return, ofi, depth_imbalance, microprice_deviation]
# Fit online or batch linear regressor, predict sign of next mid return
prediction = regressor.predict([features])[0]  # positive = up, negative = down
```

### How Prediction Integrates With κ₃ Optimization

The key insight: **prediction improves PnL per trip but cannot replace sound completion discipline.**

Prediction is most valuable for **side selection** (which direction to quote first):
- Quote the side where a taker is **likely coming to you** → higher fill rate
- Avoid quoting the side where adverse selection is likely → prevents bad RT entries

Prediction should **not** be used to skip the completion leg — the completion leg is what locks in realized PnL.

---

## 4. Strategy Options — Ranked by Impact

---

### Option A: PredictiveMakerAgent ⭐ Recommended #1

**Overall rating:** ⭐⭐⭐⭐⭐  
**κ₃ growth speed:** Fast (fill rate + directional edge)  
**Realized PnL reliability:** High  
**Penalty risk:** Low (rotation-based)  
**Complexity:** Medium-High

#### Core Idea

Combines touch-join market making (high fill rate) with OFI + microprice prediction to select **which side to quote first** on each book. When prediction is confident, places only the favored side. When neutral, alternates sides. After every maker fill, immediately completes the round-trip with a limit inside the spread.

The prediction prevents being the "wrong side" maker — e.g., posting a BID just before a wave of selling dumps through your order at a bad price.

#### What Makes This #1

1. **High fill rate** — touch-join (at best_bid / best_ask) fills fast vs. sitting inside
2. **Directional screening** — OFI + microprice reduce adverse selection on new quotes
3. **Disciplined completion** — every fill immediately gets a completing opposite leg
4. **Hard loss prevention** — if prediction is strongly adverse, skip new quotes entirely
5. **Full 128-book rotation** — penalty stays zero

#### Tick Pipeline

```
For each tick:
1. Process notices → update completion queue
2. Cancel stale orders (>2 ticks off touch)
3. Repay loans (FIFO, 1 per tick)
4. For each completion in queue → place opposite limit inside spread (with edge)
5. For each book in rotation bucket:
   a. Compute OFI, microprice, depth imbalance for THIS tick's events
   b. Update rolling signal history (EWM)
   c. Compute combined_signal = w1*OFI + w2*microprice_dev + w3*depth_imbalance
   d. If combined_signal > pos_threshold → quote ASK only
      If combined_signal < neg_threshold → quote BID only
      Else → quote based on inventory skew alternation
   e. Size check, balance check, postOnly=True → submit
6. Return response (< 1s target)
```

#### Signal Architecture

```
Inputs (each tick):
  OFI = (buy_vol - sell_vol) / total_vol         from events tape
  microprice_dev = (microprice - mid) / tick      from L2 touch
  depth_imbalance = (bid_depth - ask_depth) /    from L2 top-5
                    (bid_depth + ask_depth)
  momentum = net_buy_trades / total_trades - 0.5  from events tape

Combined signal:
  S = 0.4*OFI + 0.3*microprice_dev_norm + 0.2*depth_imbalance + 0.1*momentum

Thresholds:
  S > +0.15  → quote ASK side (sell into buyers)
  S < -0.15  → quote BID side (buy from sellers)
  |S| ≤ 0.15 → inventory-skew-driven side selection

Skip book entirely if:
  |S| > 0.50 AND our position is on the wrong side (would add to adverse position)
```

#### Completion Logic

The completion is the most important part — it is what generates positive realized PnL.

```
After maker SELL fill @ price P:
  → Place BUY limit @ max(best_bid, P - MIN_EDGE_TICKS * tick)
  → Use postOnly=False (we WANT to fill quickly for RT completion)
  → Time-in-force: GTT with 10s expiry (if not filled, cancel and retry)

After maker BUY fill @ price P:
  → Place SELL limit @ min(best_ask, P + MIN_EDGE_TICKS * tick)

Minimum edge: 2 ticks (covers maker fee ≈ 0.08%) on both legs
```

#### Risk Controls

- Hard inventory skew limit: if base_value / total_value > 65% or < 35% → only flatten, no new risk
- Book blacklist: if book had realized loss > -0.05 QUOTE on last 3 completions → skip for 20 ticks
- Loan guard: if any loan balance > 0 → close_positions FIFO before new orders on that book

---

### Option B: AdaptiveSteadyMaker ⭐ Recommended #2

**Overall rating:** ⭐⭐⭐⭐½  
**κ₃ growth speed:** Medium-Fast (lower fill rate, higher per-trip quality)  
**Realized PnL reliability:** Very High  
**Penalty risk:** Very Low  
**Complexity:** Medium

#### Core Idea

Inspired by the "SteadyMaker" family in the public repo. Places limits **inside the spread** (not at the touch) — lower fill rate, but every fill is at a better price than the touch, giving more room for a profitable completion. Uses microprice deviation only (simpler, more reliable signal than combined OFI).

Quote BID at `best_bid + 1 tick` (one tick inside) and ASK at `best_ask - 1 tick`. This ensures positive spread capture even after fees if both legs fill near touch.

#### What Makes This #2

1. **Very clean PnL** — inside-spread quotes guarantee positive edge per trip if both legs fill
2. **Low adverse selection** — you never get filled at the worst price in the book
3. **Simpler signal = fewer bugs** — microprice direction only, no complex OFI weighting
4. **Conservative enough to maintain Penalty = 0** — almost never produces outlier-low κ books

#### Trade-off vs Option A

- Lower fill rate → fewer κ₃ observations per hour → slightly slower ramp
- But per-trip quality is higher → once observations accumulate, κ₃ is cleaner

#### Tick Pipeline

```
1. Process notices → completions queue
2. Cancel stale (>3 ticks outside touch)
3. Repay loans
4. Place completions (highest priority)
5. For each rotated book:
   a. Compute microprice deviation
   b. If microprice > mid + 0.5 tick → prefer SELL (inside ask)
      If microprice < mid - 0.5 tick → prefer BUY (inside bid)
      Else → inventory skew decides
   c. Place ONE maker limit per book, 1 tick inside touch
   d. postOnly = True (fail gracefully if spread has closed)
```

#### Spread Filter (critical)

Only quote books where:
- `spread_ticks >= 5` (enough room for inside quote + edge)
- `spread_ratio < 0.002` (not anomalously wide/spiky)
- `maker_fee_rate < 0.0013`

---

### Option C: MicropriceMomentumMaker ⭐ Recommended #3

**Overall rating:** ⭐⭐⭐⭐  
**κ₃ growth speed:** Fast (high selectivity, high per-trip quality)  
**Realized PnL reliability:** High  
**Penalty risk:** Low-Medium (needs careful rotation)  
**Complexity:** Medium

#### Core Idea

Uses microprice + rolling mid-return EWM to take a directional bet: quote **only** the predicted profitable side with slightly aggressive positioning. If prediction is unclear, skip the book entirely. This means fewer books quoted per tick, but every quote is well-placed.

The key difference from Option A: this agent is **willing to skip many books per tick** rather than hedge with inventory-skew quoting when signal is weak. This results in cleaner per-trip PnL at the cost of needing more careful rotation to ensure all 128 books eventually get coverage.

#### Directional Quoting

```
Predict price direction D for next 1–3 ticks using:
  D = sign(0.6 * ewma_return_3tick + 0.4 * microprice_deviation)

If D > 0 (predict up) → quote ASK (sell into the expected buyers)
If D < 0 (predict down) → quote BID (buy into the expected sellers)
If D == 0 or |D| < min_conviction → skip this book

Quote price: AT the touch (best_bid or best_ask), not inside
```

**Why touch-join here?** Because the prediction already gives you directional edge. You don't need spread-capture edge — you need fill speed.

#### Completion Logic (same as Option A)

After directional fill → complete with opposite leg inside spread with MIN_EDGE_TICKS = 3.

---

### Option D: CrossBookRelativeValueAgent

**Overall rating:** ⭐⭐⭐  
**κ₃ growth speed:** Slow-Medium  
**Realized PnL reliability:** Medium  
**Penalty risk:** Low  
**Complexity:** High

#### Core Idea

All 128 books trade the same underlying asset. At any tick, some books have mid prices above the cross-book median, and some below. A book trading 0.10 above the median is "expensive" — quote its ASK (sell to the expensive buyers); a book trading 0.10 below is "cheap" — quote its BID.

This is a pure statistical arbitrage play: you're capturing the cross-book dispersion rather than per-book microstructure.

#### Why It's #4

- Requires computing cross-book median every tick → slightly more CPU
- The dispersion is small and may be arbitraged away by other miners quickly
- More complex to implement correctly than Options A/B/C
- But **very stable PnL** when it works — low variance = good κ₃

#### When to Use

Deploy alongside Option A or B as a **secondary agent** on separate UID if you have multiple registrations. The cross-book signal is independent of per-book OFI.

---

### Option E: HybridTakerMaker (risky)

**Overall rating:** ⭐⭐  
**κ₃ growth speed:** Can be fast OR catastrophic  
**Realized PnL reliability:** Low  
**Penalty risk:** High  
**Complexity:** High

#### Core Idea

When prediction is very strong (OFI + microprice + momentum all agree), take liquidity (market order) in the predicted direction, then immediately post the reverse as a maker to close. The taker leg is the "opener" and the maker leg is the "closer."

#### Why It's #5 (Not Recommended)

- Taker fees are 2–3× higher than maker fees → eat into the edge
- Market impact: your taker order moves the price against your next order
- One bad prediction = large realized loss = κ₃ destroyed (LPM₃ explodes)
- Hard to control inventory risk at scale across 128 books

**Only use if:** you have a prediction signal with > 60% directional accuracy, which is very hard to validate in simulation.

---

## 5. Prediction Logic — Signals Available and How to Use Them

### Signal Combination Architecture (for Option A)

```python
class SignalEngine:
    """
    Computes a combined directional signal for a single book.
    Returns float in [-1.0, +1.0]:
      > 0 = bullish (buy pressure, quote ASK to sell into it)
      < 0 = bearish (sell pressure, quote BID to buy into it)
    """
    
    def __init__(self, ewm_alpha=0.3, history_len=8):
        self.alpha = ewm_alpha
        self.ofi_ema = 0.0
        self.micro_ema = 0.0
        self.mid_history = deque(maxlen=history_len)
    
    def update(self, book) -> float:
        bid = book.bids[0].price
        ask = book.asks[0].price
        bid_q = book.bids[0].quantity
        ask_q = book.asks[0].quantity
        mid = (bid + ask) / 2
        tick = (ask - bid) / max(ask - bid, 0.01)  # normalized tick = 1
        
        # Microprice deviation (normalized by spread)
        spread = ask - bid
        microprice = (bid * ask_q + ask * bid_q) / (bid_q + ask_q)
        micro_dev = (microprice - mid) / max(spread, 0.01)  # ∈ [-0.5, +0.5]
        self.micro_ema = self.alpha * micro_dev + (1 - self.alpha) * self.micro_ema
        
        # OFI from event tape
        buy_vol = sell_vol = 0.0
        for ev in book.events:
            if getattr(ev, 'y', None) == 't':
                if ev.side == 0: buy_vol += ev.quantity
                else: sell_vol += ev.quantity
        total_vol = buy_vol + sell_vol
        ofi = (buy_vol - sell_vol) / max(total_vol, 1e-9)
        self.ofi_ema = self.alpha * ofi + (1 - self.alpha) * self.ofi_ema
        
        # Depth imbalance (top 5 levels)
        bid_depth = sum(l.quantity for l in book.bids[:5])
        ask_depth = sum(l.quantity for l in book.asks[:5])
        depth_total = bid_depth + ask_depth
        depth_imb = (bid_depth - ask_depth) / max(depth_total, 1e-9)
        
        # Mid return momentum
        self.mid_history.append(mid)
        if len(self.mid_history) >= 3:
            ewm_return = sum(
                (0.7**i) * (self.mid_history[-1-i] - self.mid_history[-2-i]) / 
                max(abs(self.mid_history[-2-i]), 1e-9)
                for i in range(min(3, len(self.mid_history)-1))
            ) / sum(0.7**i for i in range(3))
        else:
            ewm_return = 0.0
        
        # Combined signal
        combined = (
            0.35 * self.ofi_ema +
            0.30 * self.micro_ema +
            0.20 * depth_imb +
            0.15 * (ewm_return / max(abs(ewm_return) + 1e-9, 0.001))  # sign-normalized
        )
        return max(-1.0, min(1.0, combined))
```

### How to Gate Quoting Decisions

```
signal = engine.update(book)

if signal > +0.15:
    # Bullish: buyers incoming → sell to them (quote ASK)
    quote_side = "SELL"
    quote_price = best_ask  # touch-join

elif signal < -0.15:
    # Bearish: sellers incoming → buy from them (quote BID)
    quote_side = "BUY"
    quote_price = best_bid  # touch-join

else:
    # Neutral: use inventory skew
    if base_skew > SOFT_SKEW:
        quote_side = "SELL"
        quote_price = best_ask
    elif base_skew < -SOFT_SKEW:
        quote_side = "BUY"
        quote_price = best_bid
    else:
        # Alternate by book_id parity
        quote_side = "SELL" if (book_id + tick_count) % 2 == 0 else "BUY"
        quote_price = best_ask if quote_side == "SELL" else best_bid

# Additional safety gate: don't quote when signal strongly opposes our completion leg
if book_id in completion_queue:
    comp_side = completion_queue[book_id].side
    if (comp_side == "BUY" and signal < -0.40) or (comp_side == "SELL" and signal > 0.40):
        # Skip new quote — focus on getting completion filled
        pass  # don't add new quote instruction
```

### When NOT to Use Prediction

- **Completion legs:** Always place them regardless of signal. Completing a round-trip is more important than signal quality. A pending completion that goes unplaced is an open inventory position — κ₃ doesn't count it until realized.
- **Inventory flattening:** If skew is at the hard limit, flatten regardless of signal.
- **Loan repayment:** Always repay loans first, prediction is irrelevant.

---

## 6. Penalty = 0 Architecture

### Why Penalty Happens

Penalty triggers when some books have κ₃ much lower than your median (1.5× IQR below). This happens when:
1. You never trade a book (κ undefined → treated as 0 → far below median)
2. You trade a book badly (consistent losses on that book → negative κ → outlier)

### The 37.5% Rule

Up to 37.5% of 128 books = **48 books** can be inactive without penalty. So you need κ data on at least **80 books**.

### Rotation Architecture for Penalty = 0

```
ROTATION_GROUPS = 12
Each tick, process books where: book_id % 12 == current_bucket
Bucket advances each tick: current_bucket = (tick + 1) % 12

Result: Each book gets a quoting attempt roughly every 12 ticks
Over a 3-hour lookback (~10,800 ticks), each book gets ~900 quoting attempts
That guarantees >>3 round-trip observations per book on any active book
```

**Minimum books to quote per tick:**
```
Target: cover 80+ books with κ data within 3-hour lookback
At 12 books/tick rotation: 128/12 ≈ 11 books per tick is sufficient
At 11 books/tick × 10,800 ticks = 118,800 book-attempts → all 128 covered many times
```

### Blacklisting Losing Books Safely

When a book shows 3 consecutive negative realized PnLs → blacklist for N ticks, then retry.

**Critical:** If blacklisted book count > 48 (37.5%), un-blacklist the oldest ones to avoid penalty.

```python
def safe_blacklist(book_id, ticks=20):
    # Count currently blacklisted books
    active_blacklist = [b for b, t in blacklist.items() if t > current_tick]
    
    # If blacklisting would exceed 48, don't blacklist (accept some bad trades)
    if len(active_blacklist) >= 48:
        return  # skip blacklisting to protect coverage
    
    blacklist[book_id] = current_tick + ticks
```

---

## 7. Realized PnL Protection Rules

These rules keep `total_realized_pnl` positive and growing:

### Rule 1: Minimum Edge on Completion (Non-Negotiable)
```
MIN_RT_EDGE_TICKS = 2  (absolute minimum)
Recommended = 3        (covers fees with margin)

Completion BUY price = min(best_bid, fill_price - MIN_RT_EDGE_TICKS * tick)
Completion SELL price = max(best_ask, fill_price + MIN_RT_EDGE_TICKS * tick)
```

This guarantees that if both legs fill, realized PnL > 0 even after fees (maker fee ≈ 0.08%).

### Rule 2: Stale Completion Cleanup
If a completion leg sits unfilled for > 10 ticks → cancel and re-evaluate.

Don't let completions get too stale — the market may have moved, making the edge negative.

### Rule 3: Never Cross the Spread
Placing a BUY at >= ask_price or SELL at <= bid_price = taker execution = taker fee.

Taker fee is 2–3× higher than maker. Never do this as a deliberate strategy.

```python
# Safety check before every limit order:
if side == "BUY" and price >= ask:
    price = bid  # force to maker side
if side == "SELL" and price <= bid:
    price = ask  # force to maker side
```

### Rule 4: Inventory Limits

```
Hard limit:  |base_value - quote_value| / total_value > 20%  → stop new quotes, flatten only
Soft limit:  |base_value - quote_value| / total_value > 10%  → skew side selection toward flatten
```

Uncontrolled inventory is how losses accumulate. When you have too much BASE and price falls, every tick is an unrealized loss that becomes realized when you complete.

### Rule 5: Fee Filtering

Only quote books where `maker_fee_rate < 0.0013`. Above this, even a 3-tick round-trip may not be profitable after fees.

```
Profitable round-trip condition:
  spread_capture (ticks) × tick_value > 2 × maker_fee_rate × trade_value
  e.g.: 3 ticks × 0.01 × 0.32 = 0.0096 QUOTE per trip
        2 × 0.0008 × (309 × 0.32) = 0.158 QUOTE in fees
        → need larger qty or more ticks for profitability!
```

Wait — the math shows maker fees are small relative to tick capture. 3 ticks = 0.96 QUOTE capture on a 309-price, 0.32-qty order. Fees = 0.158 QUOTE. Net = +0.80 QUOTE per trip. Good.

### Rule 6: Reject Storm Prevention

Before every order submission:
```python
assert qty >= 0.25                     # min order size
assert qty >= MIN_QUANTITY             # our own floor (0.32 recommended for rounding safety)
assert side == "BUY"  → quote_balance.free >= qty * price
assert side == "SELL" → base_balance.free >= qty
assert per_book_instructions <= 4     # 1 under validator cap for safety
```

---

## 8. κ₃ Fast-Ramp Timeline

### What To Do Each Phase

#### Phase 0: First 10 Sim Minutes — Infrastructure
- [ ] Cancel all ghost orders from previous runs
- [ ] Verify `lazy_load=1` is set
- [ ] Verify response time < 1s in logs
- [ ] Check no `MINIMUM_ORDER_SIZE_VIOLATION` or `EXCEEDING_LOAN` errors

#### Phase 1: First 30 Sim Minutes — Populate Observation Buffer
- Goal: Get ≥3 completed round-trips per book on at least 80 books
- Tactic: Prioritize **high fill-rate touch-join quotes** (Option A or C)
- Avoid: wide-spread inside quoting at this stage (too few fills)
- Monitor: log count of completed round-trips per book

#### Phase 2: 30 Min – 3 Sim Hours — κ₃ Comes Alive
- κ₃ starts appearing on books with enough observations
- Now switch to **quality over quantity**: ensure MIN_RT_EDGE_TICKS ≥ 3
- Enable prediction-based side selection to improve per-trip quality
- Watch for: books with consistently negative κ₃ → blacklist them

#### Phase 3: 3–12 Sim Hours — Score Stabilization
- Maintain steady rotation — don't stop quoting any book
- Keep Penalty = 0 by checking blacklist count < 48
- Realized PnL should be positive and growing by now
- If any book is dragging (κ₃ outlier) → blacklist and allow organic expiry

#### Phase 4: 12+ Sim Hours — Incentive Rises
- By now your EMA-smoothed TradingScore should be climbing
- Dashboard should show: Kappa ↑, Penalty = 0, Realized PnL positive and rising
- On-chain incentive lags ~6–24 sim hours behind dashboard score

---

## 9. Implementation Blueprint

### File Structure

```
~/.taos/agents/
  PredictiveMakerAgent.py    ← Main agent (Option A implementation)
  signal_engine.py           ← Prediction signal computation
  book_filter.py             ← Book selection and scoring
  completion_tracker.py      ← Round-trip completion management
  inventory_guard.py         ← Position limits and skew management
```

### Agent Class Skeleton

```python
class PredictiveMakerAgent(FinanceSimulationAgent):
    """
    Option A: Predictive Maker + Completion Discipline
    """
    
    def initialize(self):
        self.signal_engines = {}        # book_id → SignalEngine
        self.completion_queue = {}      # book_id → CompletionHint
        self.book_stats = {}            # book_id → {consecutive_losses, blacklist_until}
        self.tick_count = 0
        self.rotation_bucket = 0
        self.startup_done = False
    
    def onTrade(self, event):
        """Queue completion after maker fill."""
        if event.makerAgentId == self.uid:
            # We were the passive (maker) party
            side = "BUY" if event.side == 0 else "SELL"  
            # event.side = taker side; if taker bought, we sold → we need to BUY back
            completion_side = "BUY" if event.side == 0 else "SELL"
            self.completion_queue[event.bookId] = CompletionHint(
                side=completion_side,
                fill_price=event.price,
                qty=event.quantity,
            )
    
    def onOrderRejected(self, event):
        """Track rejections to blacklist problematic books."""
        book_id = event.bookId
        msg = str(getattr(event, 'message', '')).lower()
        if 'loan' in msg:
            self.book_stats.setdefault(book_id, {})['blacklist_until'] = (
                self.tick_count + 15
            )
    
    def respond(self, state) -> FinanceAgentResponse:
        response = FinanceAgentResponse(agent_id=self.uid)
        
        if state.timestamp < 620e9:  # grace period
            return response
        
        budget = InstructionBudget(total=28, per_book=4)
        
        # Phase 0: Startup
        if not self.startup_done:
            self._startup_cancel_all(state, response)
            if self._has_open_orders():
                return response
            self.startup_done = True
        
        # Phase 1: Loan repayment
        self._repay_loans(state, response, budget)
        
        # Phase 2: Cancel stale
        self._cancel_stale(state, response, budget)
        
        # Phase 3: Completions (highest priority for PnL)
        self._place_completions(state, response, budget)
        
        # Phase 4: Flatten if skewed
        self._flatten_if_needed(state, response, budget)
        
        # Phase 5: New prediction-guided quotes
        self._place_predictive_quotes(state, response, budget)
        
        self.tick_count += 1
        self.rotation_bucket = (self.rotation_bucket + 1) % 12
        return response
```

### Key Parameters to Tune

| Parameter | Conservative | Balanced | Aggressive |
|---|---|---|---|
| `MIN_SPREAD_TICKS` | 5 | 4 | 3 |
| `MIN_RT_EDGE_TICKS` | 3 | 2 | 2 |
| `BASE_QUANTITY` | 0.30 | 0.32 | 0.40 |
| `MAX_BOOKS_PER_TICK` | 8 | 11 | 14 |
| `SIGNAL_THRESHOLD` | 0.20 | 0.15 | 0.10 |
| `MAX_SKEW` | 0.12 | 0.15 | 0.20 |
| `STALE_AGE_SECONDS` | 5 | 8 | 12 |
| `BLACKLIST_TICKS` | 30 | 20 | 10 |

---

## 10. Parameter Tuning Reference

### When κ₃ is Undefined (None)
**Cause:** Not enough round-trip completions (< 3) in lookback window  
**Fix:** Increase `MAX_BOOKS_PER_TICK`, reduce `MIN_SPREAD_TICKS` to 3 temporarily, ensure completion logic fires every tick

### When κ₃ is Positive But Penalty > 0
**Cause:** Some books have negative κ₃ acting as outliers  
**Fix:** Enable book blacklisting, check which books have negative realized PnL, reduce `BLACKLIST_TICKS` to recover coverage faster

### When Realized PnL is Falling Despite Fills
**Cause:** Completions are being placed at prices worse than the fill  
**Fix:** Increase `MIN_RT_EDGE_TICKS` to 3, check that completion price formula doesn't allow negative edge

### When Many Timeouts Appear
**Cause:** Too many books per tick, heavy signal computation  
**Fix:** Reduce `MAX_BOOKS_PER_TICK` to 8, precompute signals asynchronously where possible, verify `lazy_load=1`

### When EXCEEDING_LOAN Errors Appear
**Cause:** Margin loans not being repaid  
**Fix:** Enable FIFO loan repayment at top of every respond(), add `leverage=0` on all orders, add loan headroom check before each order

---

## Summary: Recommended Approach

1. **Deploy Option A (PredictiveMakerAgent)** as your primary strategy
2. **Start with Conservative parameters** for the first 6 sim hours — let the observation buffer fill with clean trades
3. **Switch to Balanced parameters** once κ₃ > 0 on at least 60 books
4. **Monitor blacklist count** — keep it < 48 to maintain Penalty = 0
5. **Never skip completions** — they are the source of all realized PnL
6. **Use prediction for side selection only** — not to skip completions or flatten
7. **Trust the EMA** — don't change strategy mid-window; the lookback needs stable data

---

*Document version: 2026-06-12 · τaos 0.4.5 · SN-79 mainnet netuid 79*
