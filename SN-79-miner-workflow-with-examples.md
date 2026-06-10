# SN-79 Miner Workflow — End-to-End Example

> **Audience:** Readers of [SN-79-explainer-for-beginners.md](./SN-79-explainer-for-beginners.md) who want a concrete walkthrough  
> **Example agent:** `TurboPulseV2Agent` with `turbo_profile=recover` (UID 65, mainnet)  
> **Protocol:** `MarketSimulationStateUpdate` → `FinanceAgentResponse`  
> **Related:** [Miner ↔ Validator Protocol](./SN-79-miner-validator-protocol.md) · [Order types & config reference](./SN-79-order-types-and-config-reference.md)

---

## Table of Contents

1. [The one-sentence story](#1-the-one-sentence-story)
2. [What validators send (real request shape)](#2-what-validators-send-real-request-shape)
3. [What miners do on each tick (lifecycle)](#3-what-miners-do-on-each-tick-lifecycle)
4. [Worked example: one full tick](#4-worked-example-one-full-tick)
5. [How miners recognize bad books vs opportunity](#5-how-miners-recognize-bad-books-vs-opportunity)
6. [Rules, signals, and formulas](#6-rules-signals-and-formulas)
7. [How size and price are chosen](#7-how-size-and-price-are-chosen)
8. [How the instruction budget works](#8-how-the-instruction-budget-works)
9. [What miners send back (response)](#9-what-miners-send-back-response)
10. [From this tick to a high score](#10-from-this-tick-to-a-high-score)
11. [Anti-patterns that killed our deploy miners](#11-anti-patterns-that-killed-our-deploy-miners)

---

## 1. The one-sentence story

Every ~1 simulation second, a validator sends a **compressed market snapshot**; the miner **reads books + portfolio + last-tick feedback**, **filters bad books**, **ranks opportunities**, **checks budget and balances**, places a few **limit orders**, and returns them in **under 3 seconds**.

---

## 2. What validators send (real request shape)

Validators call the miner axon with a Bittensor synapse: **`MarketSimulationStateUpdate`**.

In production logs you see lines like:

```
VALIDATOR : 5HYyPA43Hof3Lc9ubf6k3QC5TRAnRvGvfprae67ncWizXMnd
SIMULATION TIME : 17:43:00.000000000 (T=63780000000000)
```

That maps to this logical payload (simplified from a real tick):

```yaml
# MarketSimulationStateUpdate (logical view)
timestamp: 63780000000000          # sim nanoseconds since sim start
version: 45                        # validator taos version (optional)
compression_engine: lz4            # payload often compressed on wire

config:
  book_count: 128
  priceDecimals: 2                 # tick size = 0.01
  volumeDecimals: 4                # qty rounded to 4 decimals
  miner_wealth: 50000.0            # total starting QUOTE
  publish_interval: 1000000000     # ~1 sim second between ticks
  max_open_orders: 20

books:
  42:                              # book_id
    bids: [{price: 309.08, quantity: 4.2}, {price: 309.06, quantity: 8.1}, ...]
    asks: [{price: 309.14, quantity: 3.5}, {price: 309.18, quantity: 6.0}, ...]
    events:                          # trades/orders/cancels since last tick
      - {type: TradeInfo, price: 309.10, quantity: 0.28, side: SELL, ...}
      - {type: Order, price: 309.12, quantity: 1.2, side: BUY, ...}

  112:
    bids: [{price: 315.44, quantity: 5.1}, ...]
    asks: [{price: 315.50, quantity: 4.8}, ...]
    events: [...]

  # ... books 0..127

accounts:
  65:                              # YOUR uid only matters for your slice
    42:
      base_balance:  {free: 0.12, reserved: 0.28, total: 0.40}
      quote_balance: {free: 412.30, reserved: 87.50, total: 499.80}
      orders:
        - {id: 2110405, side: SELL, price: 309.20, quantity: 0.28}
      fees: {maker_fee_rate: 0.0008, taker_fee_rate: 0.0012}
      traded_volume: 4821.5
    112:
      base_balance:  {free: 0.05, reserved: 0.00, total: 0.05}
      quote_balance: {free: 398.20, reserved: 0.00, total: 398.20}
      orders: []
      fees: {maker_fee_rate: 0.0006, taker_fee_rate: 0.0010}
      traded_volume: 3910.2
    # ... one account per book

notices:
  65:                              # feedback from PREVIOUS tick
    - {type: TradeEvent, bookId: 87, makerAgentId: 65, price: 315.44,
       quantity: 0.28, side: SELL, ...}
    - {type: LimitOrderPlacementEvent, bookId: 42, success: true, orderId: 2110405, ...}

dendrite:
  hotkey: "5HYyPA43Hof3Lc9ubf6k3QC5TRAnRvGvfprae67ncWizXMnd"
  ip: "..."
```

**What the miner does *not* receive:** pre-computed “buy/sell signals”, rankings, or scores. The miner must derive mid, spread, skew, tape imbalance, and opportunity scores itself.

---

## 3. What miners do on each tick (lifecycle)

Our `TurboPulseV2Agent` (recover profile) runs this pipeline:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  VALIDATOR sends MarketSimulationStateUpdate (compressed)               │
└───────────────────────────────┬─────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  1. DECOMPRESS + update(state)                                        │
│     - self.accounts = state.accounts[65]                              │
│     - self.events   = state.notices[65]                                 │
│     - dispatch onTrade / onOrderAccepted / onOrderRejected              │
└───────────────────────────────┬─────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  2. respond(state) → FinanceAgentResponse                             │
│     a) Startup cancel-all (first ticks after restart)                   │
│     b) turbo_recover_score_tick():                                    │
│        - repay loans (FIFO)                                             │
│        - scan 128 books → filter bad → rank good                        │
│        - cancel stale orders                                            │
│        - flatten inventory skew (if needed)                             │
│        - place single-sided inside limits on top books                  │
└───────────────────────────────┬─────────────────────────────────────────┘
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  3. report(state, response) — log instructions                          │
│  4. Return synapse with FinanceAgentResponse attached                   │
│     (must finish within ~3s wall clock)                                 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Worked example: one full tick

**Setup:** UID 65, `turbo_profile=recover`, sim time `T = 63_780_000_000_000`.

**Recover params (from `miner.env`):**

| Param | Value |
|-------|-------|
| min_quantity / max_quantity | 0.28 / 0.30 |
| quantity_scale | 0.90 → qty = 0.27 → rounded **0.28** |
| max_books_per_tick | 7 |
| max_total_instructions | 16 |
| max_instructions_per_book | 3 |
| min_spread_ticks | 4 |
| min_rt_edge_ticks | 4 |
| max_spread_ratio | 0.0012 |
| book_rotation_groups | 16 |
| cadence_interval_ns | 24_000_000_000 (24 sim seconds) |

### Step 0 — Startup cancel-all (first restart ticks only)

If `cancel_all_on_startup=1` and open orders exist from a prior strategy:

```python
# Scan all books; cancel up to 28 instructions this tick
CANCEL ORDER #2110405 ON BOOK 87
CANCEL ORDER #2032374 ON BOOK 12
...
# Return early; trading starts next tick when accounts show no orders
```

Log example from a real restart:

```
TurboPulseV2Agent | profile=recover qty[0.28,0.3] books/tick=7 instr_cap=16 cancel_all_on_startup=True
TurboPulseV2Agent | startup cancel-all complete (no orders)
```

### Step 1 — Scan three books (illustrative)

#### Book 7 — **BAD** (spread too tight)

| Field | Value |
|-------|-------|
| best_bid | 300.00 |
| best_ask | 300.03 |
| mid | 300.015 |
| spread_ticks | (300.03 − 300.00) / 0.01 = **3 ticks** |

**Rule:** `spread_ticks < min_spread_ticks (4)` → **SKIP**  
Even if the book looks calm, there is not enough room for a profitable round-trip after fees.

#### Book 55 — **BAD** (toxic tape)

| Field | Value |
|-------|-------|
| best_bid | 308.00 |
| best_ask | 308.08 |
| spread_ticks | 8 ✓ |
| recent tape | 12 sells, 1 buy in `events` |

**Signal:** `tape_imbalance_ratio = (1 − 12) / 13 ≈ −0.85` → `|imbalance| > 0.50` → **SKIP**  
One-sided aggressive flow often means your maker quote gets picked off adversely.

#### Book 112 — **GOOD** (opportunity)

| Field | Value |
|-------|-------|
| best_bid | 315.44 |
| best_ask | 315.50 |
| mid | 315.47 |
| spread_ticks | (315.50 − 315.44) / 0.01 = **6 ticks** ✓ |
| spread_ratio | 0.06 / 315.47 = 0.00019 < 0.0012 ✓ |
| maker_fee_rate | 0.0006 < max_fee_rate 0.0012 ✓ |
| tape | balanced enough |

**Capturable round-trip edge (`rt_edge_ticks`):**

```
inside_buy  = min(315.44 + 0.01, 315.50 - 0.01) = 315.45
inside_sell = max(315.50 - 0.01, 315.44 + 0.01) = 315.49
rt_edge     = (315.49 - 315.45) / 0.01 = 4 ticks  ✓  (meets min_rt_edge_ticks)
```

**Fill score (higher = quote here first):**

```
fill_score ≈ spread_ticks * 2.2 + tape_notional * 0.00004 + touch_depth * 0.35 + rebate_bonus
           ≈ 6 * 2.2 + small terms + 0.35 * min(5.1, 4.8) + ...
           ≈ 13.2 + ...   (ranked against other passing books)
```

**Rotation check:**

```
rot = (63780000000000 // 24000000000) % 16 = 10
active_rots = {10, 11, 12}
112 % 16 = 0  → not in active bucket this tick → skip NEW quote on 112 unless flattening
```

(Other books with `book_id % 16 in {10,11,12}` that pass filters get quoted this tick.)

#### Book 122 — **GOOD + in rotation**

| Field | Value |
|-------|-------|
| best_bid | 304.28 |
| best_ask | 304.36 |
| spread_ticks | 8 |
| rt_edge_ticks | 4 ✓ |
| 122 % 16 | 10 ✓ (in active_rots) |

### Step 2 — Inventory skew on book 42 (flatten candidate)

```
base free  = 0.40 BASE  (0.12 free + 0.28 in open sell order)
quote free = 412.30 QUOTE
mid        = 309.11

base_value = 0.40 * 309.11 = 123.64
total      = 123.64 + 412.30 = 535.94
skew       = (123.64 / 535.94) - 0.5 = -0.27
```

**Interpretation:** Portfolio on book 42 is **too long QUOTE / too short BASE** (skew negative but we use absolute value vs thresholds).

Actually skew = -0.27 means base_val/total is much less than 0.5 → too little BASE exposure.

Wait: `(base_val / total) - 0.5` = negative means less than half the value is in BASE → miner is **underweight BASE** → prefer **BUY** to rebalance.

For recover: `|skew| = 0.27 > inventory_skew_hard (0.028)` → **flatten** with inside limit BUY (or cross only at extreme; recover uses inside first).

### Step 3 — Cancel stale resting order on book 42

Existing order: SELL @ 309.20 while touch is 309.08 / 309.14.

**Rule:** sell price > best_ask + 0.5×tick → stale → **CANCEL** (uses 1 instruction from budget).

### Step 4 — Place new quotes (budget-aware)

Assume after filtering + rotation we have ranked candidates: **122, 58, 26, 74, 90, 106, 10**.

**Quantity (fixed this tick):**

```
qty = round(max(0.28, 0.30 * 0.90), 4) = round(0.27, 4) = 0.28 BASE
```

**Book 122 — SELL inside** (neutral skew, rotation bucket, highest fill_score among SELL candidates):

```
best_bid=304.28  best_ask=304.36
inside_sell = max(304.36 - 0.01, 304.28 + 0.01) = 304.35
base free on book 122 ≥ 0.28  → OK
→ SELL 0.28 @ 304.35 on book 122
```

**Book 58 — BUY inside:**

```
inside_buy = 301.12  (example)
quote free ≥ 0.28 * 301.12 = 84.31  → OK
→ BUY 0.28 @ 301.12 on book 58
```

**Budget after 1 cancel + 2 limits:**

```
total_instr = 3
by_book: {42: 1, 122: 1, 58: 1}
remaining slots: 16 - 3 = 13
```

Continue until `quote_count == 7` books or `total_instr == 16`.

### Step 5 — Miner log output (matches real pm2 format)

```
VALIDATOR : 5HYyPA43Hof3Lc9ubf6k3QC5TRAnRvGvfprae67ncWizXMnd | SIMULATION TIME : 17:43:00.000000000 (T=63780000000000)
--------------------------------------------------
INSTRUCTIONS
--------------------------------------------------
CANCEL ORDER #2110405 ON BOOK 42
BUY  0.28@309.09 ON BOOK 42
SELL 0.28@304.35 ON BOOK 122
BUY  0.28@301.12 ON BOOK 58
SELL 0.28@298.76 ON BOOK 69
...
--------------------------------------------------
```

### Step 6 — Response object sent to validator

```python
FinanceAgentResponse(
    agent_id=65,   # MUST equal UID
    instructions=[
        CancelOrdersInstruction(bookId=42, orderId=2110405),
        LimitOrderInstruction(bookId=42, side=BUY,  quantity=0.28, price=309.09, ...),
        LimitOrderInstruction(bookId=122, side=SELL, quantity=0.28, price=304.35, ...),
        LimitOrderInstruction(bookId=58, side=BUY,  quantity=0.28, price=301.12, ...),
        ...
    ]
)
```

### Step 7 — Next tick feedback (notices)

Validator/simulator executes instructions and on the **next** tick you receive:

```
BOOK 122 : PLACED SELL LIMIT ORDER #2111273 FOR 0.28@304.35 AT ...
BOOK 58  : PLACED BUY  LIMIT ORDER #2031918 FOR 0.28@304.43 AT ...
BOOK 122 : BUY TRADE #249234 : YOUR PASSIVE ORDER #2111273 MATCHED ... FOR 0.2247@304.35
```

That `TradeEvent` in `notices` is how you know a maker fill happened — and later, when you complete the opposite side, **realized PnL** and **Kappa** update.

---

## 5. How miners recognize bad books vs opportunity

Think of a **funnel**:

```
128 books
   │  empty book / no touch?
   ▼
 ~120 books with bid+ask
   │  spread_ratio > 0.0012?
   ▼
 ~80 books
   │  spread_ticks < 4?
   ▼
 ~45 books
   │  rt_edge_ticks < 4?  (inside buy/sell don't leave room after fees)
   ▼
 ~25 books
   │  |tape_imbalance| > 0.50?
   ▼
 ~18 books
   │  maker_fee_rate > max?
   ▼
 ~15 books
   │  book_id % 16 not in today's rotation bucket?
   ▼
 ~3–7 books quoted THIS tick (recover cap)
```

### Bad book cheat sheet (recover profile)

| Symptom | Example numbers | Action |
|---------|-------------------|--------|
| Too tight | bid 300.00 / ask 300.02 (2 ticks) | Skip |
| No capturable edge | inside buy 309.09 = inside sell 309.09 | Skip |
| Wide but ratio insane | spread 2.0 on mid 100 (2%) | Skip (`max_spread_ratio`) |
| One-sided tape | 15 sell trades, 1 buy in events | Skip |
| Bad fees | maker_fee 0.002 > cap 0.0012 | Skip |
| Wrong rotation | book 5 when `rot=10`, groups `{10,11,12}` | Skip new quotes |
| Has margin loan | `loans` non-empty | Repay first, don't quote |
| Volume cap hit | `traded_volume` over 24h cap | Cancel only |

### Opportunity = passes all filters + high `fill_score` + rotation slot

---

## 6. Rules, signals, and formulas

### Derived market metrics (miner computes)

| Metric | Formula | Example (book 112) |
|--------|---------|-------------------|
| **mid** | `(best_bid + best_ask) / 2` | 315.47 |
| **spread_ticks** | `(ask − bid) / tick_size` | 6 |
| **spread_ratio** | `spread / mid` | 0.00019 |
| **rt_edge_ticks** | `(inside_sell − inside_buy) / tick` | 4 |
| **inventory_skew** | `(base_val/total) − 0.5` | see §4 |
| **tape_imbalance** | `(buy_events − sell_events) / total` | −0.85 → bad |

### Price modes (never random)

| Mode | Buy price | Sell price | When used |
|------|-----------|------------|-----------|
| **inside** | `min(bid+tick, ask−tick)` | `max(ask−tick, bid+tick)` | Default quotes (recover) |
| **join_touch** | `best_bid` | `best_ask` | Disabled in recover |
| **cross** | `best_ask` (taker) | `best_bid` (taker) | Emergency flatten only |

### Side selection (recover)

```
if inventory_skew > +0.004  → prefer SELL (too much BASE)
if inventory_skew < −0.004  → prefer BUY  (too much QUOTE)
else                        → alternate by (book_id + rot) % 2
```

### Signals vs rules

| Type | Purpose | Example |
|------|---------|---------|
| **Hard rule** | Must pass or skip book | `spread_ticks >= 4` |
| **Soft signal** | Rank among valid books | `fill_score` |
| **Risk signal** | Flatten before new risk | `|inventory_skew| >= 0.012` |
| **Coverage signal** | Visit all 128 books over time | `book_id % 16 in active_rots` |

---

## 7. How size and price are chosen

### Size

Recover uses **almost fixed size** every tick:

```
qty = round(max(min_quantity, max_quantity * quantity_scale), volumeDecimals)
    = round(max(0.28, 0.30 * 0.90), 4)
    = 0.28 BASE
```

**Hard checks before placing:**

| Side | Balance check |
|------|---------------|
| BUY | `quote_balance.free >= qty * limit_price` |
| SELL | `base_balance.free >= qty` |
| Both | `qty >= 0.25` (sim minimum) |

No random sizing — size is capped low in recover to limit damage per round-trip.

### Price

Always from **touch + tick**, never mid ± random noise:

**Book 112 SELL inside example (from live-style data):**

```
best_bid = 315.44, best_ask = 315.50, tick = 0.01
inside_sell = max(315.50 - 0.01, 315.44 + 0.01) = max(315.49, 315.45) = 315.49
→ SELL 0.28 @ 315.49
```

Log line from production recover miner:

```
SELL 0.28@315.47 ON BOOK 112
```

(315.47 is the rounded inside price for that tick's touch.)

---

## 8. How the instruction budget works

Two limits apply: **agent self-budget** and **validator hard cap**.

### Agent budget (`_InstructionBudget`)

```python
max_instructions_per_book = 3   # recover
max_total_instructions    = 16  # recover
```

Every cancel, limit, or repay consumes 1 slot:

| Action | book 42 count | total |
|--------|---------------|-------|
| Cancel stale | 1 | 1 |
| Flatten BUY | 1 | 2 |
| New SELL quote | 1 | 3 |
| **Blocked** | 4th on same book | — |

```python
def can_place(book_id, n=1):
    if total + n > 16: return False
    if per_book[book_id] + n > 3: return False
    return True
```

### Validator caps (external)

| Cap | Limit | If exceeded |
|-----|-------|-------------|
| Per book | 5 instructions | Excess dropped |
| Response time | ~3.0 s | Entire response ignored |
| Volume (24 sim h) | `10 × miner_wealth` per book | New orders blocked |

**Priority order in recover** (spends budget wisely):

1. Startup cancel-all (up to 28 instr on restart)
2. Repay one loan (FIFO rotation)
3. Cancel stale orders
4. Flatten high skew
5. New inside quotes on top `fill_score` books

---

## 9. What miners send back (response)

```python
FinanceAgentResponse(
    agent_id=65,              # must match UID exactly
    instructions=[...]        # 0–16 in recover (≤5 per book at validator)
)
```

Instruction types used in recover:

| Type | When |
|------|------|
| `cancel_order` | Stale limits, startup cancel-all |
| `limit_order` | Maker quotes (GTC/GTT, post inside spread) |
| `close_position` | Loan repay (FIFO) |

**Not used in recover:** market orders, two-sided quoting, touch-join, momentum/reversion edge.

---

## 10. From this tick to a high score

One tick is tiny. Validators score **hours of round-trips**:

```
TradingScore = 0.79 × KappaScore + 0.21 × PnLScore
```

### What a good tick contributes

| This tick | Scoring impact |
|-----------|----------------|
| Maker SELL 0.28 @ 315.49 fills | Opens leg of round-trip |
| Later BUY inside completes RT | **Realized PnL** observation |
| Repeat on 50+ books over hours | **Kappa** per book (≥3 RTs) |
| Positive RT after fees | Kappa ↑ and PnL ↑ |
| Rotate all 128 books | Avoid outlier penalty |
| Stay under volume cap | Orders not blocked |

### End-to-end scoring timeline (example)

```
Hour 0   Deploy recover → cancel ghost orders → start quoting wide books
Hour 1   First round-trips complete → realized PnL moves (hopefully positive)
Hour 3   ≥3 RT observations per book → Kappa starts (min lookback ~1.5 sim h)
Hour 6   Median Kappa + PnL blended → score > 0
Hour 12+ Consistent positive RTs → climb placement, earn weight
```

### High-score checklist (what top miners do)

| # | Practice | Why |
|---|----------|-----|
| 1 | **Inside maker** on wide spreads | Capture spread − fees |
| 2 | **Complete round-trips** | Kappa needs realized RT PnL |
| 3 | **Cover many books** | Median aggregation + 37.5% inactive tolerance |
| 4 | **Avoid toxic books** | Tape imbalance → adverse selection |
| 5 | **Control inventory** | Skew → one-sided bleed |
| 6 | **Respect budget** | ≤5/book validator cap |
| 7 | **Respond < 3s** | Timeout = zero instructions |
| 8 | **Never cross spread** unless emergency | Touch-cross was our −500 realized bug |

**Top miner reference (T1704 snapshot):** UID 106 — realized +65k, κ=0.87, score≈21. Same protocol, better edge selection + consistent profitable RTs at scale.

---

## 11. Anti-patterns that killed our deploy miners

| Mistake | What happened | Score impact |
|---------|---------------|--------------|
| Touch-join (buy@ask, sell@bid) | Negative edge every RT | Realized −500, κ=None |
| 30 instr/tick, two-sided | 300k RT volume, all losing | Volume without quality |
| Ignore rotation | Same bad books every tick | Outlier penalty |
| Survive idle forever | No new RTs | Score stuck at 0 |
| Ghost resting orders | Passive fills after “stop” | Realized kept bleeding |

**Recover profile** was built specifically to avoid repeating rows 1–3 while still trading enough to escape score=0.

---

## Quick reference diagram

```
REQUEST (validator)                    RESPONSE (miner)
─────────────────────                  ──────────────────
timestamp: 63780000000000              agent_id: 65
books[128] with bids/asks/events  →    instructions:
accounts[65][128] balances/orders      [cancel stale,
notices[65] fills/rejects from T-1      limit sell book 122 @ 304.35,
config decimals/fees/limits              limit buy  book 58  @ 301.12,
                                         ... up to 16 total]
         │                                        │
         └──────── update() → respond() ─────────┘
                    │
                    ▼
              filter → rank → budget → price → qty
                    │
                    ▼
         NEXT TICK: notices tell you what filled
                    │
                    ▼
         SCORING (every ~5s sim): Kappa + PnL → weight
```

---

## See also

- [SN-79-explainer-for-beginners.md](./SN-79-explainer-for-beginners.md) — concepts and glossary  
- [SN-79-miner-validator-protocol.md](./SN-79-miner-validator-protocol.md) — field reference and limits  
- `agents/competitive_utils.py` — `turbo_recover_score_tick`, `book_fill_score`, `startup_cancel_all_orders`  
- `agents/_turbo_v2_agent_base.py` — `respond()` wiring

---

*Document version: 2026-06-08 · Example agent: TurboPulseV2 recover · UID 65*
