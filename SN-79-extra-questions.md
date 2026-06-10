# SN-79 — Extra Questions (Beyond the Beginner's Guide)

> **Audience:** Readers of [SN-79-explainer-for-beginners.md](./SN-79-explainer-for-beginners.md) who want deeper answers  
> **Subnet:** Bittensor netuid **79** (mainnet) / **366** (testnet)  
> **Package:** τaos **0.4.5**  
> **Related:** [Beginner's Guide](./SN-79-explainer-for-beginners.md) · [Miner Workflow with Examples](./SN-79-miner-workflow-with-examples.md) · [Miner ↔ Validator Protocol](./SN-79-miner-validator-protocol.md) · [Miner Comparison Guide](./SN-79-miner-comparison-guide.md)

---

## Table of Contents

1. [What do miners do after receiving state (besides decompress)?](#1-what-do-miners-do-after-receiving-state-besides-decompress)
2. [How do miners decide cancel, buy, or sell for `FinanceAgentResponse`?](#2-how-do-miners-decide-cancel-buy-or-sell-for-financeagentresponse)
3. [Why are miner scores different from each other?](#3-why-are-miner-scores-different-from-each-other)
4. [How does the validator estimate and score what miners send back?](#4-how-does-the-validator-estimate-and-score-what-miners-send-back)
5. [How can we increase incentive score?](#5-how-can-we-increase-incentive-score)
6. [Incentive calculation end-to-end, and how to grow it fast](#6-incentive-calculation-end-to-end-and-how-to-grow-it-fast)

---

## 1. What do miners do after receiving state (besides decompress)?

A **tick** is one validator query. The word “ticket” in informal discussion usually means this tick’s state update — not a separate object miners must decode beyond `MarketSimulationStateUpdate`.

### 1.1 Full miner pipeline (one tick)

```
Validator synapse arrives
    ↓
Neuron: decompress (LZ4/zlib/zstd)
    ↓
Agent.handle(state):
    1. update(state)     ← ingest + process feedback
    2. respond(state)    ← build instructions (your strategy)
    3. report(state, response)  ← logging
    ↓
Neuron: clear_inputs() → compress response → return synapse
```

The neuron (`taos/im/neurons/miner.py`) only handles networking. **All trading logic is in the agent.**

### 1.2 `update(state)` — mandatory ingestion

The base class (`FinanceSimulationAgent.update`) does this every tick:

| Step | What happens |
|------|----------------|
| **History** | Appends state to rolling history (default last 10 ticks) |
| **Portfolio** | `self.accounts = state.accounts[your_uid]` — balances, open orders, loans, fees per book |
| **Feedback** | `self.events = state.notices[your_uid]` — fills, rejects, cancels from **last** tick |
| **Config** | `self.simulation_config = state.config` — decimals, limits, book count |
| **Event dispatch** | Routes each notice to handlers: `onTrade`, `onOrderAccepted`, `onOrderRejected`, etc. |

**Miners do not “apply” instructions themselves.** They only **read** the snapshot the validator already built from the previous simulator run.

### 1.3 What miners derive from the tick (not sent pre-computed)

From `state.books`, `self.accounts`, and `self.events`, agents typically compute:

- Best bid / best ask, mid, spread, spread ratio
- Microprice, tape imbalance (from `book.events`)
- Inventory skew per book
- Which books are in today’s rotation bucket
- Internal state: pending completion legs, last mid prices, requote hints

### 1.4 `respond(state)` — build the reply

Returns `FinanceAgentResponse(agent_id=your_uid, instructions=[...])`.

Typical priority order (SteadyMaker / Turbo families):

1. **Startup** — cancel all resting orders once (if configured)
2. **Repay loans** — FIFO settlement orders where needed
3. **Cancel stale** — orders off the touch (free instruction slots)
4. **Complete round-trips** — opposite-side limit after a maker fill (`onTrade` hint)
5. **Flatten inventory** — reduce skew if too long BASE or QUOTE
6. **New quotes** — maker limits on rotated books that pass filters

### 1.5 `report(state, response)` — logging only

Writes instructions and state summary to logs/CSV. **Does not affect scoring.**

### 1.6 After return — not the miner’s job

Once the miner returns:

1. Validator validates UID, instruction limits, volume caps
2. **Latency delay** added (slow responses → worse execution timing)
3. C++ simulator executes accepted instructions
4. **Next tick’s `notices`** tell the miner what actually filled or failed

If the miner times out (~3 s), the **entire response is dropped** — zero orders that tick.

---

## 2. How do miners decide cancel, buy, or sell for `FinanceAgentResponse`?

There is **no single protocol rule** for buy vs sell. Each agent implements a **decision pipeline**. Competitive agents use **rule-based market microstructure logic**, not random orders.

### 2.1 Decision framework (general)

```
books + accounts + notices + internal memory
        ↓
   FILTER  — skip bad books (wide spread, bad fees, toxic tape)
        ↓
   PRIORITIZE — completion legs > flatten > new quotes
        ↓
   DIRECTION — buy, sell, or cancel per book
        ↓
   PRICE & SIZE — limit price inside spread, qty ≥ min size
        ↓
   BUDGET — ≤5 instructions/book, ~28 total, balances, loans
        ↓
   FinanceAgentResponse
```

### 2.2 When to **cancel**

| Trigger | Rule (example) |
|---------|----------------|
| **Stale quote** | Buy price &lt; best_bid − ½ tick, or sell price &gt; best_ask + ½ tick |
| **Requote** | Cancel before posting a new limit on same book (instruction budget) |
| **Risk** | Flatten mode: cancel all, then only risk-reducing orders |
| **Startup** | One-time cancel-all of resting orders |

Cancel does **not** directly add score, but frees slots and avoids being picked off on bad prices.

### 2.3 When to **buy** vs **sell**

| Signal | Typical action |
|--------|----------------|
| **Maker fill (onTrade)** | You sold as maker → queue **BUY** completion; bought → queue **SELL** |
| **Inventory skew &gt; 0** | Too much BASE → prefer **SELL** |
| **Inventory skew &lt; 0** | Too much QUOTE → prefer **BUY** |
| **Microprice &gt; mid** | Short-term upward pressure → **BUY** quote (SteadyMaker) |
| **Microprice &lt; mid** | Downward pressure → **SELL** quote |
| **Rotation bucket** | Only quote books where `book_id % groups == current_bucket` |
| **Flatten / survive** | One careful limit on skewed side only |

**Market orders** (immediate taker) are usually reserved for emergency flattening — they pay spread + taker fees and hurt κ if used heavily.

### 2.4 SteadyMaker example (deploy agents)

From `steady_maker_score_tick` in `agents/competitive_utils.py`:

1. Filter books: spread ≥ 7 ticks, round-trip edge ≥ 6 ticks, tape imbalance &lt; 28%, maker fee OK
2. Cancel stale on all candidate books
3. **Phase 0 — completion:** after maker fill, place opposite limit **inside** spread only if edge vs fill price ≥ `min_rt_edge_ticks`
4. **Phase 1 — flatten:** if `|inventory_skew| ≥ soft threshold`, place reducing-side limit
5. **Phase 2 — new quotes:** on rotation-matched books, use microprice edge (≥ 1.5 ticks) or mild skew to pick BUY vs SELL; skip if edge too small

`onTrade` in `_steady_maker_base.py` records `(completion_side, fill_price)` per book for the next `respond()`.

### 2.5 Hard gates before any instruction

| Check | If failed |
|-------|-----------|
| `agent_id == uid` | All instructions discarded |
| `quantity ≥ 0.25` BASE (typical) | Simulator reject |
| Free balance covers order | Reject |
| Loan headroom | `EXCEEDING_LOAN` reject |
| ≤ 5 instructions per book | Excess dropped |
| Volume cap on book | New orders blocked (cancels OK) |
| `postOnly` crossing spread | Reject |

---

## 3. Why are miner scores different from each other?

Scores differ because **validators measure outcomes**, not effort. Same protocol, different **algorithms, parameters, infrastructure, and market luck**.

### 3.1 What the score actually is

**Trading score** (main incentive driver, ~95% of rewards when GenTRX pool is small):

```
TradingScore = 0.79 × KappaScore + 0.21 × PnLScore   (defaults)
```

**On-chain incentive** (metagraph) is an **EMA-smoothed function of validator weights**, which come from trading (+ optional GenTRX) scores. It **lags** the live dashboard by minutes to hours.

### 3.2 Sources of score differences

| Factor | Effect |
|--------|--------|
| **Algorithm / strategy** | Maker vs taker mix, spread capture, completion logic, book selection |
| **Parameters** | `min_spread_ticks`, rotation speed, qty, skew thresholds — same algo, different κ |
| **Round-trip quality** | Positive realized PnL per trip vs bleeding on every completion |
| **κ₃ (risk-adjusted)** | Steady small wins beat volatile losses; big drawdowns crush Kappa |
| **Book coverage** | Need activity on many books; median aggregation punishes weak outliers |
| **Activity factor** | Too little volume → lower Kappa weight; too much losing volume → worse PnL |
| **Penalty (IQR outliers)** | A few terrible books drag median Kappa down even if others are good |
| **History length** | New miners need ≥3 realized round-trip observations per book + lookback window |
| **Infrastructure** | Timeouts = zero orders; latency delay = worse fills; `lazy_load` vs full decompress |
| **Reject storms** | Wrong qty decimals, loans, min size → no fills → no score data |
| **Competition** | Other miners take the good fills; sim is zero-sum-ish on each book |
| **Immunity / registration** | Fresh UID may show low or zero score until enough sim time accumulates |

### 3.3 Algorithm families (this repo)

| Family | Character | Score profile |
|--------|-----------|---------------|
| **SteadyMaker** | Wide spreads, microprice gate, completion legs | Targets positive κ; lower volume |
| **Turbo v2** | More books/tick, touch-near quotes, requote | High RT volume; can bleed realized PnL |
| **SimpleRegressor / ML** | Signal from features → directional limits | Depends on model edge |
| **RandomMaker / RandomTaker** | Testing only | Near-zero competitive score |

**Parameters matter as much as algorithm.** Turbo “power” vs SteadyMaker “apex” on the same codebase can produce opposite realized PnL.

### 3.4 Why score stays low (beyond “bad algorithm”)

| Symptom | Common cause |
|---------|----------------|
| **Kappa = 0 / None** | &lt; 3 round-trip observations per book in lookback |
| **κ negative** | Completing round-trips at a loss after fees |
| **Penalty &gt; 0** | Some books much worse than your median (IQR outlier rule) |
| **Realized PnL falling** | Taker churn, crossing spread, adverse selection |
| **Score flat after deploy** | EMA smoothing; need hours of sim time |
| **Incentive &lt;&lt; dashboard score** | Weight-setting cadence + multi-validator consensus |

---

## 4. How does the validator estimate and score what miners send back?

Miners send **instructions** (limit, market, cancel, close). Validators do **not** score the instructions directly — they score **executed trades** and **realized PnL** from the simulator.

### 4.1 Instruction → execution chain

```
FinanceAgentResponse.instructions[]
    ↓
Validator pre-checks (UID, book ID, volume cap, ≤5/book)
    ↓
Latency delay applied (response time penalty)
    ↓
C++ simulator (taosim) — matching engine
    ↓
Fills, rejects, cancellations
    ↓
Next tick: notices[] to miner
    ↓
Validator _update_trade_volumes() — FIFO PnL accounting
    ↓
Every ~5s sim: kappa_3() + PnL score → TradingScore
```

**Failed or dropped instructions contribute nothing** to score.

### 4.2 How round-trips and realized PnL are “estimated”

The validator keeps an **open position ledger per UID per book** (`open_positions`):

- **Buy** with no open short → opens a **long** lot `(timestamp, qty, price, fee)`
- **Sell** with no open long → opens a **short** lot
- **Buy** when shorts exist → **FIFO match** against oldest short → **realized PnL**
- **Sell** when longs exist → FIFO match against oldest long → **realized PnL**

Core logic: `_match_trade_fifo()` in `taos/im/neurons/validator.py`:

```
realized_pnl = price_difference × matched_qty − open_fee − close_fee
roundtrip_volume = matched_qty (when a leg closes an opposite leg)
```

Fees (maker rebate or taker cost) are included in realized PnL.

Each scoring interval, realized PnL is stored in:

```
realized_pnl_history[uid][timestamp][book_id] = pnl
```

Round-trip **volume** (QUOTE notional) goes to:

```
roundtrip_volumes[uid][book_id][sampled_timestamp] += qty × price
```

### 4.3 How κ₃ is computed from that history

`kappa_3()` (`taos/im/utils/kappa.py`) per book:

1. Build time series of **realized PnL per scoring timestamp**
2. Require **≥ 3 non-zero observations** in lookback (else κ = undefined for that book)
3. Normalize by per-book MAD (scale-invariant)
4. Compute κ₃ = (μ − τ) / LPM₃(τ)^(1/3) — downside risk penalized heavily

Then `calculate_kappa_score()` (`reward.py`):

1. Normalize per-book κ to [0, 1]
2. Multiply by **activity factor** (round-trip volume vs cap; decay when idle)
3. Optionally multiply by **PnL factor** per book
4. Allow up to **37.5%** of books without κ data (no penalty)
5. **Outlier penalty** on books far below median (1.5× IQR)
6. **Median** across books → **KappaScore**

### 4.4 PnL score (21%)

`calculate_pnl_score()`:

- Sum realized PnL per book over lookback
- Convert to daily return vs allocated capital (`miner_wealth / book_count`)
- **Median** across books → map to roughly [−0.5, +0.5]

### 4.5 What is *not* scored from instructions

| Not scored | Why |
|------------|-----|
| Unfilled limit orders | No trade → no realized PnL |
| Cancel-only ticks | No new round-trip data |
| Rejected orders | Appear in `notices` as failures only |
| Unrealized inventory PnL | Open positions not in κ/PnL formula today |
| Instruction count / cancel rate | No direct penalty (but wastes budget) |

### 4.6 Timing summary

| Event | Frequency |
|-------|-----------|
| State update to miners | ~1 sim second (`publish_interval`) |
| Scoring assessment | ~5 sim seconds (`scoring.interval`) |
| Kappa lookback | ~1.5–3 sim hours of data |
| Weight / incentive EMA | Slower; on-chain updates lag dashboard |

---

## 5. How can we increase incentive score?

**Incentive** on the dashboard and metagraph is the on-chain reward share (`metagraph.incentive[uid]`). It follows **validator weights**, which follow **smoothed trading (+ optional GenTRX) scores**. You cannot bump incentive directly — you improve the inputs validators measure.

### 5.1 The causal chain

```
Better round-trip economics
    ↓
Higher KappaScore + PnLScore
    ↓
Higher TradingScore (0.79 κ + 0.21 PnL)
    ↓
Validator weights (EMA over time)
    ↓
Higher incentive + emission on metagraph
```

GenTRX adds a second pool (~5% default when active): gradient quality on held-out data, rank-normalized per round.

### 5.2 Trading score — concrete levers

| Lever | Action | Why it helps |
|-------|--------|--------------|
| **1. Profitable round-trips** | Maker inside wide spread; complete opposite leg with edge | Positive realized PnL → κ and PnL components |
| **2. Low downside volatility** | Avoid taker churn, leverage blowups, touch-crossing | κ₃ punishes large losses (LPM₃) |
| **3. Get κ observations** | ≥3 completed round-trips per book in lookback | Undefined κ = 0 contribution from that book |
| **4. Book coverage** | Rotate across 128 books over hours | Median score needs breadth; ≤37.5% inactive OK |
| **5. Kill outlier books** | Stop trading books where you lose consistently | **Penalty = 0** on dashboard (IQR rule) |
| **6. Activity without spam** | Steady RT volume below cap | Activity factor boosts κ; cap breach freezes book |
| **7. Infrastructure** | `lazy_load=1`, respond &lt;1s, no debug spam | Timeouts = zero score that tick; latency = worse fills |
| **8. Clean execution** | `qty ≥ 0.32` (finney rounding), repay loans, ≤5 instr/book | Rejects = no fills = no data |
| **9. Wait for EMA** | Run stable strategy 3–6+ sim hours | Score and incentive ramp gradually |

### 5.3 Strategy checklist (aligned with top miners)

1. **Pre-check** balances and loan headroom every tick  
2. **Cancel stale** before new quotes  
3. **Quote inside spread** as maker (`postOnly` where appropriate)  
4. **Complete round-trips** after maker fills with edge vs fill price  
5. **Rotate books** — do not concentrate on 2–3 books forever  
6. **Skip toxic books** — wide spread you cannot capture, bad fees, one-sided tape  
7. **Control inventory** — flatten skew before adding risk  
8. **Monitor** [taos.simulate.trading](https://taos.simulate.trading): Kappa, Penalty, Realized PnL, RT volume, Requests (timeouts)

### 5.4 What *not* to do (common score killers)

| Mistake | Result |
|---------|--------|
| Aggressive Turbo / touch-crossing on many books | Negative realized PnL, κ → 0 or negative |
| Market orders + leverage everywhere | `EXCEEDING_LOAN`, no fills |
| qty 0.28–0.31 without rounding fix | `MINIMUM_ORDER_SIZE_VIOLATION` |
| &gt;5 instructions per book | Validator drops excess |
| Slow agent / heavy logging | 3s timeout → entire tick wasted |
| Ignoring bad books while winning elsewhere | **Kappa Penalty** &gt; 0 |
| Expecting instant incentive after restart | EMA + lookback windows need time |

### 5.5 Operational playbook (deploy miners)

| Phase | Goal | What to watch |
|-------|------|----------------|
| **Hour 0–2** | Stop reject storms; get fills | Logs: `FAILED TO PLACE`, timeouts |
| **Hour 2–6** | κ observations appear | Dashboard: Kappa not None; ≥3 RT per book |
| **Hour 6–12** | Realized PnL flat or rising | `total_realized_pnl` in agent table snapshots |
| **Hour 12+** | Penalty → 0, κ positive | Trading score ↑ → incentive follows |

For our SteadyMaker v1.2 deploy: prioritize **realized PnL stop bleeding** first, then κ quality, then volume.

### 5.6 GenTRX path (optional ~5% pool)

If running GenTRX (`-G` / `GenTRXAgent`):

- Submit gradients that improve held-out loss vs peers (rank-normalized each round)
- Training score is **relative** — compared to other active gradient miners
- Does not replace trading score; supplements it when `gentrx.simulation_share` &gt; 0

See [doc/gentrx/miner_setup.md](./doc/gentrx/miner_setup.md).

### 5.7 How to verify incentive is rising

| Source | Metric |
|--------|--------|
| Dashboard Agent page | **Trading Score**, **Kappa Score**, **Penalty**, **Realized PnL** |
| Dashboard top row | **Incentive**, **Emission**, **Trust**, **Consensus** |
| CLI | `btcli subnet metagraph --netuid 79` → `IN incentive` column |
| Agent table JSON | `score`, `kappa`, `penalty`, `total_realized_pnl` over time |

**Rule of thumb:** If Trading Score and rank (Pos) rise on the validator dashboard for 6+ hours with Penalty ≈ 0, incentive on the metagraph should follow within the EMA window. If dashboard score is high but incentive is low, check multi-validator consensus and weight lag ([comparison guide §5](./SN-79-miner-comparison-guide.md#5-on-chain-comparison)).

---

## 6. Incentive calculation end-to-end, and how to grow it fast

This section ties together **what “incentive” actually is** on-chain and a **time-ordered sprint plan** for moving it up as quickly as the protocol allows. “Fast” here means sim-hours, not seconds — several layers of lookback and smoothing sit between a good fill and `metagraph.incentive`.

### 6.1 One diagram: trade → incentive

```
Each tick (~1 sim second)
    Miner instructions → simulator fills → FIFO realized PnL per book
        ↓
Every ~5 sim seconds (scoring.interval)
    κ₃ per book (need ≥3 round-trips in lookback)
    → activity factor (volume) → outlier penalty → median = KappaScore
    → median daily PnL return = PnLScore
    → TradingScore = 0.79 × KappaScore + 0.21 × PnLScore   [0, 1]
        ↓
Same round, all miners
    Pareto sort-multiply on trading scores (rank compression — top miners get disproportionate share)
        ↓
Slow EMA (moving_average_alpha ≈ 0.0083 per scoring round)
    self.scores[uid] ← α × pareto_reward + (1−α) × old_score
        ↓
Weight setting (periodic)
    L1-normalize scores → trading pool (~95%) + optional GenTRX pool (~5%)
    → process_weights_for_netuid → set_weights on chain
        ↓
Metagraph
    incentive[uid]  (your emission share on subnet 79)
```

**Key insight:** Incentive is **not** your last tick’s PnL. It is a **rank-weighted, EMA-smoothed share** of a pool that itself is built from **hours** of per-book median κ and PnL.

### 6.2 The formulas (defaults, τaos 0.4.5)

| Stage | Formula / rule | Typical value |
|-------|----------------|---------------|
| **Per-book κ₃** | (μ − τ) / LPM₃(τ)^(1/3) on realized-PnL time series | τ = 0; needs **≥ 3** non-zero RT observations |
| **Kappa lookback** | Window of realized PnL samples | **1.5–3 sim hours** (`min_lookback` / `lookback`) |
| **Activity factor** | Scales κ up when round-trip volume is healthy; decays when idle | Capped by `capital_turnover_cap` per book |
| **Outlier penalty** | Books far below median (1.5× IQR) → subtract up to ~67% of gap | Dashboard **Penalty** column |
| **KappaScore** | `max(median(activity_weighted_κ) − penalty, 0)` | [0, 1] |
| **PnLScore** | Median daily return vs `miner_wealth / book_count` | ~[−0.5, +0.5] → blended at 21% |
| **TradingScore** | `0.79 × KappaScore + 0.21 × PnLScore` | [0, 1] |
| **Pareto layer** | Sorted trading scores × random Pareto weights | Rewards **relative rank**, not absolute score |
| **Score EMA** | `scores ← α × reward + (1−α) × scores` | α ≈ **0.0083** → many scoring rounds to fully reflect a step change |
| **On-chain weight** | Normalized EMA scores → two-pool split → uint16 weights | Multi-validator **consensus** lags single dashboard |

GenTRX (optional): separate rank-normalized gradient score with its own EMA; does not replace trading score when the pool is small.

### 6.3 Why incentive lags the dashboard

| Layer | What you feel | Rough sim-time |
|-------|----------------|----------------|
| Fill → realized PnL | Immediate in next tick’s `notices` | **1 tick** |
| κ appears per book | After 3+ completed round-trips in window | **~30–90 min** of active quoting |
| KappaScore / TradingScore stable | Lookback fills with good trips | **2–6 sim hours** |
| Penalty → 0 | Weak books stopped or fixed | **6–12 sim hours** |
| EMA `scores` catch up | Pareto reward averaged in | **6–24 sim hours** after score step-up |
| `metagraph.incentive` | Weight commits + multi-validator EMA | **Hours** after dashboard rank rises |

You cannot skip the lookback or the EMA. You **can** shorten the path by maximizing **fill rate × profitable completions** from hour 0 so the lookback window fills with **good** data instead of losses.

### 6.4 Fast-growth sprint (ordered by impact)

Use this as a checklist after deploy or a strategy change. Each step targets the **earliest bottleneck** in §6.1.

#### Phase A — First 30 sim minutes (infrastructure + data)

| # | Action | Unlocks |
|---|--------|---------|
| A1 | Respond **&lt; 1 s** every tick; `lazy_load=1`; no heavy logging | Avoid 3 s timeout (= zero instructions, zero score data) |
| A2 | Fix rejects: `qty ≥ 0.32`, loan headroom, ≤5 instr/book | Fills → FIFO PnL ledger |
| A3 | **Cancel stale** every tick before new quotes | Frees budget; avoids adverse pick-off |
| A4 | **Never return empty ticks** — bootstrap quote on cold books if needed | Continuous RT volume → activity factor |

#### Phase B — 30 min – 3 sim hours (κ observations)

| # | Action | Unlocks |
|---|--------|---------|
| B1 | **Touch-join** at bid/ask (maker, `postOnly`) on rotated books | Fill rate — top miners run **high RT volume** at the touch |
| B2 | **Complete** every maker fill with opposite limit **inside** spread; require edge vs fill price | Closed round-trips → κ inputs + realized PnL |
| B3 | **Rotate** across **128 books** (e.g. 10–11 books/tick, bucket every few ticks) | Median κ needs breadth; ≤37.5% inactive books allowed |
| B4 | **Flatten inventory** when skew exceeds soft threshold | Lean BASE (~5k vs ~10k) — less completion bleed |

#### Phase C — 3–12 sim hours (score + rank)

| # | Action | Unlocks |
|---|--------|---------|
| C1 | **Stop losing books** — drop books with persistent negative realized PnL | **Penalty → 0** (big κ multiplier) |
| C2 | Prefer **wide spread + inside quote** over touch-crossing / taker churn | Positive κ₃ (LPM₃ punishes downside) + PnLScore |
| C3 | Steady volume **below** per-book cap | Activity factor high without cap freeze |
| C4 | Hold strategy **stable** — no daily param whipsaw | Lookback window not polluted by mixed regimes |

#### Phase D — 12+ sim hours (incentive on chain)

| # | Action | Unlocks |
|---|--------|---------|
| D1 | Confirm dashboard: **Trading Score ↑**, **Pos** (rank) ↑, **Penalty ≈ 0** | Preconditions for weight share |
| D2 | Watch `btcli subnet metagraph --netuid 79` **IN** column | On-chain incentive (lags dashboard) |
| D3 | Compare agent table snapshots: `score`, `kappa`, `total_realized_pnl` trending up | Validates sprint before EMA catches up |

### 6.5 What top miners do differently (observable patterns)

From competitive agent-table snapshots, miners at **Pos 0–5** tend to share:

| Pattern | Weak miner symptom | Strong miner symptom |
|---------|-------------------|----------------------|
| **Fill rate** | Low RT volume, many unfilled inside limits | **High RT volume** (touch-join at bid/ask) |
| **Inventory** | BASE stuck ~10k+, skewed completions | **Lean BASE** ~4–7k, active flatten |
| **κ** | `None` or negative | **κ ≈ 0.8–1.0**, Penalty **0** |
| **Realized PnL** | Flat or falling despite volume | **Rising** with volume |
| **Book coverage** | 2–3 books only | **Rotation** across many books per hour |

**Anti-patterns that look “active” but kill short-term incentive:**

- Quoting **only inside** spread on wide books → almost no fills → κ stays `None`
- High volume **without** completion edge → negative realized PnL → κ crushed
- Winning on 20 books, bleeding on 10 → **Penalty &gt; 0** drags median κ
- Restarting strategy every hour → lookback never stabilizes → EMA never ramps

### 6.6 Minimal metrics to watch (sprint dashboard)

Check every 1–2 sim hours during a growth sprint:

```
□ Requests/timeouts     → should be ~0 timeouts
□ RT volume             → should climb toward top-quartile miners
□ Kappa                 → not None; trending positive
□ Penalty               → target 0
□ Realized PnL          → flat or up (not down with rising volume)
□ Trading Score + Pos   → up before expecting incentive ↑
□ metagraph.incentive   → lags; only trust after score stable 6+ hours
```

### 6.7 Repo-aligned defaults (Ascend / Flux family)

Our competitive deploy agents (`ascend_score_tick`, profiles `prime` / `surge` / `forge` / `flux`) encode the sprint above:

- Touch-join phase + fill-score ranking for **Phase B1**
- Completion legs with `min_rt_edge_ticks` for **B2**
- 10–11 books/tick, 12-bucket rotation for **B3**
- Inventory skew soft/hard + flatten budget for **B4**
- Cold-book bootstrap so ticks are never empty for **A4**

Tuning wider spreads or fewer books trades **speed of κ data** for **PnL safety** — for fastest *incentive* growth, prioritize **profitable round-trips at the touch** over raw instruction count.

---

## Quick reference

```
TICK IN:  decompress → update → respond → report → compress OUT
DECIDE:   filter books → cancel stale → complete/flatten → quote
SCORE:    executed trades → FIFO realized PnL → κ₃ per book → median → TradingScore
INCENTIVE: TradingScore (+ GenTRX) → EMA → weights → metagraph.incentive
```

---

*Document version: 2026-06-10 · τaos 0.4.5 · SN-79 mainnet netuid 79*
