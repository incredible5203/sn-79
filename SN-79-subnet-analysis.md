# SN-79 (MVTRX / τaos) — Subnet Analysis

> **Repository:** [taos-im/sn-79](https://github.com/taos-im/sn-79)  
> **Netuid:** 79 (mainnet), 366 (testnet)  
> **Last analyzed:** June 2026  
> **Sources:** README, agents guide, validator/miner code, reward logic, GenTRX docs

---

## Table of Contents

1. [Purpose of This Subnet](#1-purpose-of-this-subnet)
2. [Main Logic of This Subnet](#2-main-logic-of-this-subnet)
3. [What Miners Have to Do](#3-what-miners-have-to-do)
4. [Reward Logic](#4-reward-logic)
5. [Source Update & How to Submit Results](#5-source-update--how-to-submit-results)
6. [Validator Logic](#6-validator-logic)
7. [How Validators Check Miner Results](#7-how-validators-check-miner-results)
8. [How to Increase Reward as a Miner](#8-how-to-increase-reward-as-a-miner)
9. [How to Make Higher Quality Results as a Miner](#9-how-to-make-higher-quality-results-as-a-miner)
10. [Required Spec & Stack](#10-required-spec--stack)

---

## 1. Purpose of This Subnet

**MVTRX (SN-79)** is a Bittensor subnet for **decentralized market research and AI model training**. It is branded as *"A New Kind of Exchange"* and combines three planned/live components:

| Component | Status | Purpose |
|-----------|--------|---------|
| **τaos** (Intelligent Markets) | Live | Agent-based simulation of automated trading in realistic limit-order-book markets |
| **GenTRX** | Live (opt-in) | Distributed training of a shared order-book generative model (~12M-param transformer) |
| **Exchange** | Coming | Live market data and order routing to real venues |

### Core mission

The subnet incentivizes miners to deploy **intelligent, risk-managed, actively trading strategies** inside high-fidelity simulated markets. Validators run a C++ matching engine that produces **Level-3 (market-by-order) order book data** — the same granularity used in real HFT and surveillance systems.

Outputs are intended for:

- Market research and microstructure analysis
- Trading strategy development and backtesting
- AI/ML training on realistic order flow
- Future live exchange integration

The mechanism is designed so that **good trading behavior in simulation** produces **useful, statistically significant datasets** across many parallel order book realizations (~40 books today, targeting 1,000+).

---

## 2. Main Logic of This Subnet

### High-level architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     C++ Simulator (taosim)                      │
│  • MAXE-based agent-based market simulation                     │
│  • ~40 parallel order books + ~1000 background agents each      │
│  • Full L3 order book + matching engine                         │
│  • Pauses while waiting for miner responses                     │
└──────────────────────────┬──────────────────────────────────────┘
                           │ state updates (IPC)
┌──────────────────────────▼──────────────────────────────────────┐
│                  Python Validator (τaos)                        │
│  • Forwards state to miners via dendrite / query service        │
│  • Validates & submits miner instructions back to simulator     │
│  • Tracks P&L, volume, Kappa-3, scores miners                   │
│  • Sets on-chain weights                                       │
│  • (Optional) GenTRX gradient server sidecar                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ MarketSimulationStateUpdate (compressed)
┌──────────────────────────▼──────────────────────────────────────┐
│                      Miner Agents (UIDs)                        │
│  • Receive book state + account/notices                         │
│  • Return FinanceAgentResponse (orders/cancels)                 │
│  • (Optional) Train GenTRX model & upload gradients to S3       │
└─────────────────────────────────────────────────────────────────┘
```

### Simulation loop (one tick)

1. **Simulator advances** until the next `publish_interval` (state publishing event).
2. **Validator pauses simulation** and packages a `MarketSimulationStateUpdate` containing:
   - Partial L3 + L2 snapshots for all books (top 21 levels)
   - All events since last update
   - Per-miner account balances and order notices
3. **Query service** compresses and sends state to all registered miners in parallel (default timeout: **3.0s** per miner, **4.0s** global).
4. **Miners respond** with `FinanceAgentResponse` containing trading instructions.
5. **Validator validates** responses (agent ID, volume caps, instruction limits, decompression).
6. **Latency scoring** applies execution delays based on response time (`set_delays`).
7. **Simulator executes** validated instructions and continues.
8. **Periodically** (every `scoring.interval`, default 5s sim time), validator computes rewards and updates moving-average scores.
9. **Weights** are set on-chain from accumulated scores (trading pool + optional GenTRX pool).

### Key design properties

- **Discrete action timescale:** Miners can only act at state publish events (not continuous streaming yet).
- **Market impact:** Miner orders interact with background agents and other miners — strategies must account for impact.
- **Rolling score window:** Scores use a lookback window that **persists across simulation restarts** (~weekly config changes).
- **Deregistration handling:** New UID on a slot gets reset capital/positions; history is cleared for that UID.
- **Init/grace period:** Warm-up before miners participate; minimum lookback required before Kappa scoring activates.

### Two parallel incentive pools

| Pool | Default share | Scored on |
|------|---------------|-----------|
| **Trading** | ~95% | Kappa-3 risk-adjusted returns + realized P&L |
| **GenTRX training** | ~5% (scales with participation) | Gradient quality vs held-out order-book data |

GenTRX is **opt-in** for both validators and miners. Unused training allocation returns to the trading pool.

---

## 3. What Miners Have to Do

### Core miner responsibilities (trading)

1. **Register a UID** on netuid 79 (or 366 for testnet).
2. **Run a miner neuron** (`taos/im/neurons/miner.py`) with a registered axon (default port 8091).
3. **Implement or deploy a trading agent** — a Python class loaded from `~/.taos/agents/` (or custom path).
4. **Respond to validator queries** within the timeout (~3 seconds):
   - Decompress incoming `MarketSimulationStateUpdate`
   - Analyze book state, events, and personal account/notices
   - Return `FinanceAgentResponse` with instructions
5. **Trade actively and profitably** across simulated order books while managing risk.

### Instruction types miners can submit

| Instruction | Description |
|-------------|-------------|
| `PLACE_ORDER_MARKET` | Immediate market order |
| `PLACE_ORDER_LIMIT` | Resting limit order (GTC, GTT, IOC, FOK, post-only) |
| `CANCEL_ORDERS` | Cancel one or more orders |
| `CLOSE_POSITIONS` | Close leveraged positions |
| `RESET_AGENT` | (Validator-only) Reset deregistered agents |

### Optional: GenTRX participation

Miners can **additionally**:

1. Subclass `GenTRXAgent` (or use example agents like `HybridTrainingAgent`).
2. Configure an **S3/R2 bucket** and commit read credentials on-chain.
3. Each round (~5 min on mainnet):
   - Receive training assignment via `GenTRXAssignment` synapse
   - Download assigned simulation data slice
   - Train shared model locally (background thread)
   - Compress and upload gradient to personal S3 bucket
4. Download updated checkpoint at next round start.

Training runs **concurrently** with live trading — no trade-off between pools.

### What miners do NOT do

- Miners do **not** run the simulator.
- Miners do **not** set weights.
- Miners do **not** push code to validators — they respond to synapses only.
- Example agents (`SimpleRegressorAgent`, `RandomMakerAgent`, etc.) are **not competitive** without customization.

---

## 4. Reward Logic

### Overview: two-pool scoring → weights

```
Per-round scores (per UID)
    ├── Trading score = kappa_weight × KappaScore + pnl_weight × PnLScore
    │       └── Trading rewards → Pareto sort-multiply distribution
    └── GenTRX score = rank-normalized gradient quality + EMA smoothing
            └── No Pareto; pool-sized at weight-setting time

Moving averages (slow EMA on validator) → prepare_weights() → on-chain weights
```

Default component weights (trading pool):

| Component | Default weight | Range |
|-----------|---------------|-------|
| **Kappa-3** | 0.79 (79%) | [0, 1] |
| **Realized P&L** | 0.21 (21%) | [-0.5, 0.5] mapped into trading score |

GenTRX pool default: **5%** of simulation rewards (`--scoring.gentrx.simulation_share=0.05`), scaled by `N_active / N_target`.

---

### 4.1 Kappa-3 Score (primary trading metric)

**Kappa-3** measures **risk-adjusted return quality** from **realized P&L** of completed round-trip trades:

```
K₃(τ) = (μ - τ) / [LPM₃(τ)]^(1/3)
```

Where:
- `μ` = mean return from realized P&L observations
- `τ` = threshold return (default 0.0)
- `LPM₃` = third lower partial moment (downside risk)

**Per-book calculation → median aggregation** across books with outlier penalty.

#### Kappa scoring pipeline

1. **Compute raw Kappa-3** per book from realized P&L history (requires min 3 observations, min lookback ~5400s sim time).
2. **Normalize** per book to [0, 1] using range [-2.5, 2.5].
3. **Activity factor** (volume-based, per book):
   - Active: `1 + (roundtrip_volume / volume_cap × impact)`, capped at 2.0
   - Inactive: exponential decay after grace period
4. **P&L factor** (optional, `kappa.pnl.impact`, default 0.0): boosts/penalizes based on per-book realized profitability.
5. **Combine:** `weighted_kappa = activity × pnl × normalized_kappa` (asymmetric weighting).
6. **Inactive book tolerance:** Up to **37.5%** of books can have no Kappa data without penalty.
7. **Outlier penalty:** Books performing significantly worse than median (1.5×IQR rule) reduce final score.
8. **Final Kappa score** = `median(weighted_kappas) - outlier_penalty`, clamped to [0, 1].

#### Key Kappa config defaults

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `kappa.lookback` | 10,800s sim time (~3 sim hours) | Assessment window |
| `kappa.min_lookback` | 5,400s | Minimum history before scoring |
| `kappa.min_realized_observations` | 3 | Min round-trips needed |
| `max_inactive_books` | 0.375 (37.5%) | Allowed inactive books ratio |

---

### 4.2 P&L Score (secondary trading metric)

Measures **absolute profitability** per book:

1. Sum **realized P&L** per book over lookback window.
2. Normalize to **daily return ratio** using per-book capital allocation (`miner_wealth / book_count`).
3. Clip to [-100%, +100%] daily return.
4. Apply same inactive-book tolerance (37.5%).
5. Take **median** across scored books.
6. Map to **[-0.5, 0.5]** where 0 = breakeven.

---

### 4.3 Activity & Volume Mechanics

| Concept | Description |
|---------|-------------|
| **Trading volume** | Total QUOTE value of matched orders (maker or taker) |
| **Roundtrip volume** | Volume from completed buy-sell (or sell-buy) cycles |
| **Volume cap** | `capital_turnover_cap × miner_wealth` (default: **10×** initial capital per assessment period) |
| **Volume sampling** | Every 600s sim time |
| **Assessment period** | 86,400s sim time (1 sim day) |

**Effects:**
- High roundtrip volume → activity factor up to **2.0×** on Kappa
- No recent trades → activity factor **decays** (prevents one burst then idle)
- Exceeding volume cap on a book → **blocked from new orders** on that book (cancellations still allowed)

> **Note:** Current config defaults set `activity.impact=0.0` and `activity.decay_rate=0.0`, meaning activity weighting may be **disabled** depending on validator deployment. The mechanism exists and can be re-enabled by validators.

---

### 4.4 Latency Penalty (execution quality)

Slow responses → higher instruction delay → worse fill prices (slippage):

```
delay = min_delay + exponential(process_time / timeout) × (max_delay - min_delay)
```

| Parameter | Default |
|-----------|---------|
| `min_delay` | 10ms sim time |
| `max_delay` | 1000ms sim time |
| `neuron.timeout` | 3.0s |
| First instruction per book | No extra per-instruction delay |
| Subsequent instructions | +5ms to +25ms random delay each |

**Timeout = no instructions executed** for that tick.

---

### 4.5 Pareto Reward Distribution (trading pool)

After computing trading scores, rewards pass through **Pareto sort-multiply**:

1. Sort scores ascending.
2. Multiply by Pareto-distributed factors (shape=1.42, scale=1.0, seeded).
3. Re-order to original UID positions.

This creates **variance in reward allocation** — top performers get disproportionately more, reinforcing competitive differentiation.

GenTRX scores **skip** Pareto and use direct rank-normalization + EMA.

---

### 4.6 GenTRX Training Rewards

When enabled:

1. Validators score uploaded gradients against **held-out** order-book data.
2. **Overfit penalty** when own-data loss beats held-out loss significantly.
3. **Rank-normalize** across miners (0 = worst, 1 = best).
4. **Per-UID EMA** smoothing (alpha=0.1).
5. Pool allocation: `simulation_pool × gentrx_simulation_share × (N_active / N_target)`.

Best gradient proposals aggregated by canonical aggregator (UID 0) → new checkpoint published on-chain.

---

### 4.7 Weight Setting

Final on-chain weights computed in `prepare_weights()`:

```python
trading_weights = normalize(trading_scores)
gentrx_weights  = normalize(gentrx_scores)

gentrx_alloc  = simulation_pool × gentrx_sim_share × shrink
trading_alloc = simulation_pool - gentrx_alloc

raw_weights = trading_alloc × trading_weights + gentrx_alloc × gentrx_weights
# + optional burn allocation
# → process_weights_for_netuid (Bittensor constraints)
# → convert to uint16 → set_weights on chain
```

Scores accumulate via **slow EMA** on the validator between weight-setting events.

---

## 5. Source Update & How to Submit Results

### Trading results (primary submission path)

Miners **do not upload files or submit results manually**. Trading outcomes are submitted **automatically via synapse response**:

```
Validator                          Miner
   │                                 │
   │── MarketSimulationStateUpdate ──►│  (compressed state)
   │                                 │  agent.handle(state)
   │                                 │  → FinanceAgentResponse
   │◄── MarketSimulationStateUpdate ──│  (compressed response)
   │                                 │
   │  validate → apply delays → execute in simulator
```

**Response format:** `FinanceAgentResponse` with `instructions[]` array.

**Miner entry point** (`taos/im/neurons/miner.py`):

```python
async def forward(self, synapse: MarketSimulationStateUpdate):
    synapse.decompress(lazy=self.config.agent.params.lazy_load)
    synapse.response = self.agent.handle(synapse)
    return synapse.clear_inputs().compress()
```

### GenTRX results (optional second submission path)

GenTRX uses a **separate async pipeline**:

| Step | Mechanism |
|------|-----------|
| Assignment delivery | Validator → miner via `GenTRXAssignment` dendrite/HTTP |
| Gradient upload | Miner → personal S3 bucket (discovered via on-chain commitment) |
| Scoring | Validator gradient server fetches from miner buckets |
| Checkpoint | Aggregator (UID 0) publishes to S3 + on-chain version |

**Setup:**
1. Create R2/Hippius bucket with write + read tokens.
2. Run `python bin/setup_miner_bucket.py` to commit read credentials on-chain.
3. Set `GENTRX_AGENT_S3_*` environment variables.
4. Launch with `./run_miner.sh -G`.

### Source code updates

Both miners and validators pull latest code via run scripts:

```bash
# Miner — auto-pulls on each start
./run_miner.sh -w <coldkey> -h <hotkey> -u 79 -a 8091

# Validator — auto-pulls, rebuilds simulator
./run_validator.sh -w <coldkey> -h <validator> -u 79
```

Manual miner launch:

```bash
cd taos/im/neurons
python miner.py --netuid 79 \
  --wallet.name <coldkey> --wallet.hotkey <hotkey> \
  --axon.port 8091 \
  --agent.path ~/.taos/agents \
  --agent.name <YourAgentClass> \
  --agent.params "lazy_load=1"
```

### Testing before mainnet

| Environment | Netuid | Method |
|-------------|--------|--------|
| Local proxy | N/A | `agents/proxy/` — offline simulator + proxy validator |
| Testnet | 366 | Register UID, deploy miner, test connectivity |
| Mainnet | 79 | Production |

---

## 6. Validator Logic

### Components

| Component | Role |
|-----------|------|
| **C++ Simulator (taosim)** | Order book matching, background agents, event generation |
| **Python Validator** | Bittensor integration, scoring, weight setting |
| **Query Service** | Standalone async process for parallel miner queries (POSIX IPC) |
| **Gradient Server** (optional) | GenTRX data accumulation, gradient scoring, checkpoint proposals |

### Validator lifecycle

1. **Initialize** simulator from XML config (`simulation.xml_config`).
2. **Start query service** as subprocess with shared memory IPC.
3. **Main loop:**
   - Receive state update from simulator
   - Call `forward()` → query all miners
   - Apply validated instructions with delays
   - Send instructions back to simulator
   - Track trades, inventory, P&L, volumes
   - On scoring interval: compute Kappa, P&L, GenTRX scores
   - Update moving-average score tensors
   - Periodically `set_weights()` on chain
4. **Handle events:** deregistrations, simulation restarts (ESE), checkpoint resume.
5. **Report metrics** to Grafana dashboard (optional).

### Scoring data tracked per UID

| Data structure | Contents |
|----------------|----------|
| `realized_pnl_history` | `{uid: {timestamp: {book_id: pnl}}}` |
| `roundtrip_volumes` | `{uid: {book_id: {timestamp: volume}}}` |
| `volume_sums` | Cumulative matched volume per book |
| `inventory_history` | Account value snapshots |
| `activity_factors` | Per-book activity multipliers |
| `kappa_values` | Computed Kappa metrics and scores |
| `gentrx_scores` | Gradient quality scores |

### GenTRX validator flow (when `-G` enabled)

1. Push simulation state ticks to gradient server (`POST /gentrx/state`).
2. Open training rounds (`POST /gentrx/round`) on block cadence (~25 blocks ≈ 5 min).
3. Deliver assignments to miners via IPC query service.
4. Poll scores (`GET /gentrx/scores`).
5. Merge GenTRX scores into weight vector at weight-setting time.

---

## 7. How Validators Check Miner Results

### 7.1 Network-level checks (query service)

Before any instruction reaches the simulator:

| Check | Action on failure |
|-------|-------------------|
| **Timeout** (>3.0s) | No instructions executed; counted as timeout |
| **Network failure** | No instructions; counted as failure |
| **Blacklist** | Rejected; counted as rejection |
| **Decompression failure** | Response discarded |
| **Missing response** | Treated as failure |

### 7.2 Response validation (`validate_responses`)

| Check | Rule |
|-------|------|
| **Agent ID match** | `response.agent_id == uid` |
| **Instruction agent ID** | Each instruction's `agentId == uid` |
| **Book ID valid** | `bookId < book_count` |
| **Volume cap** | Block new orders if `volume >= capital_turnover_cap × miner_wealth` (cancels exempt) |
| **Instruction limit** | Max **5 instructions per book** per response (excess dropped) |
| **STP enforcement** | `NO_STP` auto-converted to `CANCEL_OLDEST` |
| **Structure validity** | Malformed instructions skipped with warning |

### 7.3 Simulator-level execution

Validated instructions are submitted to the C++ simulator which:

- Applies **latency delays** based on response time
- Matches orders against book using standard exchange rules
- Enforces balance, leverage, and self-trade prevention
- Generates trade events, updates accounts
- Computes fees based on maker/taker ratio (MTR) policy

### 7.4 Performance verification (scoring)

After execution, validators continuously verify:

| Metric | How measured |
|--------|-------------|
| **Realized P&L** | From completed round-trip trades per book per timestamp |
| **Kappa-3** | Statistical calculation on realized return series |
| **Trading volume** | Sum of matched order values in QUOTE |
| **Roundtrip volume** | Volume from closed positions |
| **Inventory value** | Account value using midquote/best-bid/liquidation |
| **Response time** | Dendrite `process_time` recorded in miner stats |
| **GenTRX gradient quality** | Loss on held-out data vs assigned data (overfit penalty) |

### 7.5 Monitoring & dashboards

Public dashboards at [taos.simulate.trading](https://taos.simulate.trading) show:

- Per-agent scores, rankings, P&L, volume, activity factors
- Response time, timeout/failure/rejection counts
- Per-book Kappa, fees, MTR
- GenTRX training metrics (when active)

---

## 8. How to Increase Reward as a Miner

### Trading pool (95%+ of rewards)

1. **Maximize risk-adjusted returns (Kappa-3)**
   - Focus on **consistent positive realized P&L** from round-trips
   - Minimize downside volatility (Kappa penalizes bad tail returns)
   - Trade enough to meet minimum 3 realized observations

2. **Maintain profitability across many books**
   - Median aggregation rewards **consistent** performance
   - Avoid catastrophic losses on any single book (outlier penalty)
   - Can skip up to 37.5% of books, but excess inactive books score 0

3. **Generate realized P&L score**
   - 21% of trading score comes from absolute profitability
   - Target positive daily returns relative to allocated capital per book

4. **Respond fast**
   - Sub-3-second responses avoid timeout (zero instructions)
   - Faster responses → lower execution delay → better fills

5. **Stay active**
   - When activity weighting is enabled: maintain roundtrip volume
   - Avoid volume cap breach (blocks new orders on that book)
   - Consistent trading prevents activity factor decay

6. **Compete for Pareto amplification**
   - Higher raw trading scores get disproportionately amplified
   - Small edge in Kappa/P&L can mean large weight difference

### GenTRX pool (up to 5%)

7. **Submit high-quality gradients**
   - Train on assigned data without overfitting
   - Meet round deadlines (~5 min on mainnet)
   - Use GPU for reliable completion

8. **Participate consistently**
   - Pool scales with `N_active / N_target` — more active trainers = larger pool
   - EMA smoothing rewards sustained quality over time

### Operational

9. **Low latency to validators** — reduce network RTT
10. **Enable `lazy_load=1`** — faster deserialization, fewer timeouts
11. **Test on testnet (366)** before mainnet deployment
12. **Monitor dashboard** — track score, volume cap proximity, response times

---

## 9. How to Make Higher Quality Results as a Miner

"Quality" in SN-79 means **risk-adjusted trading performance** that produces realistic, valuable market data — not just raw profit.

### Strategy design principles

| Principle | Why it matters |
|-----------|---------------|
| **Risk management first** | Kappa-3 explicitly penalizes downside volatility |
| **Active market making / taking** | Idle miners score poorly; volume weighting rewards engagement |
| **Cross-book consistency** | Median + outlier penalty punish one-book gambling |
| **Market impact awareness** | Your orders move the book — account for slippage |
| **Latency-aware execution** | Use `delay` parameter to schedule within publish interval |
| **Capital efficiency** | Don't hit volume caps without proportional edge |
| **Leverage discipline** | Margin available but increases risk; can't hold both sides leveraged |

### Technical quality

| Area | Recommendation |
|------|----------------|
| **State parsing** | Use `lazy_load=1`; only parse books/fields you need |
| **Parallelization** | Process 40+ books concurrently if strategy is compute-heavy |
| **Event handling** | Implement `onTrade`, `onOrderAccepted`, etc. for stateful strategies |
| **L3 event analysis** | Use `book.events` for microstructure signals (see `ImbalanceAgent`) |
| **Instruction efficiency** | Max 5 instructions/book — prioritize highest-value actions |
| **Self-trade prevention** | Use appropriate STP settings |

### GenTRX quality

| Area | Recommendation |
|------|----------------|
| **Generalization** | Avoid overfitting to assigned slice (held-out scoring catches this) |
| **Timely uploads** | Complete training + upload within round window |
| **Custom training** | Override `collect_row`, `select_training_files`, `train` hooks |
| **Hardware** | GPU (6-8GB+ VRAM) for reliable round completion |

### Development workflow

```
1. Develop agent logic locally (agents/proxy/)
2. Backtest against background model
3. Deploy to testnet (netuid 366)
4. Monitor dashboard metrics (score, Kappa, volume, latency)
5. Iterate strategy based on per-book performance
6. Deploy to mainnet (netuid 79)
7. (Optional) Enable GenTRX with -G flag
```

---

## 10. Required Spec & Stack

### Miner requirements

#### Minimum (trading only)

| Resource | Requirement |
|----------|-------------|
| **RAM** | ~1 GB per miner instance (+ strategy overhead) |
| **CPU** | Depends on strategy complexity |
| **Network** | Stable connection; low latency to validators preferred |
| **OS** | Linux (Ubuntu 22.04+ recommended) |
| **Disk** | Minimal (~few GB for Python env + agent code) |

#### Recommended (competitive trading)

| Resource | Recommendation |
|----------|---------------|
| **RAM** | 4-8 GB+ |
| **CPU** | 4+ cores for multi-book parallel strategies |
| **Network** | Low-latency VPS near validator nodes |
| **Axon** | Public IP + open port (default 8091) |

#### Additional for GenTRX

| Resource | Minimum | Comfortable |
|----------|---------|-------------|
| **GPU** | NVIDIA 6 GB VRAM | RTX 3060/4060 8 GB+ |
| **RAM** | 8 GB | 16 GB |
| **Disk** | 20 GB | 50 GB |
| **Bandwidth** | 5 Mbps | 25 Mbps |

### Validator requirements

| Resource | Minimum | Notes |
|----------|---------|-------|
| **RAM** | 32 GB | 40 books × ~1000 background agents |
| **CPU** | 16 cores | More cores = faster sim (may diverge from network) |
| **OS** | Ubuntu ≥ 22.04 | |
| **Compiler** | g++ 14 | g++-13.1 on Ubuntu 22.04 |
| **Build tools** | cmake 3.29.7, vcpkg | ~2+ hours compile time |
| **Python** | 3.10.9 | Tested version |

#### Additional for GenTRX validator

| Resource | Minimum | Comfortable |
|----------|---------|-------------|
| **GPU** | NVIDIA 8 GB VRAM | 12 GB+ |
| **RAM** | 16 GB | 32 GB |
| **Disk** | 50 GB | 100 GB |

### Software stack

#### Core dependencies (from `requirements.txt`)

| Category | Packages |
|----------|----------|
| **Bittensor** | `bittensor >= 9.4.0` |
| **ML/Compute** | `torch >= 2.7.0`, `scikit-learn`, `optuna` |
| **Async/Network** | `aiohttp`, `uvloop`, `httpx`, `aiofiles` |
| **Serialization** | `msgpack`, `msgspec`, `lz4`, `zstandard`, `ypyjson` |
| **Parallelism** | `loky >= 3.5.5`, `posix-ipc` |
| **Data** | `pandas >= 2.2.3`, `polars >= 0.20`, `pyarrow >= 14.0` |
| **GenTRX** | `fastapi`, `uvicorn`, `boto3`, `aiobotocore`, `transformers >= 4.36` |
| **Monitoring** | `prometheus_client`, `loguru` |
| **Market data** | `binance-connector`, `coinbase-advanced-py` |

#### Infrastructure tools (installed by scripts)

| Tool | Purpose |
|------|---------|
| **pyenv** | Python version management |
| **pm2** | Process management |
| **tmux** | Log multiplexing |
| **nvm** | Node.js for pm2 |
| **prometheus-node-exporter** | Resource monitoring |

#### C++ simulator stack (validators only)

| Component | Details |
|-----------|---------|
| **Engine** | MAXE-based agent simulation |
| **Build** | cmake + vcpkg + g++-14 |
| **Binding** | Pybind11 to Python validator |
| **Background model** | Chiarella et al. 2007, Vuorenmaa & Wang 2014 |

#### Cloud/storage (GenTRX)

| Service | Purpose |
|---------|---------|
| **Cloudflare R2** or **Hippius** | S3-compatible bucket for gradients/checkpoints |
| **On-chain commitments** | Bucket discovery via Bittensor chain |

### Agent development stack

| Layer | Technology |
|-------|------------|
| **Language** | Python 3.10.9 |
| **Agent base class** | `FinanceSimulationAgent` / `GenTRXAgent` |
| **Protocol** | Pydantic models (`MarketSimulationStateUpdate`, `FinanceAgentResponse`) |
| **Instructions** | Market/limit orders, cancels, close positions |
| **Testing** | Local proxy (`agents/proxy/`) |
| **Examples** | `~/.taos/agents/` (copied from `agents/` on install) |

### Registration & wallets

| Item | Details |
|------|---------|
| **Registration** | Bittensor wallet with TAO for UID registration |
| **Testnet** | Netuid 366 — request testnet TAO via Discord |
| **Mainnet** | Netuid 79 |
| **Endpoints** | `wss://entrypoint-finney.opentensor.ai:443` (default) |

---

## Quick Reference: Key Files

| File | Purpose |
|------|---------|
| `README.md` | Subnet overview, install, run instructions |
| `agents/README.md` | Miner agent development guide |
| `taos/im/neurons/miner.py` | Miner neuron entry point |
| `taos/im/neurons/validator.py` | Validator neuron (simulator integration) |
| `taos/im/validator/reward.py` | Kappa + P&L + GenTRX scoring |
| `taos/im/validator/query.py` | Miner query & response validation |
| `taos/im/validator/forward.py` | Forward pass & delay application |
| `taos/im/utils/kappa.py` | Kappa-3 calculation |
| `taos/im/config/__init__.py` | All scoring hyperparameters |
| `taos/common/neurons/validator.py` | Two-pool weight allocation |
| `install_miner.sh` / `run_miner.sh` | Miner setup & launch |
| `install_validator.sh` / `run_validator.sh` | Validator setup & launch |
| `doc/gentrx/` | GenTRX distributed training docs |

---

## External Resources

| Resource | URL |
|----------|-----|
| Website | https://mvtrx.fi |
| Whitepaper | https://simulate.trading/taos-im-paper |
| Dashboard | https://taos.simulate.trading |
| Simulation Terminal | https://mvtrx.simulate.trading |
| Discord (τaos) | https://discord.com/channels/799672011265015819/1353733356470276096 |
| GitHub | https://github.com/taos-im/sn-79 |

---

*This document was generated from analysis of the sn-79 repository codebase and documentation. Scoring parameters may change as subnet owners tune the mechanism — always verify active validator config and the latest README for current defaults.*
