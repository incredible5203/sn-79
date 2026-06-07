# SN-79: How to Compare Your Results With Other Miners

> **Subnet:** MVTRX / τaos (Bittensor netuid **79** mainnet, **366** testnet)  
> **Related:** [SN-79-subnet-analysis.md](./SN-79-subnet-analysis.md) · [Dashboard field reference](https://github.com/taos-im/sn-79/blob/main/doc/dashboard/README.md)

---

## Table of Contents

1. [Official dashboards](#1-official-dashboards)
2. [Leaderboard: Agents table](#2-leaderboard-agents-table)
3. [Your agent page vs others](#3-your-agent-page-vs-others)
4. [Per-book comparison](#4-per-book-comparison)
5. [On-chain comparison](#5-on-chain-comparison)
6. [GenTRX training comparison](#6-gentrx-training-comparison)
7. [Before mainnet: local and testnet](#7-before-mainnet-local-and-testnet)
8. [Quick comparison checklist](#8-quick-comparison-checklist)
9. [What “good” looks like](#9-what-good-looks-like-relative-to-others)

---

## 1. Official dashboards

This is the primary way to compare your miner with everyone else.

| Network | Dashboard |
|---------|-----------|
| **Mainnet (79)** | [taos.simulate.trading](https://taos.simulate.trading) |
| **Testnet (366)** | [testnet.simulate.trading](https://testnet.simulate.trading) |

Additional views:

| Resource | URL |
|----------|-----|
| Simulation terminal | [mvtrx.simulate.trading](https://mvtrx.simulate.trading) |
| Dashboard field guide | [doc/dashboard/README.md](https://github.com/taos-im/sn-79/blob/main/doc/dashboard/README.md) |

Validators publish Prometheus metrics; the dashboard aggregates them so you can compare all miners in one place.

---

## 2. Leaderboard: Agents table

On the **Validators** page, open the **Agents** table. This is the subnet leaderboard.

### Key columns

| Column | What it tells you |
|--------|-------------------|
| **Pos** | Your rank vs all miners (by score) |
| **Score** | Final composite score (EMA-smoothed) |
| **Kappa Score** | Risk-adjusted performance (main metric) |
| **Median Kappa** | Raw Kappa-3 across books |
| **Realized PnL** | Profit/loss from closed trades |
| **Activity** | Round-trip volume factor |
| **24H Vol [QUOTE]** | Trading volume (24 sim hours, min across books) |
| **24H RT [QUOTE]** | Round-trip volume (same window) |
| **Penalty** | Outlier penalty (weak books dragging you down) |
| **ΔInv [QUOTE]** | Total inventory change since sim start |

### How to use it

1. Find your **UID** in the **Agent** column.
2. Note your **Pos** and **Score**.
3. Scan rows above you and compare **Kappa Score**, **Realized PnL**, **Activity**, and **Penalty**.

**Direct link (mainnet agents view):**  
[https://taos.simulate.trading/d/edy6vxytuud4wd/agents](https://taos.simulate.trading/d/edy6vxytuud4wd/agents)

---

## 3. Your agent page vs others

Click your **UID** in the Agents table (or any other miner’s UID).

### Agent page panels

| Panel | Use for comparison |
|-------|-------------------|
| **Score plot** | Your validator score vs on-chain **incentive** |
| **Performance plot** | **Rank over time** vs other miners (best “am I improving?” view) |
| **Kappa-3 plots** | Per-book raw Kappa vs median; weighted score + penalty |
| **Realized PnL plot** | Closed-trade profitability in the assessment window |
| **Daily volume plot** | Maker/taker/self volume vs **volume cap** (red dashed line) |
| **Round-trip volume plot** | Activity that feeds Kappa weighting |
| **Requests plot** | Response time, success, timeouts, failures, rejections |
| **Unrealized P&L plots** | Inventory change (secondary to realized metrics for scoring) |
| **Fee rates / balances** | Per-book costs and positions |

### Side-by-side with a competitor

1. Open your Agent page.
2. Open another miner’s Agent page in a second tab (click their UID in the Agents table).
3. Compare: Kappa, realized PnL, volume, penalty, and request success.

### Validator filter

The Agent page can show data for a **specific validator** or **all validators**. Each validator runs a slightly different simulation realization, so ranks can differ by validator — check both if you see inconsistent placement.

---

## 4. Per-book comparison

Use the **Book** page (click a **book ID** from the Trades or Books table on the Validator page):

- **Agents table (per book)** — who leads on that book only
- Trade history, depth, fee rates, MTR

**When this matters:** Your overall **Pos** is acceptable but **Penalty** is high — often one or two books are much worse than your median; fix or reduce exposure on those books.

---

## 5. On-chain comparison

The dashboard shows per-agent **incentive**, **emission**, **trust**, and **consensus** (from the metagraph). These reflect how validators weight you after scoring.

### CLI

```bash
btcli subnet metagraph --netuid 79
```

For testnet:

```bash
btcli subnet metagraph --netuid 366
```

Look at **incentive** and rank for your UID vs others.

**Note:** On-chain weights lag the simulation dashboard slightly (EMA on scores + weight-setting cadence).

---

## 6. GenTRX training comparison

If you run the miner with GenTRX (`./run_miner.sh -G`), trading and training are **separate reward pools** (~95% / ~5% by default).

| Tool | Purpose |
|------|---------|
| Dashboard GenTRX metrics | Per-miner gradient scores (when validators publish them) |
| `bin/gentrx_inspect --watch` | Terminal: round scores and acceptance vs other miners |
| `bin/gentrx_inspect --all` | Broader history from aggregation log |
| [wandb.md](https://github.com/taos-im/sn-79/blob/main/doc/gentrx/wandb.md) | Optional live dashboard (validator/aggregator setup) |

GenTRX scores are **rank-normalized** each round — you are compared directly against other gradient submitters on held-out data.

**Setup docs:** [doc/gentrx/miner_setup.md](https://github.com/taos-im/sn-79/blob/main/doc/gentrx/miner_setup.md)

---

## 7. Before mainnet: local and testnet

| Stage | How to compare |
|-------|----------------|
| **Local** | [agents/proxy/README.md](https://github.com/taos-im/sn-79/blob/main/agents/proxy/README.md) — offline simulator + proxy validator; no leaderboard, but validates strategy behavior |
| **Testnet (366)** | Deploy miner, monitor at [testnet.simulate.trading](https://testnet.simulate.trading) — same Agents / Agent UI as mainnet |

Testnet is the recommended step to compare your hosting, latency, and rank against other miners before mainnet registration.

---

## 8. Quick comparison checklist

For **your UID**, verify:

| # | Check | Where |
|---|--------|--------|
| 1 | Headline rank | **Pos** in Agents table |
| 2 | Rank trend | **Performance** plot on Agent page |
| 3 | Risk-adjusted edge | **Kappa Score** vs **Median Kappa** |
| 4 | Profitability | **Realized PnL** (21% of trading score when enabled) |
| 5 | Consistency | **Penalty** (low vs top miners) |
| 6 | Connectivity | **Requests** — mostly Success, few Timeouts |
| 7 | Activity | **24H RT** vs volume cap line on volume plots |
| 8 | Emissions | **Incentive** on Agent page / metagraph |

---

## 9. What “good” looks like relative to others

Strong miners relative to the field typically show:

- High **median Kappa** with **low Penalty** (consistent across books, not one-book gambling)
- Positive **realized PnL**, not only mark-to-market inventory gains
- Mostly **Success** on Requests (stay under validator timeout, default ~3s)
- Steady **round-trip volume** without hitting the **capital turnover cap** early
- **Performance** rank improving or stable over time, not a single spike
- Trades on **all books** (FAQ: neglecting books hurts score; up to 37.5% inactive books tolerated, excess penalized)

### Common gaps vs top miners

| Symptom | Likely cause | What to check |
|---------|--------------|---------------|
| Low Pos, high Penalty | Weak outlier books | Per-book Kappa on Agent page; Book page agents table |
| Low Pos, low Kappa | Poor risk-adjusted round-trips | Realized PnL plot; trade less or manage downside |
| No score after register | Insufficient history | Wait for min realized observations + lookback window |
| High Pos but low incentive | Weight lag / validator disagreement | Compare across validators; metagraph incentive |
| Many timeouts | Slow agent or network | Requests plot; enable `lazy_load=1`; move closer to validators |
| Volume cap hit | Over-trading | Daily volume plot; `accounts[book_id]['traded_volume']` in agent code |

---

## Troubleshooting links

| Issue | Resource |
|-------|----------|
| Score not increasing | [FAQ §8](https://github.com/taos-im/sn-79/blob/main/FAQ.md) |
| Volume cap | [FAQ §9](https://github.com/taos-im/sn-79/blob/main/FAQ.md) · [agents/README.md — Trading Limitation](https://github.com/taos-im/sn-79/blob/main/agents/README.md) |
| Field definitions | [doc/dashboard/README.md](https://github.com/taos-im/sn-79/blob/main/doc/dashboard/README.md) |
| Scoring math | [taos/im/validator/reward.py](https://github.com/taos-im/sn-79/blob/main/taos/im/validator/reward.py) |
| Discord (τaos) | [Bittensor Discord channel](https://discord.com/channels/799672011265015819/1353733356470276096) |

---

*This guide summarizes official SN-79 monitoring paths. Dashboard URLs and scoring parameters may change; confirm against the latest [README](https://github.com/taos-im/sn-79/blob/main/README.md) and validator config.*
