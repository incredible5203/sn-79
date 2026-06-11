# SN-79 Scoring Postmortem & Agent Inventory

**Date:** 2026-06-11  
**Context:** Custom agents (`AscendKappaAgent`, `HybridResilientAgent`, `MicrostructureEdgeAgent`) on UID 196 failed to grow `total_realized_pnl` and `kappa_score` while keeping `kappa_penalty = 0`. UID 196 was subsequently switched to `AscendPulseAgent` (same engine as UID 65).

Metrics below are from `agent_table/agents_20260611T0617.json` unless noted.

---

## Part 1 — Why knowing the logic was not enough

### 1.1 What we were optimizing

The validator combines two signals:

```
TradingScore ≈ 0.79 × KappaScore + 0.21 × PnLScore
```

| Metric | What it measures | How to improve |
|--------|------------------|----------------|
| **κ₃ per book** | Risk-adjusted round-trip quality: `(μ − τ) / LPM₃^(1/3)` | Raise mean return μ; crush downside (LPM₃ is cubed — one bad loss hurts a lot) |
| **KappaScore** | Median of per-book normalized κ, weighted by activity & PnL factors, minus **outlier penalty** | Consistent quality across books; avoid dead or terrible books |
| **kappa_penalty** | IQR-based outlier penalty when some books lag far below median | Keep all ~128 books “alive” with acceptable κ — not just a few great ones |
| **total_realized_pnl** | Cumulative realized PnL from completed round-trips (not mark-to-market inventory) | Complete profitable round-trips; avoid forced loss exits |
| **PnLScore** | Median daily return per book vs allocated capital | Same as above, aggregated differently |

**Critical constraint:** These goals are coupled but **not aligned**. High fill rate (good for activity and penalty=0) increases adverse selection risk. Strict no-loss completion (good for LPM₃) reduces round-trip count (hurts activity). Aggressive quoting on all 128 books (good for penalty) dilutes edge.

Knowing the formula is necessary; **executing all constraints simultaneously in a live 256-miner simulation** is the hard part.

### 1.2 Reference: what “working” looks like (our Ascend miners)

| UID | Agent | κ (raw) | kappa_penalty | kappa_score | total_realized_pnl | combined_score |
|-----|-------|---------|---------------|-------------|-------------------|----------------|
| 65 | AscendPulseAgent (rocket) | 0.018 | 0 | 0.504 | **+8,771** | 0.398 |
| 10 | AscendApexAgent (rocket) | 0.007 | 0 | 0.501 | **+5,946** | 0.396 |
| 158 | AscendForgeAgent (surge) | 0.010 | 0 | 0.502 | **+9,317** | 0.397 |
| 196 | AscendKappaAgent → Hybrid (pre-switch) | None | None | None | **−4,380** | None |

Note: raw κ values look small (~0.01–0.02) because they are **unnormalized per-book statistics**. The exported `kappa_score` (~0.50) is the validator’s normalized, activity-weighted median. **Penalty = 0** is the key win — our Ascend stack achieves that while realized PnL is positive.

UID 196 before the pulse switch: high volume (11.9M daily) but **κ = None** (insufficient valid κ observations or failed aggregation) and deeply negative realized PnL.

### 1.3 Root causes — why custom agents failed

#### A. Wrong execution engine (architecture gap)

The proven miners (UID 10/65/158) use:

```
Ascend*Agent → AscendAgent (FinanceSimulationAgent) → ascend_score_tick()
```

`ascend_score_tick` in `competitive_utils.py` is a **battle-tested** ~500-line engine with:

- Completion-first legs after maker fills (edge vs fill price)
- Touch-join on wide spreads for fill rate
- Book rotation across 128 books (`book_rotation_groups` × `rotation_windows`)
- Dedicated flatten budget (`max_flatten_per_tick`)
- Microprice direction gate, cold-book sweep, inventory skew limits
- Instruction budget enforcement aligned with validator caps

Custom agents (`AscendKappaAgent`, `HybridResilientAgent`, `MicrostructureEdgeAgent`) were **standalone reimplementations** using `_sn79_compat.py`. They understood the scoring math in comments but did not inherit years of iterative tuning embedded in `ascend_score_tick` and profile defaults (`rocket`, `surge`, etc.).

**Knowing the logic ≠ shipping the same control loop.**

#### B. Validator data requirements (why κ was None)

Per-book κ needs **≥ 3 realized round-trip observations** in the lookback window. If the agent:

- Completes too few round-trips per book (stuck inventory, no-loss hold mode)
- Realizes losses that poison the window before enough clean trips accumulate
- Rotates books too slowly relative to lookback

…then κ stays `None` for many books → aggregation returns `kappa_score = None` / `score = 0`.

UID 196 traded heavily but **lost money on completions** — volume without quality does not score.

#### C. The μ vs LPM₃ tradeoff (why PnL bled)

κ₃ denominator is **LPM₃^(1/3)** — lower partial moment of losses. Custom agents had several loss paths:

| Failure mode | Agents affected | Effect |
|--------------|-----------------|--------|
| Forced/stuck escape completions at touch | Hybrid (removed in 1.0.2) | Realized losses → LPM₃ ↑, total_realized_pnl ↓ |
| GTC orders filling adversely after market moves | Kappa, Hybrid | Passive fills without timely completion |
| Blind ledger on PM2 restart | Hybrid | Wrong completion targets → bad exits |
| OFI skew overriding edge gates | Hybrid (fixed) | Directional bets into toxic flow |
| High `MAX_TAPE_IMBALANCE` (0.70) | Kappa | Quoted into one-sided toxic books |
| Two-lane “presence” quoting at min size | Hybrid | Round-trips with insufficient edge |

**Penalty = 0** requires touching every book; **profitable PnL** requires skipping bad books. Custom agents tried both jobs in one codebase and leaked PnL on the “presence” lane.

#### D. Simulation dynamics (environment, not just code)

Even with correct logic, each book is a **competitive order book** against ~250 other agents:

- Touch-join fills are often **adverse selection** (you get filled because price is moving against you)
- Completing inside the spread requires the market to mean-revert or move favorably within TTL
- `min_rt_edge_ticks` that is too low → many small losses; too high → inventory stuck → activity decay
- Volume cap (`capital_turnover_cap × miner_wealth`) can block new orders on hot books

The Ascend profiles (`rocket`, `surge`, …) represent **empirically tuned** balances of these tensions. Re-deriving them from first principles in a new agent is slow and error-prone.

#### E. Implementation bugs (fixable but costly)

During deployment we hit issues that silently degraded performance:

- `ModuleNotFoundError` / `@dataclass` import failures on lazy load
- `self.agent.config` None → miner crash loop
- `CompatFinanceAgentResponse` validation errors
- Hybrid `_process_notices` indentation bug (`book_id` before assignment)
- Missing tick logging (hard to diagnose fills vs instructions)

Each bug cost hours of “trading blind” while inventory and losses accumulated.

### 1.4 Summary — the gap between theory and results

| We knew | What still blocked us |
|---------|----------------------|
| κ₃ = μ / LPM₃^(1/3) | μ was not consistently positive; LPM₃ inflated by loss completions |
| Penalty = 0 needs 128-book coverage | Presence quoting at min edge leaked PnL |
| Activity factor rewards round-trip volume | High volume with losses is worse than moderate volume with wins |
| Completion after maker fill | Custom completion logic ≠ `ascend_score_tick` requote hints + edge gates |
| Instruction budget = 5/book, ~30/tick | Custom agents hit caps differently; cancel storms wasted ticks |

**Conclusion:** The scoring logic is public and understood. The moat is **integrated execution** — `FinanceSimulationAgent` + `ascend_score_tick` + tuned profiles + stable ops. That is why UID 196 was switched to `AscendPulseAgent` (UID 65 engine) rather than continuing to patch standalone agents.

---

## Part 2 — Currently running agents (PM2)

| PM2 name | UID | Hotkey | Port | Deployment | Agent class | Profile |
|----------|-----|--------|------|------------|-------------|---------|
| `79-turbopulsev2` | 65 | hot2 | 5202 | `pulse-v2-1.0.0` | `AscendPulseAgent` | rocket |
| `79-turboapexv2` | 10 | hot1 | 5201 | `apex-v2-1.0.0` | `AscendApexAgent` | rocket |
| `79-turboforgev2` | 158 | hot3 | 5203 | `forge-v2-1.0.0` | `AscendForgeAgent` | surge |
| `79-hybrid` | 196 | hot4 | 5204 | `hybrid-1.0.0` | `AscendRealizedAgent` | rocket + overlay |

UIDs 10/65/158 use the **Ascend stack** (`_ascend_agent_base.py` + `competitive_utils.py` + `ascend_score_tick`). UID 196 adds `_realized_overlay.py` on top.

### 2.4 AscendRealizedAgent — UID 196

- **File:** `AscendRealizedAgent.py` → extends `AscendAgent` with `RealizedOverlay`
- **Profile:** `rocket` (same engine as UID 65)
- **Overlay:** OFI toxicity veto, inventory gate (complete before new quotes), per-book realized-loss risk-off
- **Deploy:** `./run_deploy_hybrid.sh` → PM2 `79-hybrid`

### 2.1 AscendPulseAgent — UID 65 & 196

- **File:** `AscendPulseAgent.py` → extends `AscendAgent`
- **Default profile:** `surge` (overridden to `rocket` in `miner.env`)
- **Strategy:** High-growth maker — aggressive rotation (10 groups × 10 windows), 12–13 books/tick, touch-join, completion-first, cold-book sweep, `inactive_book_frac=0` (no book skipped)
- **UID 65 params:** `cancel_all_on_startup=0`, rocket defaults (13 books/tick)
- **UID 196 params:** `cancel_all_on_startup=1`, `max_books_per_tick=12` (slight de-tune vs 65)

### 2.2 AscendApexAgent — UID 10

- **File:** `AscendApexAgent.py` → `default_ascend_profile = "apex"`
- **Deployed as:** `ascend_profile=rocket` (miner.env override — same engine params as pulse)
- **Intent:** Originally “PnL-first” with slightly higher `min_rt_edge_ticks` in apex profile; production uses rocket for speed

### 2.3 AscendForgeAgent — UID 158

- **File:** `AscendForgeAgent.py` → `default_ascend_profile = "forge"`
- **Deployed as:** `ascend_profile=surge`
- **Strategy:** Slightly more conservative edges than rocket (`min_rt_edge_ticks=3.0`, `min_completion_rt_edge_ticks=4.5` in surge/forge vs 2.5/4.0 in rocket)

---

## Part 3 — All deployment bundles (`deployments/`)

Each bundle is a **frozen directory**: `agents/`, `miner.env`, `run.sh`, `RELEASE.txt`. Deploy via `run_deploy_*.sh` at repo root.

### 3.1 Production Ascend family (v2 — active)

| Directory | Run script | Primary agent | UID | Status |
|-----------|------------|---------------|-----|--------|
| `pulse-v2-1.0.0` | `run_deploy_pulse_v2.sh` | `AscendPulseAgent` | 65 | **Running** |
| `apex-v2-1.0.0` | `run_deploy_apex_v2.sh` | `AscendApexAgent` | 10 | **Running** |
| `forge-v2-1.0.0` | `run_deploy_forge_v2.sh` | `AscendForgeAgent` | 158 | **Running** |
| `kappa-1.0.0` | `run_deploy_kappa.sh` | `AscendPulseAgent` | 196 | **Running** (repurposed from kappa) |

**Shared core files (per bundle):**

| File | Role |
|------|------|
| `_ascend_agent_base.py` | `AscendAgent` class — profile loading, `onTrade` requote hints, `respond()` → `ascend_score_tick` |
| `competitive_utils.py` | `ascend_score_tick`, instruction budgets, book selection, quote/completion/flatten logic |
| `AscendPulseAgent.py` / `AscendApexAgent.py` / `AscendForgeAgent.py` | Thin wrappers setting `agent_label` and default profile |

**Ascend profiles** (in `_PROFILE_DEFAULTS`):

| Profile | Character |
|---------|-----------|
| `rocket` | Fastest — 13 books/tick, lowest edges (2.5 ticks), highest aggression |
| `surge` / `forge` / `apex` | Slightly tighter edges (3.0 ticks), similar rotation |
| `prime` | Middle ground |
| `flux` | Rocket-like with tighter inventory hard limit |
| `recover` | Conservative fallback (not used in production) |
| `blitz` | Used by FluxPrimeAgent in ascend bundle |

**Legacy agents still bundled but not deployed:**

- `TurboPulseV2Agent`, `TurboApexV2Agent`, `TurboForgeV2Agent` — older turbo v2 engine (`_turbo_v2_agent_base.py`)
- `SteadyPulseAgent`, `SteadyApexAgent`, `SteadyForgeAgent` — conservative maker (`_steady_maker_base.py`)

### 3.2 Custom / experimental bundles (not running)

#### `hybrid-1.0.0` — HybridResilientAgent

| Field | Value |
|-------|-------|
| **Run script** | `run_deploy_hybrid.sh` |
| **PM2** | `79-hybrid` (stopped) |
| **Target UID** | 196 (superseded by kappa-pulse) |
| **Release** | `hybrid-1.0.2` |

**Design:** Two-lane architecture — Lane 1 presence on all 128 books (penalty=0), Lane 2 OFI-directed alpha on rotating subset (PnL growth). Per-book VWAP ledger, no-loss completion rule, circuit breaker, hold mode for stuck books.

**Why retired:** Despite sophisticated design, accumulated realized losses from presence-lane round-trips, stuck inventory, and restart ledger gaps. κ never stabilized; total_realized_pnl trended negative. Patched through 1.0.1 → 1.0.2 without matching Ascend results.

**Key files:** `HybridResilientAgent.py`, `_sn79_compat.py`

#### `kappa-1.0.0` — (historical) AscendKappaAgent

| Field | Value |
|-------|-------|
| **Originally** | `AscendKappaAgent` — touch-join + inside completer + panic guard |
| **Now** | `AscendPulseAgent` (pulse stack copied from `pulse-v2-1.0.0`) |
| **Legacy file** | `AscendKappaAgent.py` still on disk but unused |

**Original design:** Adaptive touch-join MM with microstructure filtering. `MAX_BOOKS_PER_TICK=11`, `MIN_RT_EDGE_TICKS=2`, high tape imbalance tolerance (0.70).

**Why original failed:** Standalone engine; aggressive fills without Ascend’s completion/flatten discipline; κ=None despite volume; total_realized_pnl −4,380 at T0617.

#### `micro-1.0.0` — MicrostructureEdgeAgent

| Field | Value |
|-------|-------|
| **Run script** | `run_deploy_micro.sh` |
| **PM2** | `79-micro` (not started) |
| **Target UID** | 209 (hot4) — conflicts with kappa on same hotkey/port |

**Design:** Selective OFI + queue-depth + cross-book median signals. Trades less often, targets higher per-trip edge (`MIN_RT_EDGE_TICKS=3`). Complementary book rotation offset vs kappa.

**Status:** Ready to deploy but not running; needs separate UID/hotkey or stop kappa first.

#### `ascend-1.0.0` — Flux / Vault / Prime experiments

| Field | Value |
|-------|-------|
| **Run script** | `run_deploy_ascend.sh` |
| **PM2** | `79-flux` (not running) |
| **Target UID** | 209 |

**Agents in bundle:**

| Agent | Base | Purpose |
|-------|------|---------|
| `FluxPrimeAgent` | `AscendAgent` | `blitz` profile + optional exchange logging |
| `AscendPrimeAgent` | `AscendAgent` | General prime profile |
| `AscendAgent` | `AscendAgent` | Generic ascend |
| `VaultPrimeAgent` | `_vault_agent_base` | Separate vault engine (`vault_engine.py`) |
| `validator_exchange_log.py` | — | Debug logging helper |

**miner.env default:** `FluxPrimeAgent`, `ascend_profile=rocket`, `cancel_all_on_startup=1`

**Status:** Experimental UID 209 slot; superseded in planning by micro or pulse clones.

---

## Part 4 — Agent architecture comparison

```
┌─────────────────────────────────────────────────────────────────┐
│  PROVEN PATH (UID 10, 65, 158, 196 post-switch)                 │
│                                                                 │
│  Ascend*Agent.py                                                │
│       ↓                                                         │
│  AscendAgent (FinanceSimulationAgent)                           │
│       ↓                                                         │
│  ascend_score_tick()  ← profiles: rocket, surge, forge, …     │
│       ↓                                                         │
│  FinanceAgentResponse → validator                               │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  CUSTOM PATH (retired on UID 196)                               │
│                                                                 │
│  AscendKappaAgent / HybridResilientAgent / MicrostructureEdge │
│       ↓                                                         │
│  _sn79_compat.py (protocol adapter)                             │
│       ↓                                                         │
│  Custom respond() loop (reimplemented MM logic)                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Part 5 — Operational reference

### Deploy commands

```bash
./run_deploy_pulse_v2.sh   # UID 65
./run_deploy_apex_v2.sh    # UID 10
./run_deploy_forge_v2.sh   # UID 158
./run_deploy_kappa.sh      # UID 196 (AscendPulseAgent)
./run_deploy_hybrid.sh     # HybridResilientAgent (dormant)
./run_deploy_micro.sh      # MicrostructureEdgeAgent (dormant)
./run_deploy_ascend.sh     # FluxPrimeAgent (dormant)
```

### Key tunables (via `AGENT_PARAMS` in `miner.env`)

| Param | Effect |
|-------|--------|
| `ascend_profile` | Selects `_PROFILE_DEFAULTS` bucket (rocket, surge, …) |
| `max_books_per_tick` | Books actively quoted per tick |
| `min_rt_edge_ticks` | Minimum edge for round-trip completion |
| `cancel_all_on_startup` | Clear stale orders on PM2 restart |
| `min_quantity` / `max_quantity` | Order size (0.32 production standard) |
| `expiry_period` | GTT expiry in nanoseconds (180e9 = 180s) |

### Metrics to watch

| Metric | Healthy signal |
|--------|----------------|
| `kappa_penalty` | `0` |
| `kappa_score` | ~0.50+ (normalized median) |
| `total_realized_pnl` | Positive and rising over days |
| `combined_score` | ~0.39–0.40 (top tier for our fleet) |
| `kappa` (raw) | > 0.01 (small is normal pre-normalization) |

---

## Part 6 — Lessons for future custom agents

1. **Extend `AscendAgent`, don’t rewrite** — override profile or params, not the tick loop.
2. **Never deploy without `FinanceSimulationAgent` trade hooks** — `onTrade` → requote hints are essential.
3. **Validate on a test UID first** — watch κ, penalty, and realized PnL for 24h before switching production UID.
4. **Separate inventory from scoring in your head** — `pnl` / `inventory_value` in metrics are mark-to-market; scoring uses **realized** round-trip PnL only.
5. **Penalty=0 and PnL>0 require different book sets** — if you must do both, use Ascend’s built-in cold-book + rotation rather than a second “presence lane.”

---

*Generated from deployment state and `agents_20260611T0617.json`. UID 196 switched to AscendPulseAgent after T0617 snapshot; expect κ/score to populate on subsequent snapshots.*
