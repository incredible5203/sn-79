# SN-79 Market Data Miners Receive from Validators

> **Scope:** Trading miners on netuid **79** (mainnet) / **366** (testnet)  
> **Package:** τaos **0.4.5**  
> **Primary type:** `MarketSimulationStateUpdate` (validator → miner)  
> **Related docs:** [SN-79-miner-validator-protocol.md](./SN-79-miner-validator-protocol.md), [SN-79-validator-and-simulator-internals.md](./SN-79-validator-and-simulator-internals.md), [SN-79-order-types-and-config-reference.md](./SN-79-order-types-and-config-reference.md), [SN-79-what-market-is-this.md](./SN-79-what-market-is-this.md)  
> **Sources:** `taos/im/protocol`, `taos/im/validator/query.py`, `taos/im/utils/compress.py`, `simulate/trading/.../ValidatorRequest.hpp`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Delivery model](#2-delivery-model)
3. [Wire format and compression](#3-wire-format-and-compression)
4. [Top-level payload fields](#4-top-level-payload-fields)
5. [Book data (public market)](#5-book-data-public-market)
6. [Simulation config](#6-simulation-config)
7. [Private data: accounts and notices](#7-private-data-accounts-and-notices)
8. [Metadata envelope](#8-metadata-envelope)
9. [What miners must compute locally](#9-what-miners-must-compute-locally)
10. [What is not sent](#10-what-is-not-sent)
11. [One-tick mental model](#11-one-tick-mental-model)
12. [Practical implications for strategy](#12-practical-implications-for-strategy)

---

## 1. Overview

Each simulation tick, the validator sends one **`MarketSimulationStateUpdate`** synapse to your miner axon. After decompression, this is the complete picture of the market you can trade on for that tick.

The payload has three logical parts:

| Part | Shared or private | Contents |
|------|-------------------|----------|
| **Books** | Shared (identical for all miners) | L2 snapshots + L3 event tape for all order books |
| **Accounts + notices** | Private (your UID only) | Balances, orders, loans, fill/reject feedback |
| **Config** | Shared | Simulation rules (decimals, limits, fees, timing) |

For what asset or venue this represents, see [SN-79-what-market-is-this.md](./SN-79-what-market-is-this.md).

---

## 2. Delivery model

The C++ simulator (`taosim`) runs continuously, then **pauses at each publish event**, builds a state snapshot, queries all miners, applies accepted instructions, and resumes.

| Property | Typical value |
|----------|---------------|
| **Cadence** | Every `config.publish_interval` sim-ns (usually **1 sim second** = `1_000_000_000` ns) |
| **Books** | **128** parallel order books (`16 books/block × 8 blocks`) |
| **Response timeout** | ~**3 seconds** wall-clock per miner |

The `timestamp` field is simulation time in **nanoseconds** since sim start. All book and event timestamps use the same clock.

**Critical rule:** Miners can only act at publish events — not on a continuous live feed.

---

## 3. Wire format and compression

Over the network, large fields are compressed (default **lz4** + msgpack for protocol version ≥ 45):

```
compressed = {
  "books":  <ONE blob, identical for ALL miners>,
  "payload": <per-UID blob: accounts, notices, config, response>
}
```

From `taos/im/validator/query.py`:

1. **`books`** — compressed once and shared across every miner (public market data).
2. **`accounts` + `notices`** — stripped to **only your UID** before send.
3. **`config`** — full simulation config (same for everyone).

Miners decompress in `MarketSimulationStateUpdate.decompress()`. With `lazy_load=1` (common in run scripts), books parse on access via `LazyBook`.

---

## 4. Top-level payload fields

Defined in `taos/im/protocol/__init__.py`:

| Field | Type | Description |
|-------|------|-------------|
| `version` | `int \| None` | Validator **taos** package version |
| `timestamp` | `int` | Sim time in nanoseconds |
| `config` | `MarketSimulationConfig` | Simulation parameters |
| `books` | `dict[int, Book]` | Book ID → L2/L3 snapshot + events |
| `accounts` | `dict[int, dict[int, Account]]` | UID → (book_id → account) — **your UID only on wire** |
| `notices` | `dict[int, list[FinanceNotice]]` | UID → events since last tick — **your UID only on wire** |
| `response` | `FinanceAgentResponse \| None` | Miner fills this on return |
| `compressed` | `str \| dict \| None` | Compressed blob when sent over network |
| `compression_engine` | `str` | `lz4`, `zlib`, or `zstd` |
| `dendrite` | (Bittensor) | Querying validator (hotkey, IP, port) |

---

## 5. Book data (public market)

For each `book_id` in `state.books` (0 … 127), you receive a hybrid **L2 snapshot + L3 event tape**.

### 5.1 L2 depth snapshot (`bids` / `asks`)

From simulator config (`simulation_0.xml`):

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `maxDepth` / `book_levels` | **21** | Price levels per side in the snapshot |
| `detailedDepth` / `detailed_book_levels` | **5** | Top N levels include per-order breakdown |

- **Bids:** best (highest) price first, descending.
- **Asks:** best (lowest) price first, ascending.

#### Top 5 levels (`LevelInfo` with order queue)

| Field | Meaning |
|-------|---------|
| `price` | Level price |
| `quantity` | Total size at that price (BASE) |
| `orders[]` | Individual resting orders at this level |

Each order in `orders[]`:

| Field | Meaning |
|-------|---------|
| `id` | Simulator order ID |
| `client_id` | Agent-assigned ID (if any) |
| `timestamp` | When placed |
| `quantity` | Remaining size |
| `side` | `0` = bid, `1` = ask |
| `price` | Limit price |
| `leverage` | Leverage on that order |

**Note:** Resting orders in the book snapshot do **not** include `agent_id`. You see queue structure at the touch, but not who owns each order unless you infer from your own placements or from trades.

#### Levels 6–21 (aggregate only)

Only `price` + `quantity` — no per-order list. Enough for depth, spread, and imbalance; not for queue-position modeling beyond the top 5.

### 5.2 Maker–taker ratio (`mtr`)

The simulator sends **`mtr`** per book (recent maker/taker volume ratio). It drives **dynamic fees** (`targetMTR=0.4` in config).

It arrives in the raw book dict as `mtr`. The typed `Book.MTR` property exists in the model, but `Book.from_json` / `LazyBook` do not currently expose it as a property — use raw dict access if needed, or infer from `account.fees`.

### 5.3 L3 event tape (`events[]`)

All market activity on that book **since the previous publish** (one `publish_interval` of sim time).

#### `Order` (`y = "o"`) — new liquidity

| Field | Present |
|-------|---------|
| `id`, `timestamp`, `quantity`, `side`, `price`, `leverage`, `client_id` | Yes |
| **`agent_id`** | **No** (stripped in wire format) |

Use for: flow detection, cancel/replace patterns, volume arriving at a price.

#### `TradeInfo` (`y = "t"`) — executions

| Field | Meaning |
|-------|---------|
| `id`, `timestamp`, `quantity`, `price`, `side` | Trade details |
| `taker_id`, `taker_agent_id`, `taker_fee` | Aggressor order + **agent UID** + fee |
| `maker_id`, `maker_agent_id`, `maker_fee` | Resting order + **agent UID** + fee |

This is the main way to see **who traded**. You can tell if a given UID was maker/taker and compute short-window OHLCV from the tape.

Helper properties on `Book` (miner-computed from events):

```python
book.trades          # dict[timestamp → TradeInfo]
book.OHLC            # open/high/low/close from trades this interval
book.traded_volume   # sum(qty * price) this interval
book.trade_imbalance # buy qty - sell qty this interval
```

#### `Cancellation` (`y = "c"`) — removals

| Field | Meaning |
|-------|---------|
| `orderId`, `timestamp`, `price`, `quantity` | What left the book |

No agent ID on cancellations.

### 5.4 Snapshot vs tape

| Data | Represents |
|------|------------|
| **L2 arrays** (`bids` / `asks`) | Book state **at the pause point** (end of interval) |
| **`events[]` tape** | Everything that happened **during** the interval |

Together they support current touch/depth plus microstructure of the last tick. The base agent `StateHistoryManager` replays `events[]` to build longer lookbacks locally.

---

## 6. Simulation config

Full **`MarketSimulationConfig`** in `state.config` — rules of the game.

### Trading constraints (most important for miners)

| Field | Typical | Use |
|-------|---------|-----|
| `book_count` | 128 | Valid `book_id` range |
| `book_levels` | 21 | Snapshot depth |
| `detailed_book_levels` | 5 | Per-order detail depth |
| `publish_interval` | 1e9 ns | Tick cadence |
| `priceDecimals` | 2 | Round limit prices |
| `volumeDecimals` | 4 | Round quantities |
| `miner_wealth` | ~50k QUOTE | Capital base; volume cap denominator |
| `max_open_orders` | 100 | Per book |
| `max_leverage` | 4 | Sim max |
| `max_loan` | 10,000 | Per book |
| Min order size (sim) | **0.25** | Minimum order qty |
| `grace_period` | 600s sim | No miner trading before this |
| `fee_policy` | dynamic | Maker/taker tiers from MTR + volume |

The config also includes background-agent parameters (STA, HFT, futures agents, fundamental price process, etc.). You do not control them, but they explain why books move.

---

## 7. Private data: accounts and notices

### 7.1 Accounts — `state.accounts[your_uid][book_id]`

One account per book; capital is **split across all books**.

| Field | Meaning |
|-------|---------|
| `base_balance` / `quote_balance` | `{total, free, reserved, initial}` |
| `base_loan` / `quote_loan` | Outstanding margin |
| `base_collateral` / `quote_collateral` | Posted collateral |
| `orders[]` | Your resting orders (full detail) |
| `loans` | `order_id → Loan` for leveraged positions |
| `fees` | `{volume_traded, maker_fee_rate, taker_fee_rate}` |
| `traded_volume` | Cumulative volume; validator may inject `v` for cap tracking |

- **`free`** — available for new orders.
- **`reserved`** — locked in open orders.

You do **not** receive other miners' accounts.

### 7.2 Notices — `state.notices[your_uid][]`

**Feedback from the previous tick** — outcomes of *your* instructions:

| Notice | When |
|--------|------|
| `LimitOrderPlacementEvent` | Limit placed (success/fail + message) |
| `MarketOrderPlacementEvent` | Market placed |
| `OrderCancellationsEvent` | Cancel batch results |
| `TradeEvent` | **Your** fill (fees, maker/taker role) |
| `ClosePositionsEvent` | Loan repay / close |
| `ResetAgentsEvent` | Deregistration wipe |
| `SimulationStartEvent` / `SimulationEndEvent` | Lifecycle |

Failed placements include `success=false` and messages like `EXCEEDING_LOAN`, `MINIMUM_ORDER_SIZE_VIOLATION`. Process notices in `update()` before `respond()`.

---

## 8. Metadata envelope

| Field | Meaning |
|-------|---------|
| `version` | Validator taos package version |
| `timestamp` | Sim time at snapshot |
| `dendrite.hotkey` | Which validator sent this |
| `compression_engine` | Codec used on the wire |

If you query multiple validators, track `dendrite.hotkey` separately — history and accounts are per-validator.

---

## 9. What miners must compute locally

Validators send raw state, not indicators:

| Metric | How to get it |
|--------|----------------|
| Mid | `(best_bid + best_ask) / 2` |
| Spread / spread ratio | `ask - bid`, divided by mid |
| Microprice | Volume-weighted touch |
| Cross-book median mid | Aggregate across `state.books` |
| Returns | `(mid - prev_mid) / prev_mid` — needs local history |
| Queue position | Only top 5 levels have order lists |
| Competitor inventory | **Not available** — only trade agent IDs on fills |

---

## 10. What is not sent

| Not included | Notes |
|--------------|-------|
| Full book beyond 21 levels | Deeper liquidity is invisible |
| Historical snapshots from prior ticks | Rebuild via local history replay |
| Fundamental price / magnetic-field state | Internal sim processes only |
| Other miners' balances or orders | Private per UID |
| Raw BTC/TAO live feeds | See [SN-79-what-market-is-this.md](./SN-79-what-market-is-this.md) |
| Validator scores / weights | On-chain / dashboard only |
| `agent_id` on order/cancel tape events | Only on `TradeInfo` |

---

## 11. One-tick mental model

```
┌─────────────────────────────────────────────────────────────────┐
│  MarketSimulationStateUpdate @ timestamp T                      │
├─────────────────────────────────────────────────────────────────┤
│  SHARED (128 books × identical for all miners)                │
│    Per book:                                                    │
│      • L2: 21 bid + 21 ask levels (top 5 with order queues)     │
│      • L3 tape: all place/trade/cancel since T - publish_int    │
│      • mtr (maker-taker ratio, raw dict)                        │
├─────────────────────────────────────────────────────────────────┤
│  PRIVATE (your UID only)                                        │
│    • 128 accounts (balances, loans, your orders, fee rates)      │
│    • notices[] (fills, rejects, cancels from last tick)         │
├─────────────────────────────────────────────────────────────────┤
│  CONFIG (shared rules)                                          │
│    • decimals, limits, fees, publish_interval, sim params       │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼  your agent: update() → respond() → FinanceAgentResponse
```

---

## 12. Practical implications for strategy

1. **Touch + 21 levels** — enough for maker quoting and cross-book relative value; not full depth.
2. **Top-5 order queues** — queue-position logic only near the touch.
3. **Trade tape with agent IDs** — see who got filled; order/cancel tape does not identify agents.
4. **1-second granularity** — design on `publish_interval`; sub-second alpha requires replaying `events[]`.
5. **128 independent books** — same asset rules, different microstructure; cross-book signals are first-class.
6. **Notices lag books by one tick** — current books are "now"; notices tell you what happened to your last orders.

For the full request/response lifecycle, scoring, and failed-order rules, see [SN-79-miner-validator-protocol.md](./SN-79-miner-validator-protocol.md).  
For high-score agent implementation, see `agents/competitive_utils.py` and [SN-79-testnet-miner-guide.md](./SN-79-testnet-miner-guide.md#turbo-scoring-agents-recommended).
