# Our SN-79 Prediction Agents — What They Predict and How They Trade

> **Scope:** Agents in `deployments/agent_table/ours.json` that use short-horizon price-direction signals to choose **which side** to quote (not when to take liquidity aggressively).  
> **Related:** Full strategy rationale and tuning lives in [`SN-79-prediction-agent-strategy-guide.md`](../SN-79-prediction-agent-strategy-guide.md).

---

## 1. What “prediction” means here

None of these agents forecast prices hours ahead. Each tick they estimate **next-tick to next-few-ticks direction** on each order book:

| Prediction target | Typical horizon | Used for |
|---|---|---|
| Short-term mid move (up / down / flat) | 1–5 sim ticks | Pick BUY vs SELL maker quote |
| Order-flow pressure (buyers vs sellers) | Current tick’s tape | Skip adverse side, favor passive fill side |
| Microprice vs mid | Instantaneous book shape | Bias toward side takers are likely to hit |

**Important:** Prediction only affects **new quote side selection** (and sometimes size). Every agent still relies on **round-trip completion** (opposite leg after a fill) to lock in realized PnL and feed κ₃ scoring.

```
Maker fill  →  queue completion  →  opposite limit inside spread  →  realized PnL observation
     ↑
  prediction picks which side to quote first (reduce adverse selection)
```

---

## 2. Shared signals (building blocks)

| Signal | Formula (concept) | Positive value implies |
|---|---|---|
| **OFI** (order flow imbalance) | `(buy_vol − sell_vol) / total_vol` from `book.events` | Buying pressure → mid likely up |
| **Microprice deviation** | `(microprice − mid) / spread`, EWM-smoothed | Microprice above mid → upward pressure |
| **Depth imbalance** | `(bid_depth − ask_depth) / total_depth` (top 5 levels) | More bid size → ask may get lifted |
| **Mid momentum** | EWM of recent mid returns | Continuation or mean-reversion depending on agent |
| **ML next-tick return** | `PassiveAggressiveRegressor` on feature vector | Predicted log return > 0 → up |

Microprice:

```text
microprice = (bid_price × ask_qty + ask_price × bid_qty) / (bid_qty + ask_qty)
```

---

## 3. Shared tick pipeline (all maker variants)

Every deployment agent runs the same high-level loop each validator tick:

1. **Startup / hygiene** — optional cancel-all; repay loans; cancel stale orders  
2. **Completions first** — after a maker fill, place opposite leg with minimum RT edge (ticks)  
3. **Inventory flatten** — if skew exceeds hard limit, reduce exposure (inside spread where fixed)  
4. **New quotes** — for books in today’s rotation bucket, apply prediction → pick side → post limit  
5. **Return** `FinanceAgentResponse` within validator time budget  

Rotation across 128 books (~8–12 buckets) keeps **kappa_penalty ≈ 0** (no book left inactive too long).

---

## 4. Agent-by-agent reference

### 4.1 AscendPredictAgent — `predict-1.0.0` (UID **127**)

| | |
|---|---|
| **PM2** | `79-predict` |
| **Profile** | Ascend **surge** + ML overlay |
| **Release** | predict-1.1.0 |

#### What it predicts

Per book, an online **PassiveAggressiveRegressor** predicts **next-tick log mid return**:

```text
y = log(mid_t / mid_{t-1})
```

**Features** (6-D vector each tick):

- spread ratio  
- microprice edge (ticks)  
- tape imbalance ratio  
- last-tick log return  
- top-of-book quantity imbalance  
- spread in ticks (scaled)

Training: after ≥8 samples per book, model updates every tick via `partial_fit`.

#### What it does with the prediction

Runs **on top of** the full Ascend surge stack (`ascend_score_tick`):

| Prediction output | Action |
|---|---|
| `pred > +threshold` (+0.002) | **Veto** new SELL quotes on that book (don’t sell into expected rise) |
| `pred < −threshold` | **Veto** new BUY quotes (don’t buy into expected fall) |
| Prediction agrees with inventory skew | **Size up** quote qty (up to `max_quantity`, `agree_size_k=0.5`) |
| Weak / neutral prediction | Base Ascend logic unchanged |

Completions, flatten legs, and requote hints stay at **base quantity** — overlay only sizes/vetoes **new** quotes.

#### How it works (flow)

```text
tick → PredictOverlay.overlay()
         ├─ update PA regressor per book (time-budget 400 ms, max 13 books)
         ├─ emit book_quote_qty{} and book_pred_sign{} (veto map)
         └─ pass into ascend_score_tick(..., book_quote_qty, book_pred_sign)
              └─ _pred_vetoes_new_quote() blocks quotes against predicted direction
```

**Strength:** Rich Ascend infrastructure (rotation, spread gates, completion discipline) + ML filter.  
**Risk:** CPU budget; cold-start until 8+ training samples per book.

---

### 4.2 PredictiveMakerAgent — `predictive-maker-1.0.0` (UID **pending** / was 18)

| | |
|---|---|
| **PM2** | `79-predictive-maker` |
| **Option** | **A** — multi-signal maker |
| **Release** | predictive-maker-1.2.0 |

#### What it predicts

Rule-based **combined signal** `S ∈ [−1, +1]` from `_predictive_signals.SignalEngine`:

```text
S = 0.35×OFI_EWM + 0.30×micro_EWM + 0.20×depth_imb + 0.15×momentum_norm
```

All components are EWM-smoothed (`alpha=0.30`) per book.

#### What it does with the prediction

| Condition | Quote action |
|---|---|
| `S > +0.15` (strong) | Place **SELL** only (inside ask: `ask − 1 tick`) |
| `S < −0.15` (strong) | Place **BUY** only (inside bid: `bid + 1 tick`) |
| `|S| ≤ 0.15` weak | Use **inventory skew** or alternate by book/tick |
| `|S| > 0.50` **and** quote would be adverse | **Skip** book entirely |
| Tape imbalance > 0.75 | Skip book (too one-sided / toxic) |

After any maker fill → **completion queue** with 4-tick RT edge, inside spread, postOnly, GTT.

#### How it works

Standalone `FinanceSimulationAgent` (no Ascend base). Books scored by spread + `|S|` + tape calmness; top 8 per tick. Spread gate: `≥ max(6, 2×inside + edge + 1)` ticks.

**Strength:** Highest signal richness; strong adverse-selection filter.  
**Trade-off:** More moving parts; needs clean completion logic (v1.2.0 fixes).

---

### 4.3 AdaptiveSteadyMaker — `adaptive-steady-1.0.0` (UID **48**)

| | |
|---|---|
| **PM2** | `79-adaptive-steady` |
| **Option** | **B** — inside-spread + microprice only |
| **Release** | adaptive-steady-1.2.0 |

#### What it predicts

**Microprice deviation only** (`_adaptive_signals.MicropriceSignalEngine`):

```text
micro_dev = (microprice − mid) / spread   →   EWM → signal
```

No OFI, depth, or ML — simplest directional estimate.

#### What it does with the prediction

| Condition | Quote action |
|---|---|
| `signal > +0.35×threshold` (~0.12) | Prefer **SELL** inside spread |
| `signal < −threshold` | Prefer **BUY** inside spread |
| Neutral | Inventory skew soft (8%) or book/tick alternation |
| Pending completion (< 6 attempts) | Block new quotes on that book |
| Spread < 5 ticks or fee too high | Skip |

**Completions (v1.2.0):** persistent hints until fill; escalate edge / drop postOnly after retries; detect completion fill in `onTrade` and clear hint.

#### How it works

Conservative maker: every fill is **1 tick inside** touch (not touch-join), 3-tick completion edge, 8 rotation groups × 10 books/tick. Optimized for **clean per-trip PnL** and penalty safety over raw fill rate.

**Strength:** Simple, robust signal; less adverse selection at entry.  
**Trade-off:** Lower fill rate → slower κ observation buildup unless completions close reliably.

---

### 4.4 MicropriceMomentumMaker — `microprice-momentum-1.0.0` (UID **pending**)

| | |
|---|---|
| **Option** | **C** — directional conviction gate |
| **Release** | bundle ready, not deployed |

#### What it predicts

**Conviction score** `D_raw` from `_momentum_signals.MomentumSignalEngine`:

```text
D_raw = 0.6 × momentum_norm + 0.4 × micro_dev
```

Momentum = EWM of last 3 mid returns, normalized by `0.002`.

#### What it does with the prediction

| Condition | Quote action |
|---|---|
| `|D_raw| ≥ 0.15` | Quote **one side only** at **touch** (touch-join): SELL if D>0, BUY if D<0 |
| `|D_raw| < 0.15` | **Skip book** (no hedge quote) |
| No quote for ≥ 36 ticks on book | **Coverage force** — quote once anyway for rotation |
| Tape imbalance > 0.75 | Skip |

Completions: 3-tick edge; same RT discipline as other makers.

#### How it works

Most selective agent: fewer quotes per tick, but each quote aligns with momentum + microprice. Requires careful rotation so all 128 books eventually get activity (penalty guard).

**Strength:** Highest per-quote selectivity.  
**Trade-off:** Skip-if-unclear reduces volume; touch-join = more adverse selection if signal wrong.

---

## 5. Comparison matrix

| Agent | UID | Predicts | Signal type | Quote style | Side selection | Completion edge |
|---|---|---|---|---|---|---|
| **AscendPredictAgent** | 127 | Next-tick log return | ML (6 features) | Ascend inside + surge | Veto adverse + size up agree | Ascend (4+ ticks) |
| **PredictiveMakerAgent** | pending | Combined pressure S | Rule (4 signals) | Inside spread | Strong/weak thresholds + skip | 4 ticks |
| **AdaptiveSteadyMaker** | 48 | Microprice vs mid | Rule (1 signal) | Inside spread | Micro + skew | 3 ticks (escalates) |
| **MicropriceMomentumMaker** | pending | Momentum + micro D | Rule (2 signals) | Touch-join | Conviction gate / skip | 3 ticks |

---

## 6. What prediction does **not** do

- Does **not** replace round-trip completions — κ₃ and PnL score use **realized** PnL from closed legs only.  
- Does **not** mark open inventory to market for scoring.  
- Does **not** guarantee positive PnL — wrong side + bad completion = large κ penalty (LPM₃ is cubed).  
- Does **not** apply to production Ascend agents (pulse / forge / apex) unless explicitly overlaid — those use spread/microprice gates inside `ascend_score_tick` but not the full prediction stacks above.

---

## 7. Deploy commands

```bash
cd /ttp/_tensor/sn-79
./run_deploy_predict.sh              # UID 127 — AscendPredictAgent
./run_deploy_predictive_maker.sh     # PredictiveMakerAgent
./run_deploy_adaptive_steady.sh      # UID 48 — AdaptiveSteadyMaker
./run_deploy_microprice_momentum.sh  # MicropriceMomentumMaker
```

Config lives in each bundle’s `miner.env` and agent params; see `deployments/agent_table/ours.json` for current UIDs and notes.

---

## 8. Reading metrics

| Metric | Meaning for prediction agents |
|---|---|
| `kappa` / `kappa_score` | Quality of **completed** round-trips (needs ≥3 RT samples per book in lookback) |
| `min_roundtrip_volume` | Recent RT activity — **0** means completions aren’t closing (κ stays dead) |
| `total_realized_pnl` | Cumulative realized — prediction should reduce adverse entries over time |
| `kappa_penalty` | Outlier books dragging median κ — rotation + consistent per-book behavior keeps this ~0 |

---

*Last updated: June 2026 — matches deployments adaptive-steady-1.2.0, predict-1.1.0, predictive-maker-1.2.0.*
