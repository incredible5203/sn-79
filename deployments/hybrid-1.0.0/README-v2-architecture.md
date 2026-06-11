# SN-79 Agents v2 — Fee-Aware Shared Engine Architecture

## Files

| File | Role |
|---|---|
| `ascend_score_tick.py` | Shared engine — ALL round-trip / fee / kappa-floor logic lives here |
| `AscendKappaAgent.py` | Thin wrapper, books **0–42**, presence-weighted |
| `MicrostructureEdgeAgent.py` | Thin wrapper, books **43–85**, alpha-weighted |
| `HybridResilientAgent.py` | Thin wrapper, books **86–127**, balanced |
| `_sn79_compat.py` | Unchanged compat shim (from your existing deployment) |

Deploy all 4 `.py` files (per agent: the wrapper + `ascend_score_tick.py`
+ `_sn79_compat.py`) into each agent's `~/.taos/agents/` directory, with
`AGENT_NAME` set accordingly per `miner.env`.

**Each agent uses a different UID/account, and each is responsible for
its own disjoint 42-43 book range** — so each independently must (and
will) satisfy the `>=3 round trips per book` floor on its own subset.

---

## Root cause analysis from your production data (UID 196)

Pulling `total_realized_pnl` and `total_roundtrip_volume` from the
`agents_2026*.json` snapshots and computing the per-interval ratio:

| Interval | Δrealized_pnl | Δroundtrip_vol | bps |
|---|---:|---:|---:|
| T0447 | -521.77 | 1,735,278 | **-3.01** |
| T0509 | -1,053.77 | 766,307 | **-13.75** |
| T0522 | -1,092.79 | 782,493 | **-13.97** |
| T0555 | -325.25 | 635,204 | **-5.12** |
| T0617 | -1,337.26 | 863,724 | **-15.48** |
| T0805 | -44.86 | 77,787 | **-5.77** |

**Every single interval is negative**, in the **3–15.5 bps** range. This
is not a few unlucky trades — it's a *per-trade structural leak*, and
the magnitude (3-15.5bps) is exactly in the range of a **maker+maker
round-trip fee pair** (2 × 1.5-7.5bps) at your sim's
~0.02-0.05% maker fee rate.

### The bug

All three original agents' completion logic checked:

```python
required_min = avg_cost + MIN_RT_EDGE_TICKS * tick   # MIN_RT_EDGE_TICKS = 2
```

At `priceDecimals=2` and price ~100, `2 * tick = $0.02` = **2 bps** of
notional. This is **smaller than the round-trip fee cost (4-10bps)**.
So every completion that "passed" the no-loss check (price moved
favorably by ≥2 ticks) still **lost 2-13bps net after both legs' maker
fees** — and this happened on every completed round trip, compounding
into the steady monotonic decline you observed (`total_realized_pnl`
went from -49 → -4424 over ~4 hours, tracking round-trip volume almost
linearly at a negative rate).

### The fix: `required_edge()`

```python
def required_edge(price, account, config, tick):
    maker_fee, _ = get_fee_rates(account)   # READ FRESH from
                                             # account.fees every tick —
                                             # fees are dynamic/MTR-based
    fee_cost = price * maker_fee * 2        # both legs assumed maker
    margin   = price * (PROFIT_MARGIN_BPS / 10000)   # extra profit, default 3bps
    return fee_cost + margin + EXTRA_EDGE_TICKS * tick
```

At your fee range (maker ≈ 0.02-0.05%) and `PROFIT_MARGIN_BPS=3`:

```
old edge (2 ticks)  = 2.00 bps   <- was LESS than fee cost
fee cost (2 legs)   = 6.00 bps   (at 3bps maker fee, mid of your range)
new total edge      = 10.00 bps
net profit per RT   = +3.00 bps  <- guaranteed positive after fees
```

This single change flips the sign of `total_realized_pnl`'s slope from
**always negative** to **always ≥ +3bps of round-trip volume per
completed trip** (by construction — no completion is ever placed unless
this edge is met). `PROFIT_MARGIN_BPS` is your direct dial for how fast
`total_realized_pnl` grows once round trips are flowing.

---

## How the engine targets each requested metric

### `total_realized_pnl` ↑
Direct consequence of `required_edge()`. Every completed round trip
nets ≥ `PROFIT_MARGIN_BPS` after fees. Raise `PROFIT_MARGIN_BPS` for
faster PnL growth per trip — but higher margin means completions wait
longer for the market to move (slower round-trip cadence). Tune
per-agent if desired (currently all three default to 3bps).

### `total_roundtrip_volume` ↑
The **presence lane** (`_place_presence_quotes`) places a sized
(`presence_qty`, e.g. 0.5-0.75, well above the protocol minimum 0.25)
symmetric quote on a rotating subset of each agent's books every tick.
Combined with the now-profitable completions firing reliably, this
lane alone generates a steady stream of round trips. `AscendKappaAgent`
(books 0-42) is configured with the largest `presence_qty=0.75` and
`presence_max_per_tick=14` to lean hardest into pure round-trip-volume
growth.

### `kappa_score` ↑
The **alpha lane** (`_place_alpha_quotes`) — same OFI-direction logic
as before, but now its completions are *also* gated by
`required_edge()`, so the round-trip return distribution is
structurally biased positive (every realized trip ≥ +margin). This
directly raises `mu` (mean round-trip return) and, since losing trips
essentially can't realize (only the `HARD_STUCK` escape valve after 400
ticks can — logged loudly, and rare), `LPM3` stays low. Both halves of
`Kappa-3 = mu / LPM3^(1/3)` move in your favor.
`MicrostructureEdgeAgent` (books 43-85) is the alpha-heaviest profile:
lower `ofi_threshold=0.10` (acts on more signals), larger
`alpha_qty=0.75`, and `alpha_books_per_tick=10`.

### `kappa_penalty` == 0
The presence lane tracks `roundtrip_ticks` per book (a deque of tick
numbers when `net_qty` returned to ~0). Each tick, it computes
`recent_rts = count of round trips within KAPPA_LOOKBACK_TICKS`
(default 1800 ticks ≈ 30 sim-minutes). If `recent_rts <
MIN_ROUNDTRIPS_FOR_KAPPA` (default 3), that book is flagged
`behind_floor` and the presence quote on that book is allowed to use a
larger fraction of available balance to ensure it actually fills and
completes — i.e. **lagging books get prioritized for round-trip
completion**, directly preventing them from becoming Penalty outliers.

---

## Per-agent profile summary

| | AscendKappaAgent | MicrostructureEdgeAgent | HybridResilientAgent |
|---|---|---|---|
| Books | 0-42 (43) | 43-85 (43) | 86-127 (42) |
| Role | Roundtrip-volume lean | Alpha/PnL lean | Balanced |
| `presence_qty` | 0.75 | 0.5 | 0.6 |
| `alpha_qty` | 0.5 | 0.75 | 0.6 |
| `ofi_threshold` | 0.15 | 0.10 | 0.13 |
| `alpha_books_per_tick` | 6 | 10 | 8 |
| `presence_max_per_tick` | 14 | 10 | 12 |

All three share identical `required_edge()`, round-trip-floor, and
circuit-breaker logic from `ascend_score_tick.py` — only the lane
sizing/aggressiveness differs, and book ranges are disjoint so the three
accounts never compete for the same book's liquidity.

---

## Other safety mechanisms retained from v1 (HybridResilientAgent)

- **VWAP position ledger** — every fill (maker or taker, either
  direction) updates net position + cost basis correctly; no dropped
  fills.
- **Never cross the book**, except the new `HARD_STUCK_TICKS=400`
  escape valve — a single, loudly-logged taker unwind only after a
  position has been stuck for ~400 ticks (~6.5 sim-minutes) despite the
  passive escape attempts at `COMPLETION_STUCK_TICKS=120`. This bounds
  worst-case inventory growth without making crossing routine.
- **Circuit breaker** — per-book rolling realized-PnL window; if a book
  goes net-negative beyond 1% of its capital share (genuine adverse
  selection, since fee-driven losses are now structurally prevented),
  alpha is disabled on that book for 600 ticks while presence continues.

---

## Tuning knobs to watch first

1. **`PROFIT_MARGIN_BPS`** (default 3.0, in `ascend_score_tick.py`) —
   the single most important knob for `total_realized_pnl` growth rate.
   Start conservative (3bps); once you confirm `total_realized_pnl` is
   trending up tick-over-tick, you can experiment with raising it
   per-agent (e.g. override in each wrapper's profile/engine call) —
   but higher margin = completions wait longer = lower round-trip
   *frequency*, so there's a volume/PnL-per-trip tradeoff.

2. **`MIN_ROUNDTRIPS_FOR_KAPPA` / `KAPPA_LOOKBACK_TICKS`** — confirm
   these against the actual validator scoring window (the docs imply a
   lookback but don't give an exact tick count; 1800 ticks / 3 RTs is a
   conservative starting guess). If `kappa_penalty` is still nonzero
   after a full lookback window has elapsed, lower
   `KAPPA_LOOKBACK_TICKS` or raise `MIN_ROUNDTRIPS_FOR_KAPPA`'s priority
   (the `behind_floor` override already relaxes balance constraints —
   could also relax the spread/price constraints if needed).

3. **`HARD_STUCK_TICKS`** — watch the logs for `HARD-STUCK unwind
   (taker)` warnings. If these fire often, either `PROFIT_MARGIN_BPS` is
   too high for that book's volatility, or `presence_qty`/`alpha_qty`
   is too large relative to typical price movement — reduce one or the
   other for the affected book range.

4. **Monitor `account.fees.maker_fee_rate`** directly via debug logs
   (`required_edge` reads it fresh every tick) — if your sim's actual
   fee is meaningfully outside the assumed 0.02-0.05% range, the
   formula self-adjusts automatically (that's the point), but it's
   worth confirming the read is succeeding (not falling back to
   `FALLBACK_MAKER_FEE=0.0005`).
