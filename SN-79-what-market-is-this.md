# SN-79: What “Market” Is This?

> **Scope:** Trading miners on netuid **79** (mainnet) / **366** (testnet)  
> **Related docs:** [SN-79-market-data-from-validators.md](./SN-79-market-data-from-validators.md), [SN-79-miner-validator-protocol.md](./SN-79-miner-validator-protocol.md), [SN-79-subnet-analysis.md](./SN-79-subnet-analysis.md)  
> **Sources:** `README.md`, `taos/im/config/__init__.py`, `simulate/trading/run/config/simulation_0.xml`, dashboard docs

---

## Table of Contents

1. [Short answer](#1-short-answer)
2. [Simulated vs live](#2-simulated-vs-live)
3. [The asset you trade](#3-the-asset-you-trade)
4. [Where real crypto appears](#4-where-real-crypto-appears)
5. [What the 128 books are](#5-what-the-128-books-are)
6. [Who else is in the market](#6-who-else-is-in-the-market)
7. [What it is not (today)](#7-what-it-is-not-today)
8. [How this connects to subnet goals](#8-how-this-connects-to-subnet-goals)
9. [FAQ](#9-faq)

---

## 1. Short answer

**SN-79 miners trade a synthetic BASE/QUOTE spot-style market inside a C++ simulation** — not a live Coinbase or Binance order book, and not real on-chain settlement.

Validators optionally **seed** that simulation with live **BTC** (spot) and **TAO** (futures) prices, but what miners receive is **simulated limit-order-book data** on **128 parallel internal books**.

---

## 2. Simulated vs live

| | SN-79 today | Planned “Exchange” component |
|---|-------------|------------------------------|
| **Venue** | C++ simulator (`taosim`) | Real venues (not live yet) |
| **Settlement** | In-simulation balances only | TBD |
| **Miner PnL** | Simulated QUOTE units | TBD |
| **Order routing** | Validator → simulator | Future live routing |

The subnet README describes three components:

| Component | Status | Role |
|-----------|--------|------|
| **τaos** (Intelligent Markets) | **Live** | Agent-based simulated trading |
| **GenTRX** | Live (opt-in) | Distributed order-book model training |
| **Exchange** | **Coming** | Live market data + real-venue routing |

Everything miners trade on today is under **τaos simulation**.

---

## 3. The asset you trade

The simulation uses generic currency names — not a named coin ticker in the miner payload.

| Term | Meaning |
|------|---------|
| **BASE** | The asset you buy and sell (like “the coin”) |
| **QUOTE** | The currency you pay and receive (like “USD”) |

It **behaves like a crypto spot pair**: limit orders, maker/taker fees, margin, leverage, min order size. There is no `BTC-USD` or `ETHUSDT` symbol in `MarketSimulationStateUpdate`.

### Typical simulation parameters

From `simulate/trading/run/config/simulation_0.xml`:

| Parameter | Typical value |
|-----------|---------------|
| **Initial price** | ~**300** QUOTE per BASE |
| **Min order size** | **0.25** BASE |
| **Miner starting wealth** | ~**50,000 QUOTE** total (split across books) |
| **Price decimals** | 2 |
| **Volume decimals** | 4 |
| **Max leverage** | 4 |
| **Max loan** | 10,000 QUOTE per book |

Prices, balances, and PnL are **simulation units**. They are not real dollars or tokens in your wallet.

The dashboard labels this as “base asset” / “quote asset” and plots “Trade Price” vs an internal **fundamental price** per book.

---

## 4. Where real crypto appears

Validators run a **seeding process** that watches live markets and writes price samples into the simulator. Defaults from `taos/im/config/__init__.py`:

| Feed | Default symbol | Role |
|------|----------------|------|
| **Fundamental (spot)** | **BTC-USD** (Coinbase), **btcusdt** (Binance) | Seeds internal fair-value process per book |
| **External (futures)** | **TAO-PERP-INTX** (Coinbase), **taousdt** (Binance) | Feeds `FuturesSignal` process |

### How seeding affects the sim (not your payload)

```
Live BTC spot  ──► FundamentalPrice process ──► per-book "fundamental" value
Live TAO perp  ──► FuturesSignal process     ──► volume/return nudges on books
                           │
                           ▼
              C++ matching engine + NPC agents
                           │
                           ▼
         MarketSimulationStateUpdate → miners
         (simulated bids/asks/trades only)
```

**Miners never receive raw BTC or TAO tick streams.** They only see the **output**: simulated order books shaped by those processes plus hundreds of background trading agents.

So the market is best described as:

> **Crypto-flavored synthetic simulation**, influenced by BTC and TAO live feeds, **not** “you are trading the BTC market directly.”

---

## 5. What the 128 books are

**128 parallel simulated markets** — not 128 different cryptocurrencies.

| Concept | Reality |
|---------|---------|
| Book 0 vs book 127 | Same **rules** (fees, decimals, min size), different **realization** |
| Cross-book strategies | Compare mids/spreads across books (relative value) |
| Capital | Split: one account per book per UID |

Architecture from sim config:

- **16 books** per parallelization block  
- **8 blocks**  
- **128 books** total (`16 × 8`)

Each book has its own:

- Order book state  
- Fundamental price process instance  
- Mix of background agent activity  

They mimic **many parallel market realizations** of the same abstract asset class — useful for research and robust strategy scoring.

---

## 6. Who else is in the market

Besides registered miners (remote agents), the simulator runs **NPC agents** that create realistic microstructure:

| Agent type | Role |
|------------|------|
| **Initialization agents** | Seed random book structure at sim start |
| **HFT agents** | Market-maker-like liquidity |
| **STA agents** | Chartist / fundamentalist / noise traders |
| **ALGO trader agents** | Algorithmic flow |
| **Futures-linked agents** | React to external (TAO) signal |
| **Background exchange logic** | Matching, fees, margin |

Miner orders **interact** with these agents and with other miners. Market impact is real within the simulation.

---

## 7. What it is not (today)

| Misconception | Actual |
|---------------|--------|
| “I receive Binance/Coinbase L2 data” | **No** — simulated L2/L3 only |
| “I am trading BTC” | **No** — synthetic BASE/QUOTE; BTC seeds internal processes |
| “Each book is a different coin” | **No** — same asset class, different sim instances |
| “PnL is real money” | **No** — in-simulation; drives Bittensor **weights** |
| “Orders go to a live exchange” | **No** — Exchange component not live for miners |

---

## 8. How this connects to subnet goals

From the subnet README and whitepaper framing:

**Purpose:** Produce high-fidelity **Level-3 order book datasets** and incentivize **risk-managed, active trading strategies** in realistic limit-order-book environments.

Outputs are intended for:

- Market microstructure research  
- Strategy development and backtesting  
- AI/ML training (including GenTRX)  
- Future live exchange integration  

Good performance in **simulation** → higher validator scores → higher on-chain **weights** (TAO emissions). The “market” is the training and evaluation environment, not a retail trading account.

---

## 9. FAQ

### Is this a coin market?

**Indirectly inspired by crypto**, but **not a live coin market**. It is a simulated spot-style pair with crypto-like mechanics, seeded partly from BTC and TAO live prices.

### Can I arb sim prices against Binance?

**No direct link.** Sim prices evolve from internal processes and agent interaction. They may correlate with seeded feeds over long horizons but are not pegged tick-for-tick.

### Why 128 books if it is one asset?

To generate **many parallel market paths** under the same rules. Scoring aggregates performance across books; strategies must work broadly, not on one lucky book.

### Will live trading come?

The **Exchange** component is planned for live market data and order routing. Mechanism details will be published when it enters testnet. Today, all miner trading is simulation-only.

### What should I call it in code/docs?

Use **BASE/QUOTE**, **book_id**, and **simulation units**. Avoid assuming a specific ticker unless reading validator seed config for context.

---

For the exact fields in each tick's payload, see [SN-79-market-data-from-validators.md](./SN-79-market-data-from-validators.md).
