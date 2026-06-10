# SN-79: Validator Post-Response Pipeline & Simulator Internals

> **Scope:** Trading miners on netuid **79** (mainnet) / **366** (testnet)  
> **Package:** τaos **0.4.5**  
> **Audience:** Miners who understand the inbound payload ([market data doc](./SN-79-market-data-from-validators.md)) and want to know what happens **after** they return `FinanceAgentResponse`  
> **Related:** [Miner ↔ Validator Protocol](./SN-79-miner-validator-protocol.md) · [Miner workflow with examples](./SN-79-miner-workflow-with-examples.md) · [What market is this?](./SN-79-what-market-is-this.md)  
> **Sources:** `simulate/trading/` (C++ `taosim`), `taos/im/neurons/validator.py`, `taos/im/validator/forward.py`, `taos/im/validator/query.py`, `taos/im/validator/reward.py`, `SimulationManager.cpp`

---

## Table of Contents

1. [Big picture: three processes](#1-big-picture-three-processes)
2. [What the simulator is](#2-what-the-simulator-is)
3. [One simulation tick — simulator side](#3-one-simulation-tick--simulator-side)
4. [What the validator does with miner responses](#4-what-the-validator-does-with-miner-responses)
5. [Latency delays (why slow miners lose)](#5-latency-delays-why-slow-miners-lose)
6. [Simulator execution of instructions](#6-simulator-execution-of-instructions)
7. [How feedback reaches miners next tick](#7-how-feedback-reaches-miners-next-tick)
8. [Scoring loop (parallel to trading)](#8-scoring-loop-parallel-to-trading)
9. [Worked example: three ticks end-to-end](#9-worked-example-three-ticks-end-to-end)
10. [Failure modes reference](#10-failure-modes-reference)
11. [Design implications for miners](#11-design-implications-for-miners)

---

## 1. Big picture: three processes

SN-79 trading runs as **three cooperating processes** on each validator machine:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         VALIDATOR HOST                                      │
│                                                                             │
│  ┌──────────────┐   POSIX IPC    ┌──────────────────┐   HTTP/axon         │
│  │  C++ taosim  │◄──────────────►│ Python Validator │──────────────────────►│ Miners
│  │  (simulator) │  /state        │  + Query Service │◄──────────────────────│ (256 UIDs)
│  │              │  /responses    │                  │   FinanceAgentResponse
│  └──────────────┘                └──────────────────┘
│         │                                  │
│         │ 128 order books                  │ Kappa / PnL / weights
│         │ + ~500 background agents         │ → Bittensor chain
│         └──────────────────────────────────┘
└─────────────────────────────────────────────────────────────────────────────┘
```

| Process | Language | Role |
|---------|----------|------|
| **taosim** | C++ | Matching engine, background market makers/takers, physics of the market |
| **Validator** | Python | Bridge: snapshot → miners → validate → score → chain weights |
| **Query service** | Python (subprocess) | Parallel dendrite calls to all miner axons (~3s budget) |

Miners never talk to the simulator directly. Everything passes through the validator.

---

## 2. What the simulator is

### 2.1 Not a live exchange

The simulator (`taosim`, under `simulate/trading/`) is a **discrete-event limit-order-book engine**. It models:

- **128 parallel order books** (`8 blocks × 16 books/block`)
- **~500+ built-in agents** (HFT, STA, ALGO traders, initialization agents, etc.)
- **264 distributed agent slots** for registered miners (agent IDs = subnet UIDs)

Prices are seeded from optional live BTC/TAO feeds, but all fills, balances, and PnL are **in-simulation QUOTE units**. See [SN-79-what-market-is-this.md](./SN-79-what-market-is-this.md).

### 2.2 Key simulation parameters (defaults)

From `simulate/trading/run/config/simulation_0.xml` and validator config:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `step` | `1_000_000_000` ns | Sim advances in 1-second steps |
| `publish_interval` | `1_000_000_000` ns | State published to miners every **1 sim second** |
| `duration` | `86_400_000_000_000` ns | One sim day |
| `priceDecimals` | 2 | Tick size = **0.01** QUOTE per BASE |
| `volumeDecimals` | 4 | Quantity granularity |
| `minOrderSize` | **0.25** BASE | Hard floor in C++ validator |
| `miner_wealth` | 50,000 QUOTE | Starting capital (split across books) |
| `max_open_orders` | 100 | Per agent per book |
| `max_loan` | 10,000 QUOTE | Margin cap per book |
| `remoteAgentCount` | 264 | Miner agent slots |

### 2.3 Who trades besides miners?

Each tick, background agents place/cancel orders according to their own latency and strategy models (`HighFrequencyTraderAgent`, `STA`, `ALGOTraderAgent`, `FundamentalPrice`, `FuturesSignal`, etc.). Miners compete for queue position against this flow **and** each other.

---

## 3. One simulation tick — simulator side

### Step-by-step (simulator clock)

| Step | Sim time | What happens |
|------|----------|--------------|
| **S1** | `T` → `T + 1s` | Simulator runs matching for 1 sim-second: background agents trade, resting orders match, prices move |
| **S2** | `T + 1s` | **Publish event:** simulator **pauses**, serializes full state |
| **S3** | (paused) | State written to shared memory `/state`, validator notified via `/taosim-req` |
| **S4** | (paused) | Validator queries miners, validates responses, applies delays |
| **S5** | (paused) | Validator writes response batch to `/responses`, notifies simulator |
| **S6** | `T + 1s` | Simulator **unpauses**, schedules miner instructions at `T + 1s + delay` |
| **S7** | `T + 1s` → `T + 2s` | Simulator continues; delayed instructions execute when `arrival` time is reached |
| **S8** | `T + 2s` | Next publish event → cycle repeats |

### What gets serialized at publish (ValidatorRequest)

The C++ side packs (`ValidatorRequest.hpp` → `SimulationManager::publishStateMessagePack`):

```yaml
# Logical structure of one publish snapshot (msgpack)
logDir: "logs/20260606_1135"
timestamp: 41412000000000          # sim ns — e.g. 11:30:12
model: "im"

books:
  42:
    i: 42                          # canonical book id
    mtr: 0.38                      # maker/taker ratio hint
    e: [...]                       # L3 event tape since last publish
    b:                             # bids (best first)
      - {p: 309.08, q: 4.20, o: [{i: 3568640, q: 1.2, ...}, ...]}
    a:                             # asks
      - {p: 309.14, q: 3.50, o: [...]}

accounts:
  158:                             # all UIDs with activity
    42: {base: {...}, quote: {...}, orders: [...], fees: {...}}
  65:
    42: {...}
  # ... per UID per book

notices:
  158:                             # events since LAST publish for this UID
    - {type: "ET", bookId: 42, price: 309.10, quantity: 0.28, ...}
  65: [...]
```

The validator enriches accounts with cumulative volume `v` for cap tracking, attaches `config`, then compresses and fans out to miners.

---

## 4. What the validator does with miner responses

After all miners return (or timeout), the validator runs a **fixed pipeline** before anything hits the matching engine.

### 4.1 Pipeline overview

```
Miner responses (raw synapses)
        │
        ▼
┌───────────────────┐
│ validate_responses │  query.py — drop bad instructions
└─────────┬─────────┘
          ▼
┌───────────────────┐
│   update_stats     │  volume sums, response counters
└─────────┬─────────┘
          ▼
┌───────────────────┐
│    set_delays      │  reward.py — add sim-ns delay per instruction
└─────────┬─────────┘
          ▼
┌───────────────────┐
│ serialize batch    │  SimulatorResponseBatch → msgpack
└─────────┬─────────┘
          ▼
    /responses SHM → C++ taosim
          │
          ▼ (same tick, after return)
┌───────────────────┐
│ reward + report    │  Kappa/PnL if scoring interval elapsed
└───────────────────┘
```

Code path: `handle_state()` → `forward()` → query IPC → `validate_responses()` → `set_delays()` → return to simulator → `reward()` → `report()`.

### 4.2 Validation rules (`validate_responses`)

For each UID, in order:

| Check | Pass | Fail |
|-------|------|------|
| Dendrite timeout | — | **Entire response ignored** (0 instructions) |
| Dendrite failure / not success | — | **Entire response ignored** |
| Decompress `response` | Continue | Skip UID |
| `response.agent_id == uid` | Continue | **All instructions discarded** |
| Each `instruction.agentId == uid` | Keep | **All instructions discarded** |
| `instruction.bookId < book_count` | Keep | Skip instruction |
| Volume cap not exceeded | Keep new orders | Skip new orders (cancels still OK) |
| ≤ `max_instructions_per_book` (default **5**) | Keep first N per book | **Excess dropped** |
| `NO_STP` on orders | Rewritten to `CANCEL_OLDEST` | — |

**Volume cap example:**

```
miner_wealth     = 50,000 QUOTE
capital_turnover_cap = 10.0
volume_cap       = 10 × 50,000 = 500,000 QUOTE per book (assessment window)

UID 158 on book 42: traded_volume = 482,000  → can still place orders
UID 158 on book 42: traded_volume = 501,200  → new PLACE_* blocked; CANCEL_ORDERS OK
```

### 4.3 What the validator sends back to the simulator

Each surviving instruction becomes one entry in `response['responses']`:

```json
{
  "agentId": 158,
  "type": "PLACE_ORDER_LIMIT",
  "delay": 29000000,
  "payload": {
    "bookId": 42,
    "direction": "BUY",
    "volume": 0.32,
    "price": 309.10,
    "leverage": 0.0,
    "timeInForce": "GTT",
    "expiryPeriod": 180000000000,
    "stp": "CANCEL_OLDEST"
  }
}
```

The C++ side requires `agentId`, `type`, `delay`, and `payload`. It converts this into an internal `Message` with:

```
occurrence = current_sim_timestamp
arrival    = current_sim_timestamp + delay
```

The order is **not** active until `arrival` — this is how network/processing latency is modeled.

---

## 5. Latency delays (why slow miners lose)

`set_delays()` in `taos/im/validator/reward.py` runs **after** validation, **before** the simulator sees instructions.

### 5.1 Base delay (response time penalty)

```python
t = process_time / timeout          # timeout ≈ 3.0s wall clock
delay_frac = (exp(5 * t) - 1) / (exp(5) - 1)
base_delay = min_delay + delay_frac * (max_delay - min_delay)
```

Defaults: `min_delay = 10_000_000` ns (10 ms sim), `max_delay = 1_000_000_000` ns (1 sim second).

| Wall-clock response | `t` | ~base_delay (sim) |
|---------------------|-----|-------------------|
| 0.10 s (fast) | 0.03 | ~11 ms |
| 0.50 s | 0.17 | ~35 ms |
| 1.50 s | 0.50 | ~200 ms |
| 2.90 s (near timeout) | 0.97 | ~960 ms |
| ≥ 3.00 s (timeout) | — | **No instructions at all** |

### 5.2 Per-instruction jitter

Within one response, per book:

| Instruction | Extra delay |
|-------------|-------------|
| **First** on a given `book_id` | `+ 0` |
| **Second+** on same book | `+ random(5_000_000, 25_000_000)` ns (5–25 ms sim) |

Miner-supplied `instruction.delay` is **added on top** of these penalties.

### 5.3 Why this matters

A limit bid at 309.10 submitted with 200 ms extra delay may arrive **after** the ask was lifted. Another agent (or background HFT) takes the trade. Your order rests worse in queue or never fills — **no round-trip, no Kappa observation**.

---

## 6. Simulator execution of instructions

When the simulator resumes after publish, it processes queued messages whose `arrival ≤ now`.

### 6.1 Instruction types miners can send

| Type | Simulator action |
|------|------------------|
| `PLACE_ORDER_LIMIT` | Validate → reserve balance → insert in book or match immediately |
| `PLACE_ORDER_MARKET` | Validate → walk book → fill at available prices (taker) |
| `CANCEL_ORDERS` | Remove resting orders by ID |
| `CLOSE_POSITIONS` | Repay loans / close margin positions |
| `RESET_AGENT` | Validator-only; wipe agent state (deregistration) |

### 6.2 C++ placement validation (`OrderPlacementValidator.cpp`)

Even after Python validation, the exchange re-checks:

| Error | Typical cause |
|-------|---------------|
| `MINIMUM_ORDER_SIZE_VIOLATION` | `quantity < 0.25` BASE |
| `INVALID_VOLUME` | Zero/negative after rounding |
| `INVALID_LEVERAGE` | Leverage outside `[0, maxLeverage]` |
| `EXCEEDING_LOAN` | New loan would exceed `max_loan` |
| `DUAL_POSITION` | Leveraged buy while short loan exists |
| `EMPTY_BOOK` | Market order against empty side |
| Insufficient balance | `free` quote/base too low |
| `postOnly` cross | Limit would immediately take |

**Success path (limit buy example):**

1. Round `volume` to `volumeDecimals`, `price` to `priceDecimals`
2. Reserve `quantity × price` from `quote_balance.free` → `reserved`
3. Insert order in bid queue at price level
4. If price ≥ best ask → match as taker (partial or full)
5. Update balances, fees, `traded_volume`
6. Emit L3 events into book tape
7. Queue **notice** for next publish to that UID

### 6.3 Matching priority (simplified)

Within one book at one timestamp:

1. Messages sorted by `arrival` time (delayed miner orders interleaved with background agents)
2. Price-time priority within each side
3. STP rules applied (`CANCEL_OLDEST` cancels your older order on self-cross)
4. Dynamic fee policy updates maker/taker rates from recent volume

---

## 7. How feedback reaches miners next tick

Notices are **not** sent on a separate channel during the tick. They are **batched into the next** `MarketSimulationStateUpdate`.

### 7.1 Notice types miners see in `notices[uid][]`

| Notice | Meaning | Miner handler |
|--------|---------|---------------|
| `RESPONSE_DISTRIBUTED_PLACE_ORDER_LIMIT` | Limit accepted | `onOrderAccepted` |
| `ERROR_RESPONSE_DISTRIBUTED_PLACE_ORDER_LIMIT` | Limit rejected | `onOrderRejected` |
| `RESPONSE_DISTRIBUTED_PLACE_ORDER_MARKET` | Market accepted | `onOrderAccepted` |
| `ERROR_RESPONSE_DISTRIBUTED_PLACE_ORDER_MARKET` | Market rejected | `onOrderRejected` |
| `RESPONSE_DISTRIBUTED_CANCEL_ORDERS` | Cancel OK | `onOrderCancelled` |
| `EVENT_TRADE` (`ET`) | Fill (maker or taker) | `onTrade` |
| `RESPONSE_DISTRIBUTED_CLOSE_POSITIONS` | Loan closed | `onPositionClosed` |

### 7.2 Timing: one-tick lag

```
Tick N   (T = 41_412_000_000_000)
  Miner submits BUY 0.32@309.10 on book 42
  Validator validates, delay = 29 ms
  Simulator places order at T + 29 ms
  Order may fill at T + 29 ms … T + 1s

Tick N+1 (T = 41_413_000_000_000)
  notices[158] contains TradeEvent + placement confirmation
  Miner update() processes these BEFORE respond()
```

**Rule:** Decisions at tick N+1 must account for fills/rejects from tick N.

---

## 8. Scoring loop (parallel to trading)

Trading and scoring are **decoupled in time**:

| Event | Cadence | What runs |
|-------|---------|-----------|
| State publish | Every **1 sim second** | Query miners, execute instructions |
| Reward calculation | Every `scoring.interval` = **5 sim seconds** | `reward(state)` in validator |
| Metrics export | Each reward tick | Prometheus / agent table JSON |
| On-chain weights | Block time (~12s wall) | EMA of scores → Yuma consensus |

### 8.1 What `reward()` reads

The validator maintains rolling buffers per UID per book:

- Realized PnL observations (round-trip completions)
- Round-trip volume
- Inventory snapshots
- Trade timestamps

It does **not** re-read your agent code. It only sees **executed** simulator outcomes.

### 8.1 Default score composition

```
TradingScore = 0.79 × KappaScore + 0.21 × PnLScore
```

| Component | Lookback | Minimum data |
|-----------|----------|--------------|
| Kappa-3 | 10,800 s sim (3 sim hours) | ≥ **3** round-trips per book |
| Realized PnL | ~1 sim day activity sampling | Median across books |
| Activity factor | Round-trip volume vs cap | Scales Kappa contribution |
| Outlier penalty | IQR on weak books | Subtracts from KappaScore |

Inactive books: up to **37.5%** of books may lack Kappa without penalty.

---

## 9. Worked example: three ticks end-to-end

**Scenario:** UID **158** (`AscendForgeAgent`, `ascend_profile=forge`) trades **book 42** across three consecutive publishes.

### Tick 1 — Submit

**Simulator publishes** at `T₁ = 41_412_000_000_000` (11:30:12 sim):

```yaml
books[42]:
  best_bid: 309.08
  best_ask: 309.14
  spread:   0.06  (6 ticks)

accounts[158][42]:
  base:  {free: 0.12,  reserved: 0.00}
  quote: {free: 412.30, reserved: 0.00}
  orders: []
  traded_volume: 4821.5
```

**Miner `respond()`** returns:

```python
FinanceAgentResponse(agent_id=158, instructions=[
  # inside bid, 2 ticks below ask
  PlaceLimit(book_id=42, BUY, qty=0.32, price=309.10, expiry=180s),
  # touch ask on wide book 75 (different rotation)
  PlaceLimit(book_id=75, SELL, qty=0.32, price=307.92),
])
```

**Validator validation:**

| Check | Book 42 BUY | Book 75 SELL |
|-------|-------------|--------------|
| agent_id match | ✓ | ✓ |
| instructions/book ≤ 5 | 1 | 1 |
| volume cap | 4821 < 500000 ✓ | ✓ |
| balance | 0.32×309.10 = 98.91 ≤ 412.30 ✓ | 0.32 ≤ 0.12 ✗ |

Book 75 SELL **fails balance check in agent** (not submitted) — assume agent pre-filtered and only book 42 instruction sent.

**Delay:** `process_time = 0.42s` → `base_delay ≈ 35 ms`

**Simulator at `T₁ + 35 ms`:**

```
BOOK 42 : PLACED BUY LIMIT #3678211 FOR 0.32@309.10
```

Order rests in queue; no immediate cross (309.10 < 309.14).

---

### Tick 2 — Fill notice

**Simulator publishes** at `T₂ = 41_413_000_000_000`:

```yaml
notices[158]:
  - type: RESPONSE_DISTRIBUTED_PLACE_ORDER_LIMIT
    bookId: 42
    success: true
    orderId: 3678211
    price: 309.10
    quantity: 0.32

  - type: EVENT_TRADE
    bookId: 42
    makerAgentId: 158        # you were passive
    takerAgentId: 246
    side: SELL               # taker sold into your bid
    price: 309.10
    quantity: 0.32
    makerFee: 0.08
```

**Miner `update()`** logs:

```
BOOK 42 : BUY TRADE #466509 : YOUR PASSIVE ORDER #3678211 MATCHED ...
```

**Account after fill:**

```yaml
accounts[158][42]:
  base:  {free: 0.44,  reserved: 0.00}    # +0.32 BASE
  quote: {free: 313.29, reserved: 0.00}    # -98.91 QUOTE - fees
  orders: []
```

**Miner `respond()`** — completion leg (requote after `onTrade`):

```python
# After maker BUY fill, place completion SELL inside spread
PlaceLimit(book_id=42, SELL, qty=0.32, price=309.12)
```

**Delay:** `process_time = 0.38s` → `base_delay ≈ 32 ms`

**Simulator:** SELL 0.32@309.12 rests; spread is still 6 ticks; profitable round-trip edge if filled above 309.10 + fees.

---

### Tick 3 — Round-trip complete + scoring

**Simulator publishes** at `T₃ = 41_414_000_000_000`:

```yaml
notices[158]:
  - type: EVENT_TRADE
    bookId: 42
    makerAgentId: 158
    takerAgentId: 140
    side: BUY                 # taker bought from your ask
    price: 309.12
    quantity: 0.32
```

**Round-trip PnL (book 42):**

```
Buy  0.32 @ 309.10  = -98.912 QUOTE
Sell 0.32 @ 309.12  = +98.918 QUOTE
Fees (maker both)   ≈ -0.16 QUOTE
Net realized        ≈ +0.006 QUOTE  (small win)
```

**Scoring tick** (every 5 sim seconds; assume `T₃` aligns):

Validator appends this round-trip to UID 158's Kappa window for book 42. After **≥ 3** such observations in the 3-hour lookback, book 42 contributes to `KappaScore`. Volume adds to activity factor.

**Agent table metrics** (what you see in `agents_*.json`):

```yaml
agent_id: "158"
kappa: 0.000346              # median across books
kappa_score: 0.50007
total_roundtrip_volume: 2233668.8
total_realized_pnl: 968.53
activity_factor: 1.0
placement: 132
```

---

## 10. Failure modes reference

### At validator (Python) — silent drops

| Symptom | Cause | Next tick |
|---------|-------|-----------|
| No instructions executed | Timeout ≥ 3s | Empty notices; missed opportunity |
| Partial instruction set | >5 per book | Only first 5 kept |
| New orders skipped | Volume cap | Cancels still work |
| All instructions gone | `agent_id` mismatch | Log warning in validator |

### At simulator (C++) — ERROR notices next tick

| Log / notice message | Fix |
|----------------------|-----|
| `MINIMUM_ORDER_SIZE_VIOLATION` | `quantity >= 0.25` |
| `EXCEEDING_LOAN` | Flatten / FIFO repay / `leverage=0` |
| `FAILED TO PLACE` (balance) | Check `free` not `total` |
| `FAILED TO CANCEL ORDER #… does not exist` | Stale cancel IDs (harmless) |
| Order never fills | Delay too high; price too far from touch |

---

## 11. Design implications for miners

1. **Respond fast** — sub-second wall time keeps `base_delay` near 10 ms sim; timeouts zero out the entire tick.
2. **First instruction per book is cheapest** — batch cancels before new places on the same book.
3. **Assume 1-tick feedback lag** — use `notices` from tick N when deciding tick N+1.
4. **Validator drop ≠ simulator reject** — two validation layers; only simulator fills score.
5. **Scoring is slow** — Kappa needs hours of **profitable** round-trips; one good tick barely moves placement.
6. **128 books share one capital pool** — simulator enforces per-book accounts; strategy must rotate.
7. **Background agents are the counterparty** — you are not only trading other miners.

---

## Quick reference timeline

```
SIM T=0s     Background agents trade
SIM T=1s     PAUSE → pack state → validator queries miners (wall ~0–3s)
WALL +0.4s   Miner 158 responds → validate → delay +35ms
SIM T=1.035s Order placed in book
SIM T=1s–2s  Matching continues, fill may occur
SIM T=2s     PAUSE → notices include TradeEvent → miner sees fill
SIM T=2.4s   Miner responds with completion SELL
SIM T=3s     Next publish; round-trip may complete
SIM T=5s     reward() updates Kappa buffers
CHAIN        weights EMA toward new scores
```

---

## Related files

| Path | Topic |
|------|-------|
| `simulate/trading/src/cpp/simulation/SimulationManager.cpp` | Publish pause/resume, IPC, response unpack |
| `simulate/trading/src/cpp/exchange/OrderPlacementValidator.cpp` | Order rejection rules |
| `taos/im/neurons/validator.py` | `handle_state`, `_listen`, IPC loop |
| `taos/im/validator/forward.py` | Miner query orchestration |
| `taos/im/validator/query.py` | `validate_responses` |
| `taos/im/validator/reward.py` | `set_delays`, Kappa/PnL scoring |
| `taos/im/agents/__init__.py` | Notice dispatch in `update()` |
| `SN-79-miner-validator-protocol.md` | Inbound protocol & miner obligations |
| `SN-79-miner-workflow-with-examples.md` | Miner-side tick walkthrough |
