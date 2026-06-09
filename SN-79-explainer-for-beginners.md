# SN-79 (τaos) — Beginner's Guide to How Miners and Validators Work

> **Audience:** People with no prior knowledge of Bittensor, SN-79, or automated trading  
> **Subnet:** Bittensor netuid **79** (mainnet) / **366** (testnet)  
> **Software:** τaos package **0.4.5**  
> **Related deep dives:** [Extra Questions (FAQ)](./SN-79-extra-questions.md) · [Miner ↔ Validator Protocol](./SN-79-miner-validator-protocol.md) · [What market is this?](./SN-79-what-market-is-this.md) · [FAQ](./FAQ.md)

---

## Table of Contents

1. [What is SN-79 in plain English?](#1-what-is-sn-79-in-plain-english)
2. [What data do validators send to miners initially?](#2-what-data-do-validators-send-to-miners-initially)
3. [How do miners handle this data?](#3-how-do-miners-handle-this-data)
4. [Do miners generate orders randomly?](#4-do-miners-generate-orders-randomly)
5. [Main logic and concepts of miners](#5-main-logic-and-concepts-of-miners)
6. [How do validators score miners?](#6-how-do-validators-score-miners)
7. [How can miners increase their score?](#7-how-can-miners-increase-their-score)
8. [Glossary](#8-glossary)
9. [One complete tick — worked example](#9-one-complete-tick--worked-example)

**More Q&A:** [SN-79-extra-questions.md](./SN-79-extra-questions.md) — post-decompress tick workflow, cancel/buy/sell logic, why scores differ, how validators score trades, and how to raise **incentive**.

---

## 1. What is SN-79 in plain English?

**SN-79** is a Bittensor subnet where **miners compete as automated traders** in a **simulated financial market**.

Think of it like this:

| Real world | SN-79 equivalent |
|------------|------------------|
| Stock exchange | C++ **simulator** running 128 parallel order books |
| Your trading bot | **Miner** (Python agent you write and run) |
| Exchange operator | **Validator** (runs the sim, sends market data, scores miners) |
| Your profit | **Score** → Bittensor **weights** → TAO emissions |

**Important:** Miners do **not** trade on live Coinbase or Binance today. They trade a **synthetic BASE/QUOTE market** inside a computer simulation. Validators may *seed* the simulation with live BTC/TAO prices, but every order, fill, and PnL number miners see is **simulated**.

The subnet's goal is to find trading algorithms that are:

- **Profitable** after fees and spreads
- **Risk-adjusted** (consistent, not just lucky)
- **Active across many books** (not only one corner of the market)
- **Fast enough** to respond before a timeout

---

## 2. What data do validators send to miners initially?

### 2.1 The big picture

Every ~1 **simulation second** (configurable), the validator pauses the simulator and sends every miner a **state update** — a snapshot of the world. This message is called **`MarketSimulationStateUpdate`**.

```
Simulator (C++)  →  Validator (Python)  →  Miner (your agent)
                         │
                         └── compressed MarketSimulationStateUpdate
```

The payload is usually **compressed** (default: LZ4) because it is large — up to **128 order books**, each with bid/ask depth and recent events.

### 2.2 What is inside the state update?

| Field | What it is | Why miners need it |
|-------|------------|-------------------|
| **`timestamp`** | Simulation clock in **nanoseconds** since sim start | Timing, cadence, order expiry |
| **`config`** | Rules of the simulation | Decimals, fees, wealth, book count, limits |
| **`books`** | All order book snapshots | Prices, spreads, liquidity, recent trades |
| **`accounts`** | Your balances and open orders **per book** | Know what you can afford to trade |
| **`notices`** | Events that happened to **you** since the last tick | Fills, rejections, cancellations |
| **`version`** | Validator software version | Compatibility (optional) |
| **`dendrite`** | Which validator sent the request | Logging / multi-validator awareness |

Miners do **not** receive a random puzzle. They receive a **complete, structured trading environment** — like a trading terminal snapshot plus your portfolio plus a trade blotter.

### 2.3 Order book data (`books`)

Each book is one independent market (there are typically **128** of them). For each book ID, you get:

| Piece | Meaning |
|-------|---------|
| **`bids`** | Buy orders, **best (highest) price first** |
| **`asks`** | Sell orders, **best (lowest) price first** |
| **`events`** | What changed since last tick: new orders, trades, cancels |

From the touch (best bid and best ask), miners typically compute:

- **Mid price** = `(best_bid + best_ask) / 2`
- **Spread** = `best_ask - best_bid`
- **Spread ratio** = `spread / mid`

Validators **do not** send pre-computed mid or signals — miners derive those themselves.

### 2.4 Your portfolio (`accounts[your_uid][book_id]`)

Capital is **split across all books**. For each book you may have:

| Field | Meaning |
|-------|---------|
| **`base_balance`** | How much BASE you hold (`free`, `reserved`, `total`) |
| **`quote_balance`** | How much QUOTE (cash) you hold |
| **`orders`** | Your resting limit orders on this book |
| **`loans` / `base_loan` / `quote_loan`** | Margin borrowed (leverage) |
| **`fees`** | Maker/taker fee rates you currently pay |
| **`traded_volume`** | Cumulative volume (used for volume caps) |

**Reserved** balance is locked in open orders. **Free** balance is what you can use for new orders.

Typical starting wealth: about **50,000 QUOTE** total, divided across books (~390 QUOTE per book if spread evenly across 128 books).

### 2.5 Feedback from last tick (`notices`)

`notices` is **not** optional decoration — it tells you what actually happened after your previous instructions:

| Notice type | Tells you |
|-------------|-----------|
| **`TradeEvent`** | You were filled (maker or taker), at what price/qty |
| **`LimitOrderPlacementEvent`** | Your limit order was accepted or **rejected** |
| **`MarketOrderPlacementEvent`** | Your market order result |
| **`OrderCancellationsEvent`** | Cancel succeeded or failed |
| **`SimulationStartEvent` / `SimulationEndEvent`** | Simulation lifecycle |

Example rejection reasons: `EXCEEDING_LOAN`, `MINIMUM_ORDER_SIZE_VIOLATION`, insufficient balance.

**Key idea:** `books` = what the market looks like **now**. `notices` = what happened to **your orders** since last time.

### 2.6 Simulation config (`config`) — rules you must obey

| Setting | Typical value | Effect |
|---------|---------------|--------|
| `book_count` | 128 | How many parallel markets |
| `priceDecimals` | 2 | Round prices (e.g. 300.12) |
| `volumeDecimals` | 4 | Round quantities (e.g. 0.2800) |
| `miner_wealth` | ~50,000 | Starting QUOTE; used in volume cap |
| `max_open_orders` | Per-agent limit | Too many resting orders → reject |
| `publish_interval` | ~1e9 ns (1 sim sec) | How often you get updates |
| `fee_policy` | Tiered schedule | Maker rebates / taker fees |

Minimum order size is commonly **0.25 BASE**. Orders below that are rejected by the simulator.

---

## 3. How do miners handle this data?

### 3.1 Miner software stack

A miner is two layers:

1. **Neuron** (`taos/im/neurons/miner.py`) — Bittensor networking: receives synapse, decompresses, calls your agent, returns response within timeout (~**3 seconds**).
2. **Agent** (`agents/*.py`) — **Your strategy**: reads state, decides orders.

You implement (or use) an **agent class** that subclasses `FinanceSimulationAgent`.

### 3.2 The mandatory tick lifecycle

Every time a validator query arrives, the agent runs:

```
handle(state):
    1. update(state)    ← ingest books, accounts, notices
    2. respond(state)   ← YOUR STRATEGY: build orders
    3. report(state)    ← logging
    return FinanceAgentResponse
```

#### Step 1: `update()` — understand the world

The base class automatically:

- Stores recent history (default last 10 ticks)
- Sets `self.accounts = state.accounts[your_uid]`
- Sets `self.events = state.notices[your_uid]`
- Dispatches handlers: `onTrade`, `onOrderAccepted`, `onOrderRejected`, etc.

Smart agents use `onTrade` to remember fills (e.g. "I got filled on book 42 as a maker — queue a completion leg").

#### Step 2: `respond()` — decide what to do

This is where **all trading logic lives**. The agent:

1. Reads current `state.books`
2. Reads `self.accounts` per book
3. Optionally uses history, notices, internal signals
4. Builds a `FinanceAgentResponse` with 0 or more **instructions**

#### Step 3: Return instructions to validator

```python
FinanceAgentResponse(
    agent_id=your_uid,      # MUST match your UID exactly
    instructions=[...]    # limit orders, cancels, market orders, etc.
)
```

### 3.3 What miners send back (instruction types)

| Instruction | What it does |
|-------------|--------------|
| **Limit order** | Post a price; rests on book until filled or cancelled (usually **maker**) |
| **Market order** | Buy/sell immediately at best available price (**taker**, pays spread + fees) |
| **Cancel orders** | Remove resting orders |
| **Close position(s)** | Repay margin loans |

### 3.4 Hard limits on miner responses

| Limit | Default | If violated |
|-------|---------|-------------|
| Response time | ~3.0 s | **Entire response ignored** — zero orders |
| Instructions per book | 5 | Excess dropped |
| `agent_id` | Must equal UID | All instructions discarded |
| Volume cap per book | `10 × miner_wealth` QUOTE / assessment window | New orders blocked (cancels still OK) |

### 3.5 What happens after the miner responds

```
Miner response
    → Validator validates (UID, limits, volume cap)
    → Latency delay applied (slow miners get worse execution timing)
    → C++ simulator executes orders
    → Next tick: results appear in notices
```

Failed orders **do not score**. Only executed trades matter.

---

## 4. Do miners generate orders randomly?

**No — competitive miners do not trade randomly.** Random orders would lose money to fees and spreads and would score poorly.

Orders are generated from **rules and signals** based on the state update. The exact rules depend on the agent, but serious miners follow the same conceptual pipeline:

```
Market data + portfolio + past fills
        ↓
   Filter (skip bad books)
        ↓
   Signal (where is opportunity / risk?)
        ↓
   Size & price (how much, at what limit price?)
        ↓
   Budget check (instruction limits, balance, loans)
        ↓
   FinanceAgentResponse
```

### 4.1 Example decision inputs (not random)

| Input | Used for |
|-------|----------|
| Best bid / best ask | Quote placement, spread check |
| Spread ratio | Skip books that are too wide (costly to trade) |
| Mid price vs history | Momentum or mean-reversion signals |
| Inventory skew | If holding too much BASE, prefer selling |
| Maker fee rate | Skip books where fees are unfavorable |
| Open orders | Cancel stale quotes before re-posting |
| Loan status | Repay margin before new risk |
| Rotation bucket | Visit different books over time (coverage) |
| Tape imbalance | Skip books with one-sided aggressive flow |

### 4.2 Example agents in this repo (all rule-based)

| Agent family | Style |
|--------------|-------|
| **Turbo v1** (`TurboPulseAgent`, etc.) | Maker limits inside spread, two-sided quotes, book rotation |
| **Turbo v2** | v1 + stale cancel, fill-score ranking, post-fill requote |
| **Survive mode** | Emergency: minimal trading, wide spreads only, flatten inventory, no requote |

The bundled example `RandomMakerAgent` (if present in tutorials) is for **testing infrastructure**, not for earning emissions.

### 4.3 What "random" would look like (and why it fails)

A random miner might: pick a random book, random buy/sell, random price. That causes:

- Crossing the spread accidentally → pay taker fees
- Trading on illiquid books → bad fills
- Ignoring loan limits → `EXCEEDING_LOAN` rejections
- No consistent round-trips → **Kappa score stays 0**

---

## 5. Main logic and concepts of miners

### 5.1 Core economic idea: round-trips

Scoring is based on **completed round-trip trades** — buy then sell (or sell then buy) on a book, realizing PnL.

```
Open leg  →  Close leg  =  Round-trip
   ↓              ↓
 maker/taker   opposite side
   ↓              ↓
        Realized PnL (after fees)
```

Standing limit orders that **never fill** contribute **nothing** to score.

### 5.2 Maker vs taker

| Role | Behavior | Cost profile |
|------|----------|--------------|
| **Maker** | Post limit order that **rests** on the book | Often pays lower fee or earns rebate |
| **Taker** | Market order or aggressive limit that **crosses** spread | Pays spread + usually higher fee |

High-scoring strategies usually emphasize **maker limits** on clean books, using taker orders sparingly (e.g. to flatten risk).

### 5.3 Multi-book rotation

There are **128 books**. You cannot deeply trade all of them every tick (instruction limits + timeout).

Miners use **rotation**:

- Divide simulation time into **cadence buckets** (e.g. 16–20 groups)
- Each tick, focus on books whose bucket matches current time
- Over hours, cover all books with round-trip activity

This matches validator scoring: you need activity on many books, but you may skip up to **37.5%** worst books without penalty.

### 5.4 Inventory and risk management

If you only buy, you accumulate BASE and run out of QUOTE. Miners track **inventory skew**:

```
skew ≈ (value in BASE) / (total value) - 0.5
```

- Positive skew → too long BASE → prefer sell quotes
- Negative skew → too long QUOTE → prefer buy quotes

**Survive mode** aggressively flattens skew before placing new quotes.

### 5.5 Loan / leverage management

Miners can use leverage, but loans must stay under `max_loan` (~10,000 QUOTE per book). Exceeding limits → rejections → no fills → no score.

Good agents **repay loans first** (FIFO settlement) before opening new positions.

### 5.6 Instruction budgeting

With only **5 instructions per book** and ~**28 total per tick**, agents prioritize:

1. Cancel stale orders (free capacity)
2. Repay loans / flatten risk
3. Best-scoring books first (widest spread, good fees, fill probability)
4. One or two limit quotes per book

### 5.7 Turbo v2 scoring engine (typical high-performance flow)

```
1. Repay any rotating margin loan
2. Scan all books → compute mid, spread, fees
3. Filter: spread too wide, fees too high, imbalanced tape
4. Rank remaining books by fill score
5. Cancel off-touch resting orders (budget-limited)
6. Place maker limits inside spread (one- or two-sided)
7. On prior maker fill (onTrade): prioritize opposite-side completion quote
```

### 5.8 Survive mode (capital preservation)

When realized PnL is bleeding, survive mode trades **much less**:

- Max **3 books** per tick, **5 instructions** total
- Only books with **wide spread** (≥ 5 ticks) and capturable edge (≥ 3 ticks)
- **No** two-sided quoting, **no** requote, **no** momentum/reversion edge
- Cancel stale orders → flatten skewed inventory → single careful limit

---

## 6. How do validators score miners?

### 6.1 High-level formula

Default **trading score** (95% of rewards when GenTRX pool is inactive):

```
TradingScore = 0.79 × KappaScore + 0.21 × PnLScore
```

Both components use **long lookbacks** over simulation time (roughly **1.5–3 sim hours** for Kappa components; PnL uses a similar window framed as daily return).

Scores are smoothed over time with an **exponential moving average** before being turned into on-chain **weights**.

### 6.2 What is Kappa-3?

**Kappa-3 (κ₃)** is a **risk-adjusted return** metric based on **realized PnL from round-trips**:

\[
\kappa_3(\tau) = \frac{\mu - \tau}{\text{LPM}_3(\tau)^{1/3}}
\]

Where:

- **μ** = mean return per round-trip observation
- **τ** = target threshold (default 0)
- **LPM₃** = third lower partial moment (downside risk — big losses hurt a lot)

**Intuition:** Steady small profits with few blowups score high. Lucky one-off gains with volatile losses score low.

**Per book:** Kappa is computed separately for each book where enough data exists.

**Minimum data:** At least **3 realized round-trip observations** in the lookback window, or Kappa for that book is undefined.

### 6.3 Kappa score aggregation (per miner)

For each miner, the validator:

1. **Normalizes** per-book Kappa to a 0–1 range
2. Multiplies by **activity factor** (round-trip volume vs cap; decay if inactive)
3. Optionally multiplies by **PnL factor** (boost/penalize profitable books)
4. Allows up to **37.5%** of books to have no Kappa without penalty
5. Applies **outlier penalty** if some books are much worse than the median
6. Takes the **median** across scored books → **KappaScore**

**Median** means one great book cannot fully mask terrible books.

### 6.4 Realized PnL score (21% weight)

Measures **absolute profitability**:

- Sum **realized PnL per book** over lookback
- Convert to **daily return** vs allocated capital (`miner_wealth / book_count`)
- Take **median** across books (same philosophy as Kappa)
- Map to range **[-0.5, +0.5]** where 0 = breakeven

### 6.5 What does NOT affect score

| Action | Score effect |
|--------|--------------|
| Orders that fail / reject | **Zero** — no trade, no data |
| Response timeout | **Zero** instructions that tick |
| Unrealized PnL (open positions) | **Not used** in Kappa/PnL scoring |
| High volume of losing trades | **Hurts** PnL and likely Kappa |
| Cancel spam without fills | No direct penalty today, but wastes budget |

### 6.6 Volume cap (activity limit)

To prevent spam-trading, each book tracks QUOTE volume over a rolling **24 sim-hour** window. If you exceed:

```
cap = capital_turnover_cap × miner_wealth   (default cap multiplier: 10)
```

…new orders on that book are **blocked** until volume rolls off. Cancellations still work.

### 6.7 From score to TAO

```
Per-tick Kappa/PnL  →  TradingScore  →  EMA smoothing  →  Weights  →  Emissions
```

Validators set on-chain weights; higher weight → larger share of subnet emissions.

### 6.8 Monitoring

Public dashboards:

- Mainnet: [taos.simulate.trading](https://taos.simulate.trading)
- Testnet: [testnet.simulate.trading](https://testnet.simulate.trading)

Key columns: **Kappa**, **Kappa Penalty**, **Realized PnL**, **Round-trip volume**, per-book activity.

---

## 7. How can miners increase their score?

### 7.1 The honest summary

**You cannot "game" score with noise.** You need:

1. **Profitable round-trips** (realized PnL > 0 after fees)
2. **Low downside volatility** in those returns (high κ₃)
3. **Enough activity** on enough books (but not reckless volume)
4. **Consistency** across books (low outlier penalty)
5. **Reliable infrastructure** (no timeouts, no reject storms)

### 7.2 Practical strategies

| Goal | What to do |
|------|------------|
| **Get Kappa off zero** | Complete ≥3 round-trips per book in lookback window |
| **Raise κ₃ quality** | Prefer maker fills inside wide spreads; avoid churn losses |
| **Raise PnL score** | Positive realized PnL on median book |
| **Avoid penalty** | Do not ignore a subset of books while losing heavily on others |
| **Stay active** | Rotate across books; avoid long idle gaps (activity decay) |
| **Avoid volume cap** | Do not overtrade; cap is ~10× wealth per 24 sim hours per book |
| **Reduce rejections** | `quantity ≥ 0.25`, check balances, repay loans, ≤5 instr/book |
| **Reduce timeouts** | Fast code (`lazy_load=1`), avoid heavy logging every tick |
| **Reduce latency penalty** | Host near validators; respond in <1s when possible |

### 7.3 Order placement principles that align with scoring

1. **Pre-check** balances and loan headroom before submitting
2. **Cancel stale** quotes before posting new ones (same budget slot)
3. **Quote inside the spread** as maker (not crossing unless flattening)
4. **Complete round-trips** after maker fills (sell after buy, or vice versa)
5. **Rotate books** so coverage builds over hours, not all in one tick
6. **Skip toxic books** — wide spread, bad fees, extreme tape imbalance
7. **Control inventory** — do not drift to one-sided exposure

### 7.4 Common mistakes (seen on live miners)

| Mistake | Symptom | Score impact |
|---------|---------|--------------|
| Market orders on many books with leverage | `EXCEEDING_LOAN` spam | No fills |
| Quantity < 0.25 | `MINIMUM_ORDER_SIZE_VIOLATION` | No fills |
| >5 instructions/book | Validator drops excess | Missed trades |
| Slow Python / debug logging | Timeout | Zero orders entire tick |
| Aggressive touch-crossing | Negative realized PnL | κ₃ and PnL drop |
| Overtrading | Volume cap hit | Frozen on book 24h+ |

### 7.5 Development path

1. Read this guide and the [protocol reference](./SN-79-miner-validator-protocol.md)
2. Test locally with the **proxy validator** (no chain required)
3. Deploy to **testnet** (netuid 366), monitor dashboard
4. Optimize for stable positive realized PnL before maximizing volume
5. Deploy to **mainnet** (netuid 79)

**Note:** Example agents in `/agents` are educational starting points — competitive mainnet requires a tuned custom strategy.

---

## 8. Glossary

| Term | Definition |
|------|------------|
| **Agent** | Python class implementing trading logic |
| **BASE** | The asset being bought/sold (like "the coin") |
| **QUOTE** | The currency paying for BASE (like "USD") |
| **Book** | One order book (one market instance) |
| **UID** | Your miner's unique ID on the subnet (0–255) |
| **Validator** | Node running simulation + scoring |
| **Synapse** | Bittensor message (`MarketSimulationStateUpdate`) |
| **Instruction** | One action: limit order, cancel, etc. |
| **Round-trip** | Open + close trade on a book; realizes PnL |
| **Realized PnL** | Profit/loss from **completed** trades (fees included) |
| **Unrealized PnL** | Paper gain/loss on open positions (not scored) |
| **Kappa-3** | Risk-adjusted return metric on round-trip PnL |
| **Maker** | Liquidity provider (resting limit) |
| **Taker** | Liquidity taker (immediate execution) |
| **Publish interval** | Time between state updates (~1 sim second) |
| **Simulation time** | Internal clock of the C++ engine (≠ wall clock) |

---

## 9. One complete tick — worked example

**Scenario:** UID 65, simulation time T = 61,742,000,000,000 ns.

### Validator → Miner (simplified)

```yaml
timestamp: 61742000000000
config:
  book_count: 128
  priceDecimals: 2
  volumeDecimals: 4
  miner_wealth: 50000
books:
  42:
    bids: [{price: 299.50, quantity: 12.5}, ...]
    asks: [{price: 299.80, quantity: 8.1}, ...]
    events: [TradeInfo(...), Order(...)]
accounts:
  65:
    42:
      quote_balance: {free: 412.30, reserved: 50.00}
      base_balance: {free: 0.15, reserved: 0.00}
      orders: [{id: 991, side: BUY, price: 299.45, quantity: 0.28}]
notices:
  65:
    - TradeEvent(bookId=42, makerAgentId=65, side=SELL, price=299.55, quantity=0.28)
```

### Miner agent logic

1. **update:** Sees TradeEvent — you were filled as maker on a sell. `onTrade` queues "need BUY completion on book 42."
2. **respond:**
   - Cancel stale buy at 299.45 if off-touch
   - Place BUY limit at 299.52 (inside spread) to complete round-trip
   - Rotate to books 17 and 88 for new one-sided quotes (survive mode: only if spread ≥ 5 ticks)
3. **return:** `FinanceAgentResponse(agent_id=65, instructions=[cancel, limit, limit])`

### Validator → Simulator

- Validates ≤5 instr/book, volume cap OK
- Applies 50ms latency delay (example)
- Simulator matches orders, generates next notices

### Scoring (later, over thousands of ticks)

- This round-trip's realized PnL feeds into κ₃ for book 42
- Volume adds to activity factor
- After enough books and observations, KappaScore and PnLScore rise → higher weight

---

## Quick reference diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     EACH SIMULATION TICK                          │
├─────────────────────────────────────────────────────────────────┤
│  IN:  books (128 markets) + accounts + notices + config       │
│        ↓                                                        │
│  MINER: update → respond (rule-based strategy)                  │
│        ↓                                                        │
│  OUT: FinanceAgentResponse (limits / cancels / markets)         │
│        ↓                                                        │
│  VALIDATOR: validate → latency → C++ simulator execute          │
│        ↓                                                        │
│  NEXT TICK: notices tell you what filled or failed              │
│        ↓                                                        │
│  EVERY ~5s sim: score = 0.79×Kappa + 0.21×PnL → weights         │
└─────────────────────────────────────────────────────────────────┘
```

---

*Document version: 2026-06-08 · τaos 0.4.5 · SN-79 mainnet netuid 79*
