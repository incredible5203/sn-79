# SN-79: Order Types, Log Messages & Simulation Config Reference

> **Scope:** Trading miners on netuid **79** / **366**  
> **Package:** τaos **0.4.5**  
> **Audience:** Anyone reading PM2 miner logs or parsing `state.config` / `notices`  
> **Related:** [Miner workflow (examples)](./SN-79-miner-workflow-with-examples.md) · [Validator & simulator internals](./SN-79-validator-and-simulator-internals.md) · [Market data from validators](./SN-79-market-data-from-validators.md) · [agents/README.md](./agents/README.md)

---

## Table of Contents

1. [Three layers of “order language”](#1-three-layers-of-order-language)
2. [Directions: BUY vs SELL](#2-directions-buy-vs-sell)
3. [Order kinds: LIMIT vs MARKET](#3-order-kinds-limit-vs-market)
4. [What you submit: instructions](#4-what-you-submit-instructions)
5. [PM2 log lines explained](#5-pm2-log-lines-explained)
6. [Notices: feedback on the next tick](#6-notices-feedback-on-the-next-tick)
7. [Public book events (not yours)](#7-public-book-events-not-yours)
8. [Error codes when placement fails](#8-error-codes-when-placement-fails)
9. [Simulation config — every field](#9-simulation-config--every-field)
10. [Quick lookup table](#10-quick-lookup-table)

---

## 1. Three layers of “order language”

SN-79 uses the same concepts in three places. Do not confuse them.

| Layer | When | Where | Example |
|-------|------|-------|---------|
| **A. Instructions** | You *send* this tick | `FinanceAgentResponse.instructions` | `BUY 0.32@309.10 ON BOOK 42` |
| **B. Log lines** | Same tick, after `report()` | PM2 stdout | `INSTRUCTIONS` block, then later `BOOK 42 : PLACED BUY LIMIT...` |
| **C. Notices** | *Next* tick | `state.notices[your_uid]` | `TradeEvent`, `LimitOrderPlacementEvent` |

```
Tick N   respond() → INSTRUCTIONS logged → validator → simulator executes
Tick N+1 update() → notices describe what happened to those instructions
```

**Important:** `PLACED BUY LIMIT` in logs is **not** instant confirmation. It is the simulator’s outcome after your instruction runs (often visible in the same log burst, but logically it is feedback from execution). Your agent’s `onOrderAccepted` / `onTrade` handlers fire when the **next** state update’s `notices` are processed.

---

## 2. Directions: BUY vs SELL

| Term | Meaning | Balance effect (simplified) |
|------|---------|----------------------------|
| **BUY** | You want to acquire **BASE** using **QUOTE** | Spend QUOTE, receive BASE |
| **SELL** | You want to dispose of **BASE** for **QUOTE** | Give BASE, receive QUOTE |

Internal encoding: `OrderDirection.BUY = 0`, `SELL = 1`. In placement events, `side=0` is BUY, `side=1` is SELL.

### “BUY TRADE” vs “SELL TRADE” in trade lines

Trade log lines use **trade direction** (who initiated the match), not only your order side:

```
BOOK 42 : BUY TRADE #466509 : YOUR PASSIVE ORDER #3678221 (AGENT 65) MATCHED AGAINST #3678222 (AGENT 43) FOR 0.23@306.35
```

| Phrase | Meaning |
|--------|---------|
| **BUY TRADE** | The **aggressor** (taker) was a **buyer** — they lifted an ask |
| **SELL TRADE** | The **aggressor** was a **seller** — they hit a bid |

Your role:

| Phrase | You were… |
|--------|-----------|
| **YOUR PASSIVE ORDER** | **Maker** — your limit was resting; someone traded into you |
| **YOUR AGGRESSIVE ORDER** | **Taker** — your order crossed the spread and took liquidity |

Example: You placed a resting **BUY** limit at 309.10. A seller hits it → log may show **SELL TRADE** (taker sold) and you are **PASSIVE** maker on the bid.

---

## 3. Order kinds: LIMIT vs MARKET

| Kind | Behavior | Typical use |
|------|----------|-------------|
| **LIMIT** | Rest at a price (or cross if price through market) | Maker strategy, controlled edge |
| **MARKET** | Execute immediately at best available prices | Urgent flatten, taker |

### LIMIT specifics

| Concept | Meaning |
|---------|---------|
| **Price** | Exact limit price (rounded to `priceDecimals`, usually 2 → 0.01 tick) |
| **postOnly** | If order would match immediately → **reject** (stay maker) |
| **timeInForce** | `GTC` (rest until cancel), `GTT` (+ expiry), `IOC` (partial fill + cancel rest), `FOK` (all or nothing) |
| **expiryPeriod** | Sim-ns lifetime when using `GTT` |

### MARKET specifics

| Concept | Meaning |
|---------|---------|
| **quantity in BASE** | Buy/sell this many BASE units |
| **quantity in QUOTE** | Spend/receive this much QUOTE; engine computes BASE fill |

### Maker vs taker (fees & scoring)

| Role | How you get it | Fees |
|------|----------------|------|
| **Maker** | Passive limit filled by someone else | Usually lower (`maker_fee_rate`) |
| **Taker** | Market order or limit that crosses spread | Usually higher (`taker_fee_rate`) |

Both can count toward volume; **scoring cares about profitable round-trips**, not taker vs maker alone.

---

## 4. What you submit: instructions

Returned in `FinanceAgentResponse`. Logged in the `INSTRUCTIONS` block:

```
--------------------------------------------------
INSTRUCTIONS
--------------------------------------------------
CANCEL ORDER #3533662 ON BOOK 64
SELL 0.32@284.91 ON BOOK 64
BUY  0.32@313.52 ON BOOK 93
BUY  0.32@294.32 ON BOOK 12
CANCEL ORDER #3533662 ON BOOK 64
BUY  0.32@306.14 ON BOOK 109
SELL 0.32@306.35 ON BOOK 109
--------------------------------------------------
```

### Instruction types

| Log / type | API | What it does |
|------------|-----|--------------|
| `BUY …@price ON BOOK n` | `response.limit_order(..., BUY, ...)` | Place limit bid |
| `SELL …@price ON BOOK n` | `response.limit_order(..., SELL, ...)` | Place limit ask |
| `BUY …@MARKET ON BOOK n` | `response.market_order(..., BUY, ...)` | Market buy |
| `SELL …@MARKET ON BOOK n` | `response.market_order(..., SELL, ...)` | Market sell |
| `CANCEL ORDER #id ON BOOK n` | `response.cancel_orders(...)` | Cancel resting order |
| `CLOSE POSITIONS ON BOOK n` | `response.close_positions(...)` | Repay margin / close loan |

### Instruction fields (payload)

| Field | Applies to | Meaning |
|-------|------------|---------|
| `bookId` | All | 0 … `book_count - 1` (usually 0–127) |
| `direction` | Place | `BUY` or `SELL` |
| `quantity` / `volume` | Place | Size in BASE (or QUOTE for market-in-quote) |
| `price` | Limit only | Limit price |
| `leverage` | Place | `0` = spot; `>0` borrows up to `(1+leverage)×quantity` |
| `settleFlag` / `settlement_option` | Place | `NONE`, `FIFO` loan repay, or specific order id |
| `stp` | Place | Self-trade prevention (default rewritten to `CANCEL_OLDEST`) |
| `postOnly` | Limit | Reject if would cross |
| `timeInForce` | Limit | GTC / GTT / IOC / FOK |
| `expiryPeriod` | Limit + GTT | Rest time in sim-ns |
| `delay` | All | Extra sim-ns delay (added to validator latency penalty) |
| `orderId` | Cancel | Simulator-assigned id from prior `PLACED` notice |

---

## 5. PM2 log lines explained

### 5.1 State header

```
VALIDATOR : 5EWwdZB7qCCMaAso5Mzcks4UUcPxKYvpAj32t5Mg1v6HSxoF
SIMULATION TIME : 11:30:13.000000000 (T=41413000000000)
```

| Field | Meaning |
|-------|---------|
| `VALIDATOR` | Hotkey of validator that queried you |
| `SIMULATION TIME` | Human-readable sim clock |
| `T=` | Raw timestamp in **simulation nanoseconds** since sim start |

### 5.2 `INSTRUCTIONS` / `NO INSTRUCTIONS TO SUBMIT`

| Line | Meaning |
|------|---------|
| `INSTRUCTIONS` | Your agent returned ≥1 instruction this tick |
| `NO INSTRUCTIONS TO SUBMIT` | Empty response (hold, timeout recovery, or risk-off) |

### 5.3 Placement outcomes

Format (`events.py`):

```
BOOK {id} : {PLACED|FAILED TO PLACE} {BUY|SELL} {LIMIT|MARKET} ORDER #{orderId} FOR {qty}@{price} AT {time} (T={ns}) [: error]
```

**Examples:**

```
BOOK 64 : PLACED SELL LIMIT ORDER #3533854 FOR 0.32@284.91 AT 11:30:12.074550105 (T=41412074550105)
```

| Part | Meaning |
|------|---------|
| `PLACED` | Simulator accepted order; it rests or filled |
| `FAILED TO PLACE` | Simulator rejected (see §8) |
| `SELL LIMIT ORDER` | Limit ask |
| `#3533854` | **Order ID** — use for `CANCEL ORDER #3533854` |
| `0.32@284.91` | Quantity × price |
| `1.32x0.32@...` | Leveraged: effective size = `(1+leverage)×quantity` |

```
BOOK 93 : PLACED BUY MARKET ORDER #3648943 FOR 0.32 AT 11:30:12.065839003 (T=41412065839003)
```

Market orders omit `@price` in the placement line; fill price comes in the **TRADE** line.

### 5.4 Cancellation outcomes

```
BOOK 64 : CANCELLED ORDER #3533662 AT 11:30:12.065839003 (T=41412065839003)
BOOK 18 : FAILED TO CANCEL ORDER #3606254 AT 11:27:11.068519158 (T=41231068519158) : Order IDs 3606254 do not exist.
```

| Line | Meaning | Typical cause |
|------|---------|---------------|
| `CANCELLED ORDER #id` | Order removed from book | Success |
| `FAILED TO CANCEL ORDER #id` | Cancel rejected | Order already filled, already cancelled, or stale ID |
| `… do not exist` | No such order id on this book | Race: filled before cancel arrived; or duplicate cancel |

**Stale cancels are common** when you cancel every tick and the order already traded. Harmless but noisy.

### 5.5 Trade lines

```
BOOK 4 : SELL TRADE #430412 : YOUR PASSIVE ORDER #3568642 (AGENT 65) MATCHED AGAINST #3568726 (AGENT 140) FOR 0.255@299.51 AT 11:30:12.241952022 (T=41412241952022)
```

| Part | Meaning |
|------|---------|
| `SELL TRADE` / `BUY TRADE` | Taker side (aggressor direction) |
| `#430412` | Trade id (unique match) |
| `YOUR PASSIVE ORDER #3568642` | Your resting order filled (maker) |
| `(AGENT 65)` | Your UID |
| `MATCHED AGAINST #3568726 (AGENT 140)` | Counterparty order and UID |
| `FOR 0.255@299.51` | Quantity × price |

**Balance impact (maker BUY @ 299.51, qty 0.255):**

- BASE: `+0.255`
- QUOTE: `-0.255 × 299.51` minus maker fee

### 5.6 Position close lines

```
CLOSED POSITION FOR ORDER #12345 FOR 0.32 AT ...
FAILED TO CLOSE POSITION FOR ORDER #12345 : ...
```

Repays margin loan tied to a leveraged order. Use `close_positions()` when flattening loans.

### 5.7 Global lifecycle

| Line | Meaning |
|------|---------|
| `SIMULATION STARTED!` | New sim run (config may have changed) |
| `SIMULATION ENDED!` | Sim day complete |

---

## 6. Notices: feedback on the next tick

Delivered in `state.notices[your_uid]` and dispatched in `update()`:

| Notice class | Trigger | Handler |
|--------------|---------|---------|
| `LimitOrderPlacementEvent` | Limit place success/fail | `onOrderAccepted` / `onOrderRejected` |
| `MarketOrderPlacementEvent` | Market place success/fail | same |
| `OrderCancellationsEvent` | Cancel batch | `onOrderCancelled` / `onOrderCancellationFailed` |
| `TradeEvent` | Your order traded | `onTrade` |
| `ClosePositionsEvent` | Loan close batch | `onPositionClosed` |
| `SimulationStartEvent` | Sim start | `onStart` |
| `SimulationEndEvent` | Sim end | `onEnd` |
| `ResetAgentsEvent` | UID deregistered | account wipe |

### Notice vs log string

The string format is identical — notices are structured versions of the same events:

```python
# LimitOrderPlacementEvent.__str__
"PLACED BUY LIMIT ORDER #3678211 FOR 0.32@309.10 AT 11:30:12.065839003 (T=41412065839003)"

# On failure:
"FAILED TO PLACE SELL LIMIT ORDER FOR 0.32@309.10 AT ... : EXCEEDING_LOAN"
```

### `TradeEvent` fields

| Field | Meaning |
|-------|---------|
| `bookId` | Book |
| `tradeId` | Match id |
| `makerAgentId` / `takerAgentId` | UIDs |
| `makerOrderId` / `takerOrderId` | Order ids |
| `makerFee` / `takerFee` | Fees paid (QUOTE) |
| `side` | `0` = buy-initiated trade, `1` = sell-initiated |
| `price`, `quantity` | Fill |

---

## 7. Public book events (not yours)

In `state.books[book_id].events` — **all** market activity on that book last tick:

| Tape type | Abbrev | Meaning |
|-----------|--------|---------|
| Order | `o` | Someone placed an order |
| TradeInfo | `t` | A trade occurred (may not involve you) |
| Cancellation | `c` | Order removed |

These do **not** change your balance. Use for signals (tape imbalance, last trade price). Your fills **also** appear here and in `notices`.

---

## 8. Error codes when placement fails

From C++ `OrderErrorCode` (`simulate/trading/src/cpp/util/Order.hpp`):

| Code | Meaning | What to do |
|------|---------|------------|
| `MINIMUM_ORDER_SIZE_VIOLATION` | `quantity < 0.25` BASE (after rounding) | Use `≥ 0.25` (many agents use 0.32) |
| `EXCEEDING_LOAN` | Margin loan would exceed `max_loan` | `close_positions`, FIFO settle, `leverage=0` |
| `EXCEEDING_MAX_ORDERS` | Too many open orders on book | Cancel stale orders |
| `DUAL_POSITION` | Leveraged long + short loan same book | Close opposite loan first |
| `INSUFFICIENT_BASE` | Not enough free BASE | Flatten or cancel sells |
| `INSUFFICIENT_QUOTE` | Not enough free QUOTE | Flatten or cancel buys |
| `INVALID_LEVERAGE` | Outside `[0, max_leverage]` | Fix leverage param |
| `INVALID_VOLUME` / `INVALID_PRICE` | Zero or bad rounding | Round to decimals |
| `EMPTY_BOOK` | Market order, no liquidity | Skip market or use limit |
| `PRICE_INCREMENT_VIOLATED` | Price not on tick grid | Round to `priceDecimals` |
| `VOLUME_INCREMENT_VIOLATED` | Qty not on lot grid | Round to `volumeDecimals` |
| `CONTRACT_VIOLATION` | `postOnly` would cross | Lower buy / raise sell |
| `NONEXISTENT_ACCOUNT` | Internal / wrong book | Should not happen for miners |

Validator may drop instructions **before** the simulator (timeout, wrong `agent_id`, >5 instr/book, volume cap). Those produce **no** notice — only missing expected fills.

---

## 9. Simulation config — every field

Full type: `MarketSimulationConfig` in `state.config` (`taos/im/protocol/models.py`).  
Parsed from `simulation_0.xml` on the validator.

### 9.1 Simulation identity & timing

| Field | Example | Meaning |
|-------|---------|---------|
| `simulation_id` | `20260606_1135` | Current run id (from log dir); resets on config change |
| `logDir` | path | Simulator log directory (often stripped on wire) |
| `time_unit` | `ns` | All times in nanoseconds |
| `duration` | `86400000000000` | Total sim length (1 sim day) |
| `grace_period` | `600000000000` | **600 sim seconds** before miners can trade |
| `publish_interval` | `1000000000` | State publish every **1 sim second** |
| `log_window` | `3600000000000` | Metrics/logging window (1 sim hour) |

### 9.2 Book structure

| Field | Example | Meaning |
|-------|---------|---------|
| `block_count` | `8` | Parallel simulation blocks |
| `books_per_block` | `16` | Books per block |
| `book_count` | `128` | Total books (`block_count × books_per_block`) |
| `book_levels` | `21` | L2 depth in each snapshot |
| `detailed_book_levels` | `5` | Top N levels include per-order queue |
| `remoteAgentCount` | `264` | Miner agent slots in sim |

### 9.3 Decimals & precision

| Field | Example | Meaning |
|-------|---------|---------|
| `priceDecimals` | `2` | Price tick = **0.01** |
| `volumeDecimals` | `4` | Qty step = **0.0001** |
| `baseDecimals` | `4` | BASE balance precision |
| `quoteDecimals` | `10` | QUOTE balance precision |
| `init_price` | `300.0` | Starting price anchor |

**Min order size** is **not** a separate config field on the wire — it comes from sim XML `minOrderSize` = **0.25** BASE.

### 9.4 Miner capital & limits

| Field | Example | Meaning |
|-------|---------|---------|
| `miner_capital_type` | `pareto` / `static` | How initial wealth is split across books |
| `miner_base_balance` | varies | Initial BASE per book (if static) |
| `miner_quote_balance` | varies | Initial QUOTE per book (if static) |
| `miner_wealth` | `50000` | Total starting wealth in QUOTE terms; used for **volume cap** (`10 × miner_wealth` default) |
| `max_open_orders` | `100` | Max resting orders per agent per book |
| `max_leverage` | `4` | Max leverage multiplier |
| `max_loan` | `10000` | Max QUOTE loan per book |
| `maintenance_margin` | `0.10` | Liquidation threshold for margin |

### 9.5 Fees

| Field | Meaning |
|-------|---------|
| `fee_policy` | `FeePolicy` object: `fee_type`, `params`, `tiers[]` |
| `fee_policy.fee_type` | `static`, `tiered`, or `dynamic` |
| `fee_policy.tiers[].volume_required` | Volume to reach tier |
| `fee_policy.tiers[].maker_fee` | Maker rate (fraction) |
| `fee_policy.tiers[].taker_fee` | Taker rate (fraction) |

Dynamic policy (default XML): maker/taker rates adjust from maker-taker ratio and your volume.

### 9.6 Fundamental price process (background)

Drives fair-value drift on each book:

| Field | Meaning |
|-------|---------|
| `fp_update_period` | How often FP updates |
| `fp_seed_interval` | Reseed interval |
| `fp_mu` | Drift |
| `fp_sigma` | Volatility |
| `fp_lambda` | Jump intensity |
| `fp_mu_jump` / `fp_sigma_jump` | Jump distribution |

You cannot control these; they explain correlated moves across books.

### 9.7 Initialization agents

| Field | Meaning |
|-------|---------|
| `init_agent_count` | Number of one-shot liquidity seeders |
| `init_agent_capital_type` | Capital distribution |
| `init_agent_base_balance` / `init_agent_quote_balance` | Per-agent balances |
| `init_agent_wealth` | Total wealth |
| `init_agent_tau` | After this time, their orders are cancelled |

### 9.8 High-frequency trader (HFT) agents

Background market makers (~10 instances):

| Field | Meaning |
|-------|---------|
| `hft_agent_count` | Instance count |
| `hft_agent_*_balance` / `hft_agent_wealth` | Capital |
| `hft_agent_feed_latency_min` | Min market-data delay |
| `hft_agent_order_latency_min` / `max` | Order placement latency range |
| `hft_agent_order_latency_scale` | Latency scaling |
| `hft_agent_tau` | Strategy time constant |
| `hft_agent_delta` | Sensitivity |
| `hft_agent_psi` | Probability weight |
| `hft_agent_gHFT` | Aggressiveness |
| `hft_agent_kappa` | Inventory control |
| `hft_agent_spread` | Target spread |
| `hft_agent_order_size_mean` | Typical order size |
| `hft_agent_price_noise` / `price_shift` | Pricing noise / bias |

### 9.9 Stylized trader agents (STA)

Chartist / fundamentalist mix:

| Field | Meaning |
|-------|---------|
| `sta_agent_count` | Instance count |
| `sta_agent_*_balance` / `sta_agent_wealth` | Capital |
| `sta_agent_feed_latency_*` | Feed delay distribution |
| `sta_agent_order_latency_*` | Order delay distribution |
| `sta_agent_decision_latency_mean` / `std` | Think time |
| `sta_agent_selection_scale` | Book selection |
| `sta_agent_noise_weight` | Noise trader weight |
| `sta_agent_chartist_weight` | Technical trader weight |
| `sta_agent_fundamentalist_weight` | Fundamental trader weight |
| `sta_agent_tau` / `tauHist` / `tauF` | Decision and forecast horizons |
| `sta_agent_sigmaEps` | Forecast noise |
| `sta_agent_r_aversion` | Risk aversion |

### 9.10 Futures / external signal agents

| Field | Meaning |
|-------|---------|
| `futures_agent_count` | Agents trading on external signal |
| `futures_agent_*_balance` / `wealth` | Capital |
| `futures_agent_volume` | Typical size |
| `futures_agent_sigmaEps` | Signal noise |
| `futures_agent_lambda` | Arrival intensity |
| `futures_agent_feed_latency_*` | Feed delays |
| `futures_agent_order_latency_*` | Order delays |
| `futures_agent_selection_scale` | Book selection |

Brings live futures-style signal into sim; explains bursts on some books.

### 9.11 Account fields (per book, not in `config` but paired with it)

| Field | Meaning |
|-------|---------|
| `base_balance.total` | All BASE held |
| `base_balance.free` | Available for new sells |
| `base_balance.reserved` | Locked in open sell orders |
| `quote_balance.*` | Same for QUOTE |
| `base_loan` / `quote_loan` | Outstanding borrow |
| `orders[]` | Your resting orders (`id`, `side`, `price`, `quantity`, `leverage`, `timestamp`) |
| `fees.volume_traded` | Volume on this book |
| `fees.maker_fee_rate` / `taker_fee_rate` | Your current tier rates |
| `traded_volume` | Cumulative; validator may add `v` for cap |

---

## 10. Quick lookup table

| You see… | It means… | Scores? |
|----------|-----------|---------|
| `INSTRUCTIONS` + `BUY 0.32@309.10` | You asked to place limit bid | Not yet |
| `PLACED BUY LIMIT ORDER #id` | Order accepted | Not until fill |
| `FAILED TO PLACE … EXCEEDING_LOAN` | Rejected | No |
| `CANCELLED ORDER #id` | Removed resting order | No |
| `FAILED TO CANCEL … do not exist` | Stale cancel | No |
| `BUY/SELL TRADE … YOUR PASSIVE` | Maker fill | Yes (volume + PnL path) |
| `BUY/SELL TRADE … YOUR AGGRESSIVE` | Taker fill | Yes |
| `NO INSTRUCTIONS TO SUBMIT` | Idle tick | No |
| (nothing + timeout) | Validator dropped you | No |

---

## Related files

| Path | Topic |
|------|-------|
| `taos/im/protocol/instructions.py` | Instruction types |
| `taos/im/protocol/events.py` | Notice types & log strings |
| `taos/im/protocol/models.py` | `MarketSimulationConfig`, enums |
| `taos/im/protocol/response.py` | `limit_order`, `market_order`, `cancel_orders` |
| `taos/im/agents/__init__.py` | `update()` notice dispatch, trade log format |
| `simulate/trading/run/config/simulation_0.xml` | Raw sim XML defaults |
| `agents/README.md` | Agent API reference |
