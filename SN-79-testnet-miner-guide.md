# SN-79 Testnet Miner — Step-by-Step Guide

> Run a **τaos** miner on Bittensor **testnet** (subnet **netuid 366**) before mainnet (**79**).  
> **Dashboard:** [testnet.simulate.trading](https://testnet.simulate.trading)  
> **Related:** [SN-79-subnet-analysis.md](./SN-79-subnet-analysis.md) · [SN-79-miner-comparison-guide.md](./SN-79-miner-comparison-guide.md)

---

## What you are building

```text
Testnet validators  ──query──►  Your miner (axon :8091)
       │                              │
       │                              ▼
       │                    Trading agent (Python)
       │                              │
       └◄── instructions (orders) ────┘
```

You need: a Linux machine, a Bittensor wallet, testnet TAO, a registered UID on **366**, and the `sn-79` miner process reachable on the network.

**Important:** Example agents in `agents/` are for learning only. They are **not** expected to rank well without your own strategy.

---

## Overview (recommended path)

| Step | What | Time (rough) |
|------|------|----------------|
| 0 | *(Optional)* Test agent locally with proxy | 1–3 hours first time (simulator build) |
| 1 | Install system deps + `sn-79` miner | 15–30 min |
| 2 | Create Bittensor wallet | 5 min |
| 3 | Get testnet TAO | Discord / faucet |
| 4 | Register UID on netuid **366** | 5 min |
| 5 | Open axon port + set external IP | 5–15 min |
| 6 | Start miner on testnet | 5 min |
| 7 | Verify logs + dashboard | ongoing |

**GenTRX** (optional GPU training) is **not** in this guide. Do trading-only first; add `-G` later using [doc/gentrx/miner_setup.md](https://github.com/taos-im/sn-79/blob/main/doc/gentrx/miner_setup.md).

---

## Prerequisites

| Item | Requirement |
|------|-------------|
| **OS** | Linux (Ubuntu 22.04+ recommended) |
| **RAM** | ~2 GB+ for miner process (more if your agent is heavy) |
| **CPU** | 2+ cores recommended for multi-book strategies |
| **Network** | Public **IP** or port-forward; validators must reach your axon |
| **Python** | 3.10.9 (installed by `install_miner.sh` via pyenv) |
| **Git** | Clone `sn-79` repo |

You do **not** need the C++ simulator on the miner host (validators run that). You only need it for optional **Step 0** (local proxy).

---

## Step 0 (optional): Test your agent locally

Skip to **Step 1** if you want to go straight to testnet. This step is strongly recommended by the subnet FAQ.

1. Clone the repo (if not already):

   ```bash
   git clone https://github.com/taos-im/sn-79.git
   cd sn-79
   ```

2. Install the Python package:

   ```bash
   pip install -e .
   ```

3. For full local simulation, install the simulator (**as root**, long build):

   ```bash
   sudo ./install_validator.sh   # includes simulator; 2+ hours on Ubuntu 22.04
   ```

   Or follow [agents/proxy/README.md](https://github.com/taos-im/sn-79/blob/main/agents/proxy/README.md) for proxy-only workflow.

4. Run the proxy test stack from repo root:

   ```bash
   ./agents/proxy/run
   ```

5. Confirm your agent receives state and returns orders without errors.

Agents tested here work on testnet/mainnet with the same `--agent.name` / `--agent.params`.

---

## Step 1: Install the miner environment

From the repository root (`sn-79/`):

```bash
cd /path/to/sn-79
./install_miner.sh
```

This installs (among other things):

- **pyenv** + **Python 3.10.9**
- **pm2** (process manager)
- **tmux**
- **`taos`** package (`pip install -e .`)
- Example agents copied to **`~/.taos/agents`**

After install, **open a new shell** (or reload bash) so `pyenv` and `pm2` are on your `PATH`.

Verify:

```bash
python --version    # should show 3.10.9
pm2 --version
ls ~/.taos/agents   # example agents
```

---

## Step 2: Create a Bittensor wallet

If you already have a coldkey + hotkey, skip to Step 3.

Install [btcli](https://docs.learnbittensor.org/getting-started/install-btcli) if needed, then:

```bash
btcli wallet create --wallet.name my-testnet-coldkey
btcli wallet create --wallet.name my-testnet-coldkey --wallet.hotkey my-hotkey
```

List wallets:

```bash
btcli wallet list
```

**Custom wallet directory** (e.g. `/ttp/_tensor/btcli/wallets/`):

Use `--wallet.path` everywhere:

```bash
export WALLET_PATH=/ttp/_tensor/btcli/wallets
btcli wallet list --wallet.path "$WALLET_PATH"
```

You will pass the same path to `run_miner.sh` as `-p "$WALLET_PATH"`.

---

## Step 3: Get testnet TAO

Registration and subnet participation require **testnet TAO** on your coldkey.

1. Join the [Bittensor Discord](https://discord.com/channels/799672011265015819/1389370202327748629).
2. Request testnet TAO in the testnet/faucet channel (see current Discord instructions).
3. Confirm balance:

   ```bash
   btcli wallet balance \
     --wallet.name my-testnet-coldkey \
     --subtensor.network test
   ```

   Add `--wallet.path "$WALLET_PATH"` if not using the default `~/.bittensor/wallets/`.

---

## Step 4: Register on subnet 366 (τaos testnet)

τaos testnet uses **netuid 366** (mainnet uses **79**).

Check subnet info:

```bash
btcli subnet show --netuid 366 --subtensor.network test
```

Register your hotkey (costs testnet TAO):

```bash
btcli subnet register \
  --netuid 366 \
  --subtensor.network test \
  --wallet.name my-testnet-coldkey \
  --wallet.hotkey my-hotkey
```

If registration is full, use recycle (when available):

```bash
btcli subnet recycle_register \
  --netuid 366 \
  --subtensor.network test \
  --wallet.name my-testnet-coldkey \
  --wallet.hotkey my-hotkey
```

Confirm you appear on the metagraph:

```bash
btcli subnet metagraph --netuid 366 --subtensor.network test
```

Note your **UID** (row for your hotkey). You need it for the dashboard.

---

## Step 5: Networking — axon must be reachable

Validators query your miner at the **IP:port** published on-chain. Default axon port in `run_miner.sh` is **8091**.

### 5.1 Cloud VPS (simplest)

- Use the instance’s **public IP**.
- Open **TCP 8091** in the cloud security group / firewall.

### 5.2 Home / NAT

- Forward external port **8091** → your machine’s LAN IP:8091.
- Use your **public** IP as `external_ip` when serving the axon.

### 5.3 Firewall (Ubuntu example)

```bash
sudo ufw allow 8091/tcp
sudo ufw status
```

### 5.4 Pick a free port (if 8091 is taken)

Use another port (e.g. `8092`) and pass `-a 8092` to `run_miner.sh`. Open that port instead.

---

## Step 6: Choose an agent (first run)

Example agents in `~/.taos/agents/`:

| Agent | Role |
|-------|------|
| `RandomMakerAgent` | Random limit orders (simple smoke test) |
| `RandomTakerAgent` | Random market orders |
| `ImbalanceAgent` | LOB imbalance signal |
| `SimpleRegressorAgent` | ML regressor demo (default in `run_miner.sh`) |

For a **first testnet smoke test**, `RandomMakerAgent` or `ImbalanceAgent` is fine. For competition you must write or heavily customize a strategy — see [agents/README.md](https://github.com/taos-im/sn-79/blob/main/agents/README.md).

Optional: copy and edit an agent:

```bash
cp ~/.taos/agents/ImbalanceAgent.py ~/.taos/agents/MyTestAgent.py
# Edit class name inside file to MyTestAgent
```

---

## Step 7: Start the miner on testnet

From **`sn-79/`** repo root.

### 7.1 Testnet parameters (required)

| Flag | Testnet value | Why |
|------|---------------|-----|
| `-u` | `366` | τaos testnet netuid |
| `-e` | `wss://test.finney.opentensor.ai:443` | Test chain (not finney mainnet) |
| `-w` / `-h` | your coldkey / hotkey | Wallet |
| `-p` | wallet path if non-default | e.g. `/ttp/_tensor/btcli/wallets` |
| `-a` | axon port | default `8091` |

### 7.2 First launch (example)

```bash
cd /path/to/sn-79

./run_miner.sh \
  -e wss://test.finney.opentensor.ai:443 \
  -u 366 \
  -p /ttp/_tensor/btcli/wallets \
  -w my-testnet-coldkey \
  -h my-hotkey \
  -a 8091 \
  -n RandomMakerAgent \
  -m "min_quantity=0.1 max_quantity=1.0" \
  -l info
```

The script will:

1. `git pull` and `pip install -e .`
2. Start **`miner.py`** under **pm2** as process name `miner`
3. Stream logs (`pm2 logs miner`)

### 7.3 Serve axon with public IP (if behind NAT)

If validators cannot reach you, set external IP when starting manually (alternative to `run_miner.sh`):

```bash
cd sn-79/taos/im/neurons

python miner.py \
  --netuid 366 \
  --subtensor.chain_endpoint wss://test.finney.opentensor.ai:443 \
  --wallet.path /ttp/_tensor/btcli/wallets \
  --wallet.name my-testnet-coldkey \
  --wallet.hotkey my-hotkey \
  --axon.port 8091 \
  --axon.external_ip YOUR_PUBLIC_IP \
  --axon.external_port 8091 \
  --logging.info \
  --agent.path ~/.taos/agents \
  --agent.name RandomMakerAgent \
  --agent.params min_quantity=0.1 max_quantity=1.0
```

On a VPS with a public interface, external IP is often detected automatically; if queries time out, set `external_ip` explicitly.

### 7.4 Faster parsing (recommended)

Add to agent params:

```text
lazy_load=1
```

Example:

```bash
-m "lazy_load=1 min_quantity=0.1 max_quantity=1.0"
```

---

## Step 8: Verify the miner is working

### 8.1 Process and logs

```bash
pm2 status
pm2 logs miner --lines 100
```

Look for:

- `Serving miner axon at ... netuid: 366`
- `Decompressed (...)` / responses being built
- No repeated tracebacks

### 8.2 Metagraph axon

```bash
btcli subnet metagraph --netuid 366 --subtensor.network test
```

Your UID should show a non-zero **IP** and **port** (not `0.0.0.0` / `0`).

### 8.3 Testnet dashboard

1. Open [testnet.simulate.trading](https://testnet.simulate.trading).
2. Select the testnet validator (if prompted).
3. Open the **Agents** table.
4. Click your **UID**.

Check:

| Signal | Good sign |
|--------|-----------|
| **Requests** plot | Mostly **Success**, few **Timeouts** |
| **Trades** table | Recent trades appearing |
| **Score / Kappa** | May stay flat until enough history (normal right after register) |

Full field meanings: [doc/dashboard/README.md](https://github.com/taos-im/sn-79/blob/main/doc/dashboard/README.md).

---

## Step 9: Operate day-to-day

### Restart after code update

```bash
cd /path/to/sn-79
./run_miner.sh \
  -e wss://test.finney.opentensor.ai:443 \
  -u 366 \
  -p /ttp/_tensor/btcli/wallets \
  -w my-testnet-coldkey \
  -h my-hotkey \
  -a 8091 \
  -n RandomMakerAgent
```

`run_miner.sh` pulls latest code each run.

### Stop miner

```bash
pm2 stop miner
pm2 delete miner
```

### Change agent

Stop miner, then relaunch with different `-n` / `-m`.

### Compare with other miners

See [SN-79-miner-comparison-guide.md](./SN-79-miner-comparison-guide.md) (use the **testnet** dashboard URL).

---

## Troubleshooting

### Validators never query me / all timeouts

| Check | Action |
|-------|--------|
| Axon on metagraph | IP/port correct, not `0.0.0.0` |
| Firewall | Port open on host + cloud |
| `external_ip` | Set to public IP if behind NAT |
| Registered | UID exists on netuid **366** testnet |
| Blacklist | Default: only **validators** can query; do not enable `allow_non_validators` unless you know the risk |

### `pm2` / `python` not found after install

Open a new terminal or run:

```bash
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
```

### Responses too slow (>3s)

- Use `lazy_load=1`
- Simplify agent logic or parallelize per-book processing
- More CPU / lower network latency to validators

### Score stays zero

Normal shortly after registration. You need:

- Enough **realized round-trip** trades (Kappa lookback)
- Activity on **books** (avoid ignoring most books)
- See [FAQ §8](https://github.com/taos-im/sn-79/blob/main/FAQ.md)

### Hit volume cap

You traded more than `capital_turnover_cap × miner_wealth` on a book in the assessment window. Wait for volume to roll off or trade less aggressively. See [FAQ §9](https://github.com/taos-im/sn-79/blob/main/FAQ.md).

### Wrong network

| Mistake | Fix |
|---------|-----|
| `-u 79` on test chain | Use `-u 366` |
| Finney endpoint + testnet register | Use `-e wss://test.finney.opentensor.ai:443` |
| Testnet register + mainnet miner | Match endpoint and netuid |

---

## Step 10: When ready for mainnet

1. Confirm stable **Success** rate and sensible trades on [testnet.simulate.trading](https://testnet.simulate.trading).
2. Register on **netuid 79** with **finney** endpoint and real TAO.
3. Run:

   ```bash
   ./run_miner.sh \
     -e wss://entrypoint-finney.opentensor.ai:443 \
     -u 79 \
     -w YOUR_MAINNET_COLDKEY \
     -h YOUR_MAINNET_HOTKEY \
     -a 8091 \
     -n YourAgentName
   ```

4. Monitor [taos.simulate.trading](https://taos.simulate.trading).

---

## Quick reference commands

```bash
# Balance (testnet)
btcli wallet balance --wallet.name COLDKEY --subtensor.network test

# Register τaos testnet
btcli subnet register --netuid 366 --subtensor.network test \
  --wallet.name COLDKEY --wallet.hotkey HOTKEY

# Metagraph
btcli subnet metagraph --netuid 366 --subtensor.network test

# Run miner (testnet)
cd sn-79 && ./run_miner.sh -e wss://test.finney.opentensor.ai:443 -u 366 \
  -w COLDKEY -h HOTKEY -a 8091 -n RandomMakerAgent -l info

# Logs
pm2 logs miner
```

---

## Helpful links

| Resource | URL |
|----------|-----|
| Testnet dashboard | https://testnet.simulate.trading |
| Mainnet dashboard | https://taos.simulate.trading |
| Repo README | https://github.com/taos-im/sn-79 |
| Agent development | https://github.com/taos-im/sn-79/blob/main/agents/README.md |
| FAQ | https://github.com/taos-im/sn-79/blob/main/FAQ.md |
| Testnet TAO Discord | https://discord.com/channels/799672011265015819/1389370202327748629 |
| τaos Discord | https://discord.com/channels/799672011265015819/1353733356470276096 |
| Compare miners | [SN-79-miner-comparison-guide.md](./SN-79-miner-comparison-guide.md) |

---

## Extras: 10 testnet miners (UIDs 215–224)

| UID | PM2 | Port | Agent | Stack |
|-----|-----|------|-------|--------|
| 215 | miner215 | 8100 | EliteMarketMakerAgent | Power taker `hold`, scale 1.12 |
| **216** | miner216 | 8101 | SmartFlowTakerAgent | Power taker `hold`, scale 1.14 |
| 217 | miner217 | 8102 | SpreadReversionProAgent | **`specter`** — fade every move, size ∝ \|return\| |
| **218** | miner218 | 8103 | CrossBookRelativeAgent | Volume taker + cross-book bias |
| 219 | miner219 | 8104 | DepthPressureAgent | Power taker `imbalance` |
| 220 | miner220 | 8105 | RoundTripFocusAgent | Power taker `pingpong` |
| 221 | miner221 | 8106 | MicropriceSkewAgent | Power taker `hold`, scale 1.10 |
| 222 | miner222 | 8107 | HybridSignalAgent | **`riptide`** — even/odd book opposing flow + 22s flip |
| 223 | miner223 | 8108 | TightSpreadTakerAgent | **`warpmesh`** — rotating buy/sell/pingpong book groups (25s) |
| 224 | miner224 | 8109 | VolatilityBurstAgent | **`storm`** — all-book buy/sell waves + flip bursts (20s) |

- **216** uses `_power_agent_base.py` / `fast_power_taker_tick` (low latency, fixed STP).
- **All others** use `_scoring_agent_base.py` / `volume_taker_tick` (UID 210 pattern: market on every book, no pre-flatten).
- Restart one UID at a time: `./run_miner215.sh` … `./run_miner224.sh` (35–60s apart on one host).
- **Firewall:** allow axon ports **8100–8109** (UIDs 223–224 use **8108–8109**; UFW had only 8100–8107 open, which blocks validators).

---

*Subnet parameters and endpoints can change. If something fails, check the latest [sn-79 README](https://github.com/taos-im/sn-79/blob/main/README.md) and Discord announcements.*
