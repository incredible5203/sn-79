# SN-79 Miner ↔ Validator Protocol Reference

> **Scope:** Trading miners on netuid **79** (mainnet) / **366** (testnet)  
> **Package:** τaos **0.4.5**  
> **Primary types:** `MarketSimulationStateUpdate` (validator → miner), `FinanceAgentResponse` (miner → validator)  
> **Sources:** `taos/im/protocol`, `taos/im/agents`, `taos/im/validator/query.py`, `taos/im/validator/reward.py`, `agents/README.md`  
> **See also:** [Market data from validators](./SN-79-market-data-from-validators.md) · [Validator & simulator internals](./SN-79-validator-and-simulator-internals.md) · [What market is this?](./SN-79-what-market-is-this.md)

---

## Table of Contents

1. [End-to-end process sequence](#1-end-to-end-process-sequence)
2. [What validators send: `MarketSimulationStateUpdate`](#2-what-validators-send-marketsimulationstateupdate)
3. [Market data in detail](#3-market-data-in-detail)
4. [Account and notice data](#4-account-and-notice-data)
5. [What miners must do each tick](#5-what-miners-must-do-each-tick)
6. [What miners send back: `FinanceAgentResponse`](#6-what-miners-send-back-financeagentresponse)
7. [Validator pre-simulator validation](#7-validator-pre-simulator-validation)
8. [Simulator execution and feedback](#8-simulator-execution-and-feedback)
9. [Scoring: what counts and what does not](#9-scoring-what-counts-and-what-does-not)
10. [Failed orders → more score?](#10-failed-orders--more-score)
11. [Mandatory constraints checklist](#11-mandatory-constraints-checklist)
12. [Practical miner design implications](#12-practical-miner-design-implications)

---

## 1. End-to-end process sequence

Each **simulation tick** (every `config.publish_interval` nanoseconds of sim time) follows this order:

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│ C++ Simulator│────▶│  Validator   │────▶│    Miner     │────▶│  Validator   │
│ (taosim)     │     │  (Python)    │     │  (agent)     │     │  (validate)  │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
       ▲                    │                    │                    │
       │                    │  compressed        │  FinanceAgent      │
       │                    │  MarketSimulation  │  Response          │
       │                    │  StateUpdate       │  (instructions[])  │
       │                    ▼                    ▼                    ▼
       │              Parallel query          handle()            Drop invalid /
       │              all UIDs (~3s           update()            excess instr.
       │              timeout)                respond()           Apply latency
       │                                        report()            delay
       └──────────────── execute validated instructions ─────────────┘
```

### Step-by-step (mandatory order)

| Step | Actor | Action |
|------|-------|--------|
| 1 | Simulator | Advances matching engine until next publish event; **pauses** while miners act |
| 2 | Validator | Builds `MarketSimulationStateUpdate`: books, accounts, notices, config, timestamp |
| 3 | Validator | Compresses payload (default **lz4**), queries all miner axons in parallel |
| 4 | Miner | Receives synapse on axon; **decompresses** if `compressed` is set |
| 5 | Miner | `handle()` → `update()` → `respond()` → `report()` (see §5) |
| 6 | Miner | Returns `FinanceAgentResponse` attached to synapse (≤ **3.0s** typical timeout) |
| 7 | Validator | Validates response: UID match, decompress, instruction limits, volume caps |
| 8 | Validator | Applies **latency delay** to instructions based on response time |
| 9 | Simulator | Executes accepted instructions; generates fills, rejects, cancellations |
| 10 | Validator | Every `scoring.interval` (~5s sim): updates Kappa, PnL, activity, weights |

**Critical timing rule:** Miners can only act at publish events — not continuously. Strategy timescale ≥ `publish_interval / 1e9` seconds (often **1 sim second**).

---

## 2. What validators send: `MarketSimulationStateUpdate`

Defined in `taos/im/protocol/__init__.py`. This is the full payload miners receive each tick.

| Field | Type | Mandatory | Description |
|-------|------|-----------|-------------|
| `version` | `int \| None` | Optional | Validator **taos** package version (compatibility) |
| `timestamp` | `int` | **Yes** | Sim time in **nanoseconds** since simulation start |
| `config` | `MarketSimulationConfig \| str \| None` | **Yes** | Simulation parameters (decimals, fees, limits, wealth) |
| `books` | `dict[int, Book] \| None` | **Yes** | Order book ID → L2/L3 book snapshot + recent events |
| `accounts` | `dict[int, dict[int, Account]] \| None` | **Yes** | UID → (book_id → your account on that book) |
| `notices` | `dict[int, list[FinanceNotice]] \| None` | **Yes** | UID → events since **last** update (fills, rejects, etc.) |
| `response` | `FinanceAgentResponse \| None` | Miner fills | Mutable; miner attaches response here |
| `compressed` | `str \| dict \| None` | Wire format | Compressed blob when sent over network |
| `compression_engine` | `str` | Wire format | `lz4`, `zlib`, or `zstd` (default **lz4**) |
| `dendrite` | (Bittensor) | **Yes** | Identifies querying validator (hotkey, IP, etc.) |

### Config fields miners must respect

From `config` (`MarketSimulationConfig`):

| Field | Why it matters |
|-------|----------------|
| `book_count` | Number of parallel order books (e.g. 128) |
| `book_levels` | Depth of L2 snapshot per side (e.g. 21 levels) |
| `detailed_book_levels` | Top N levels include per-order breakdown |
| `publish_interval` | Nanoseconds between state updates |
| `priceDecimals` | Round limit prices to this precision |
| `volumeDecimals` | Round order quantities to this precision |
| `baseDecimals` / `quoteDecimals` | Balance precision |
| `miner_wealth` | Initial QUOTE capital → used for **volume cap** |
| `max_open_orders` | Max resting orders per agent per book |
| `max_leverage` / `max_loan` | Margin limits (default max loan ~10,000 QUOTE per book) |
| `fee_policy` | Tiered maker/taker fee schedule |
| `simulation_id` | Identifies current sim run (resets on config change) |

---

## 3. Market data in detail

### 3.1 Per-book snapshot (`Book`)

Each `books[book_id]` contains:

| Field | Content |
|-------|---------|
| `id` | Book identifier (0 … book_count−1) |
| `bids` | List of `LevelInfo`, **best bid first** (descending price) |
| `asks` | List of `LevelInfo`, **best ask first** (ascending price) |
| `events` | Incremental L3 events since last publish (orders, trades, cancels) |

### 3.2 Price level (`LevelInfo`)

| Field | Meaning |
|-------|---------|
| `price` | Level price |
| `quantity` | Total size at this level (BASE) |
| `orders` | *(Top `detailedDepth` levels only)* List of individual `Order` objects |

### 3.3 Book events (`events[]`)

Events since the previous snapshot — use for microstructure signals and confirming your orders:

| Event type | Key fields | Use |
|------------|------------|-----|
| **Order** | `id`, `side`, `price`, `quantity`, `timestamp` | New order placed on book |
| **TradeInfo** | `taker_id`, `maker_id`, `quantity`, `price`, `side`, fees | Trade occurred |
| **Cancellation** | `orderId`, `price`, `quantity`, `timestamp` | Order removed |

TradeInfo includes `taker_agent_id` / `maker_agent_id` — identify your fills vs others.

### 3.4 Derived market metrics (miner-computed)

Validators do **not** send these; agents typically compute:

- **Mid:** `(best_bid + best_ask) / 2`
- **Spread:** `best_ask - best_bid`, spread ratio `spread / mid`
- **Microprice:** volume-weighted touch price
- **Cross-book median mid:** for relative-value strategies across books
- **Return since last tick:** `(mid - prev_mid) / prev_mid`

---

## 4. Account and notice data

### 4.1 Accounts (`accounts[your_uid][book_id]`)

Your portfolio **per book** (capital is split across books):

| Field | Meaning |
|-------|---------|
| `base_balance` | `{ total, free, reserved }` in BASE |
| `quote_balance` | `{ total, free, reserved }` in QUOTE |
| `base_loan` / `quote_loan` | Outstanding margin borrowed |
| `base_collateral` / `quote_collateral` | Posted collateral |
| `orders` | Your open resting orders on this book |
| `loans` | `order_id → Loan` for leveraged positions |
| `fees` | `{ volume_traded, maker_fee_rate, taker_fee_rate }` |
| `traded_volume` | Cumulative volume (validator may inject `v` for cap tracking) |

**Reserved** balance is locked in open orders; **free** is what you can use for new orders.

### 4.2 Notices (`notices[your_uid][]`)

**Feedback from the previous tick** — what actually happened to your instructions:

| Notice type | When | Key fields |
|-------------|------|------------|
| `LimitOrderPlacementEvent` | Limit placed | `bookId`, `orderId`, `success`, `message`, `price`, `quantity` |
| `MarketOrderPlacementEvent` | Market placed | Same + immediate fill info |
| `OrderCancellationsEvent` | Cancel batch | Per-order `success`, `message` |
| `TradeEvent` | Your order traded | `takerAgentId`, `makerAgentId`, `price`, `quantity`, fees |
| `ClosePositionsEvent` | Loan close | Success/failure per position |
| `SimulationStartEvent` / `SimulationEndEvent` | Sim lifecycle | Reset / shutdown |
| `ResetAgentsEvent` | UID deregistered | Account wiped |

**Failed placement notices** include `success=false` and `message` (e.g. `EXCEEDING_LOAN`, `MINIMUM_ORDER_SIZE_VIOLATION`).

Miners should process notices in `update()` before deciding the next `respond()`.

---

## 5. What miners must do each tick

Agent lifecycle (`taos/common/agents/__init__.py` + `taos/im/agents/__init__.py`):

```
handle(state):
  1. update(state)     ← ingest books, accounts, notices; log events
  2. respond(state)    ← build FinanceAgentResponse (YOUR STRATEGY)
  3. report(state, response)  ← log instructions submitted
  return response
```

### `update(state)` — ingest (mandatory base behavior)

- Append state to rolling `history` (last 10 ticks)
- Set `self.accounts = state.accounts[self.uid]`
- Set `self.events = state.notices[self.uid]`
- Set `self.simulation_config = state.config`
- Log validator hotkey + sim time
- Dispatch event handlers: `onOrderAccepted`, `onTrade`, `onOrderRejected`, etc.

### `respond(state)` — decide (your strategy)

- Read **current** `state.books` and **your** `self.accounts`
- Optionally use `self.events` / history for signals
- Return `FinanceAgentResponse(agent_id=self.uid)` with zero or more instructions

### Performance requirement

- Target **< 3 seconds** wall time per tick (decompress + logic + serialize)
- Timeout → **zero instructions executed** for that tick (see §9)

---

## 6. What miners send back: `FinanceAgentResponse`

Defined in `taos/im/protocol/response.py`.

```python
FinanceAgentResponse(
    agent_id: int,           # MUST equal your UID
    instructions: list,      # max ~200_000 total (practical limit much lower)
)
```

### Instruction types

| Type | API method | Purpose |
|------|------------|---------|
| **Market order** | `response.market_order(...)` | Immediate execution at best available price (taker) |
| **Limit order** | `response.limit_order(...)` | Resting order at price (maker if not crossing) |
| **Cancel orders** | `response.cancel_orders(...)` | Remove resting orders |
| **Close position** | `response.close_position(...)` | Close one leveraged position |
| **Close positions** | `response.close_positions(...)` | Close multiple leveraged positions |

### Common order parameters

| Parameter | Notes |
|-----------|-------|
| `book_id` | 0 … book_count−1 |
| `direction` | `OrderDirection.BUY` or `SELL` |
| `quantity` | BASE size; respect `volumeDecimals` |
| `price` | Limit only; respect `priceDecimals` |
| `leverage` | 0 = no borrow; `(1+leverage)×quantity` effective size |
| `settlement_option` | `NONE`, `FIFO`, or specific order ID — repay loans from proceeds |
| `timeInForce` | GTC, GTT (+ `expiryPeriod`), IOC, FOK |
| `postOnly` | Reject if would immediately match |
| `stp` | Self-trade prevention mode |
| `delay` | Extra sim-ns delay (added to latency penalty) |

### Response limits (mandatory)

| Limit | Default | Consequence if exceeded |
|-------|---------|-------------------------|
| **Instructions per book** | **5** | Excess instructions **dropped** by validator |
| **Response timeout** | **3.0s** | Entire response ignored |
| **Volume cap per book** | `10 × miner_wealth` QUOTE (assessment window) | New orders on that book **blocked** (cancels OK) |
| **agent_id** | Must equal UID | All instructions **discarded** |

---

## 7. Validator pre-simulator validation

Before instructions reach the C++ matching engine (`taos/im/validator/query.py`):

1. **Timeout / network failure** → no instructions
2. **Decompress failure** → no instructions
3. **`response.agent_id != uid`** → all instructions discarded
4. **Invalid book ID** → that instruction skipped
5. **Volume cap hit** on book → new orders skipped (not cancels)
6. **> max_instructions_per_book** on same book → excess dropped
7. **Malformed instruction** → skipped with warning

Only **validated** instructions are forwarded to the simulator.

---

## 8. Simulator execution and feedback

The C++ engine (`OrderPlacementValidator.cpp`) may **reject** orders even after validator acceptance:

| Error code | Typical cause |
|------------|----------------|
| `MINIMUM_ORDER_SIZE_VIOLATION` | Quantity below sim minimum (often **0.25** BASE) |
| `EXCEEDING_LOAN` | Margin loan would exceed `max_loan` |
| Insufficient balance | `free` base/quote too low |
| `postOnly` violation | Limit would cross spread |
| Max open orders | Too many resting orders on book |

Rejections appear in **next tick's `notices`** as failed placement events and in miner logs as `FAILED TO PLACE ...`.

**Latency delay:** Slow miner responses get additional execution delay → worse fills (slippage).

---

## 9. Scoring: what counts and what does not

Trading score (default **95%** of rewards) combines:

```
TradingScore = 0.79 × KappaScore + 0.21 × PnLScore   (defaults)
```

Both components use **long lookbacks** (~1.5–3 sim hours for Kappa; ~1 sim day for PnL activity sampling).

### What increases score

| Signal | Source | Requirement |
|--------|--------|-------------|
| **Kappa-3 (79%)** | Realized PnL of **completed round-trips** | ≥ **3** round-trip observations per book in lookback |
| **Realized PnL (21%)** | Sum of realized gains/losses per book | Median across books |
| **Activity factor** | Round-trip **volume** vs cap | Up to **2×** multiplier on Kappa (when enabled) |
| **Trades (maker or taker)** | Only if they **fill** and contribute to round-trips | Failed orders = no contribution |

### Per-book aggregation rules

1. Normalize raw Kappa to [0, 1] per book
2. Weight by activity × optional PnL factor
3. Up to **37.5%** of books may lack Kappa data without penalty
4. **Outlier penalty:** books much worse than median (1.5× IQR) reduce final Kappa score
5. Take **median** across scored books

### What does NOT increase score

| Action | Score effect |
|--------|--------------|
| Submitting orders that **fail** at simulator | **Zero** — no fill, no volume, no round-trip |
| **Timeout** (no response) | **Zero** instructions — missed tick |
| Validator-**dropped** excess instructions | Those orders never execute |
| Open limit orders that **never fill** | No realized PnL until filled |
| Unrealized mark-to-market | Not used in current Kappa/PnL scoring |
| High volume of **losing** taker trades | Hurts PnL; may hurt Kappa quality |

---

## 10. Failed orders → more score?

**Short answer: No.** Converting failures to successes only helps if the **successful** trades are part of **profitable, risk-adjusted round-trips** across enough books.

### Detailed logic

| Scenario | Score impact |
|----------|--------------|
| Order **fails** (loan, min size, balance) | No trade → **no** volume, **no** round-trip, **no** Kappa observation |
| Fix failure → order **fills** at a **loss** | Adds volume but **decreases** Realized PnL; may worsen Kappa |
| Fix failure → order **fills** profitably | Can improve PnL and Kappa **if** it completes round-trips with good risk profile |
| Spam 128 MARKET orders/tick, many fail | Wasted compute; failures don't help; successes may **bleed fees** |
| Spam failures **and** successes on same books | Outlier books → **penalty** increases |

### What “fixing failures” actually means operationally

1. **Pre-check before submit:** `free` balance, `min_quantity ≥ minOrderSize`, no loan cap breach
2. **Use FIFO settlement** to repay legacy loans instead of stacking new leverage
3. **Prefer maker limits** on clean books — avoids taker fee bleed
4. **Stay under 5 instructions/book** — prioritize best opportunities
5. **Rotate activity** across books — avoid >37.5% inactive books

**More successful orders ≠ higher score.** More **good round-trips on median-performing books** = higher score.

---

## 11. Mandatory constraints checklist

Use this every tick when building `respond()`:

- [ ] `FinanceAgentResponse.agent_id == self.uid`
- [ ] `quantity >= minOrderSize` (commonly **0.25** BASE after rounding)
- [ ] Prices/quantities rounded to `priceDecimals` / `volumeDecimals`
- [ ] `account.quote_balance.free >= qty × price` for BUY limits
- [ ] `account.base_balance.free >= qty` for SELL limits
- [ ] `leverage=0` unless intentionally using margin (and within `max_loan`)
- [ ] ≤ **5 instructions per book** per response
- [ ] Response within **~3s** wall clock
- [ ] Not over **volume cap** on target books (or only cancel)
- [ ] Process **`notices`** from prior tick before trading (know what filled/failed)

---

## 12. Practical miner design implications

### Recommended tick workflow

```
1. Decompress state (if needed)
2. Parse notices → update internal PnL / loan / fill tracking
3. For each book in rotation:
   a. Read touch (bid/ask/mid/spread)
   b. Read account (free balance, loans, open orders)
   c. Skip or repay if loan blocks trading
   d. Place ≤2 maker limits OR 1 flatten action per book
4. Return response before timeout
```

### Log tags (optional, for debugging without grep)

When `log_tag` is configured (e.g. `main90`, `miner232`):

| Tag | Content |
|-----|---------|
| `[tag-VAL-STATE]` | Full or brief `MarketSimulationStateUpdate` summary |
| `[tag-VAL-REQ]` | Validator hotkey + sim timestamp |
| `[tag-VAL-EVENT]` | Book events / trades from notices |
| `[tag-ORDER]` | Instructions your agent submitted |

View with: `pm2 logs <miner_name>` (no filtering required).

### Anti-patterns observed in production

| Pattern | Symptom | Score effect |
|---------|---------|--------------|
| Leveraged MARKET on all books every tick | `EXCEEDING_LOAN` spam | No fills + fee bleed on partial fills |
| `quantity < 0.25` | `MINIMUM_ORDER_SIZE_VIOLATION` | Zero execution |
| >5 instructions/book | Validator drops excess | Missed intended trades |
| Slow Python / huge loops | Timeout | **Zero** instructions entire tick |
| `debug_state_log=1` on every tick | Slow respond | Latency penalty; score grows slowly |
| >15 books/tick + 7 two-sided | Instruction reject storm | Failed orders don't score |
| Same bad books every tick | High **penalty** | Kappa median − outlier penalty |

---

## Quick reference: one tick timeline

```
T=0   Simulator publishes state at timestamp T
T+1   Validator compresses & queries all miners (parallel)
T+2   Miner: update(notices from T-1) → respond(instructions for T)
T+3   Validator validates, applies delay, sends to simulator
T+4   Simulator executes; generates notices for T+1
...
Every scoring.interval: validator updates Kappa/PnL moving averages → weights on chain
```

---

## Related files

| Path | Topic |
|------|-------|
| `sn-79/taos/im/protocol/__init__.py` | `MarketSimulationStateUpdate` |
| `sn-79/taos/im/protocol/response.py` | `FinanceAgentResponse` |
| `sn-79/taos/im/protocol/models.py` | `Book`, `Account`, `Order` |
| `sn-79/agents/README.md` | Agent API and field reference |
| `sn-79/taos/im/validator/reward.py` | Kappa + PnL scoring |
| `sn-79/agents/competitive_utils.py` | `turbo_kappa_score_tick` (high-score testnet agents) |
| `sn-79/agents/TurboForgeAgent.py` | Recommended balanced turbo agent |
| `sn-79/taos/im/validator/query.py` | Response validation |
| `SN-79-subnet-analysis.md` | Broader subnet architecture and reward tuning |
