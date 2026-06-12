"""Signal engine and state for PredictiveMakerAgent (Option A)."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class CompletionHint:
    book_id: int
    side: str
    fill_price: float
    fill_qty: float
    queued_ts_ns: int
    attempts: int = 0


@dataclass
class SignalState:
    ofi_ema: float = 0.0
    micro_ema: float = 0.0
    mid_history: deque = field(default_factory=lambda: deque(maxlen=6))
    last_signal: float = 0.0


@dataclass
class BookHealth:
    consecutive_losses: int = 0
    blacklist_until: int = 0
    total_rt_pnl: float = 0.0
    rt_count: int = 0


class SignalEngine:
    """OFI + microprice + depth + momentum → combined signal in [-1, +1]."""

    def __init__(self, alpha: float = 0.30, mid_history_len: int = 6):
        self.alpha = alpha
        self.mid_history_len = mid_history_len
        self.states: dict[int, SignalState] = defaultdict(SignalState)

    def compute(self, book_id: int, book) -> float:
        st = self.states[book_id]
        try:
            bids = book.bids
            asks = book.asks
            if not bids or not asks:
                return 0.0

            bid_p = float(bids[0].price)
            ask_p = float(asks[0].price)
            bid_q = float(bids[0].quantity)
            ask_q = float(asks[0].quantity)
            spread = ask_p - bid_p
            mid = (bid_p + ask_p) / 2.0
            if spread <= 0 or mid <= 0:
                return 0.0

            micro = (bid_p * ask_q + ask_p * bid_q) / max(bid_q + ask_q, 1e-9)
            micro_dev = (micro - mid) / spread
            st.micro_ema = self.alpha * micro_dev + (1.0 - self.alpha) * st.micro_ema

            buy_vol = sell_vol = 0.0
            for ev in book.events:
                if getattr(ev, "y", None) == "t":
                    q = float(getattr(ev, "quantity", 0.0))
                    s = getattr(ev, "side", -1)
                    if s == 0:
                        buy_vol += q
                    elif s == 1:
                        sell_vol += q
            ofi = (buy_vol - sell_vol) / max(buy_vol + sell_vol, 1e-9)
            st.ofi_ema = self.alpha * ofi + (1.0 - self.alpha) * st.ofi_ema

            bid_depth = sum(float(l.quantity) for l in bids[:5])
            ask_depth = sum(float(l.quantity) for l in asks[:5])
            depth_imb = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)

            st.mid_history.append(mid)
            if len(st.mid_history) >= 3:
                mids = list(st.mid_history)
                returns = [
                    (mids[-1 - i] - mids[-2 - i]) / max(abs(mids[-2 - i]), 1e-9)
                    for i in range(min(3, len(mids) - 1))
                ]
                weights = [0.7**i for i in range(len(returns))]
                momentum = sum(w * r for w, r in zip(weights, returns)) / sum(weights)
                momentum_norm = max(-1.0, min(1.0, momentum / 0.002))
            else:
                momentum_norm = 0.0

            combined = (
                0.35 * st.ofi_ema
                + 0.30 * st.micro_ema
                + 0.20 * depth_imb
                + 0.15 * momentum_norm
            )
            combined = max(-1.0, min(1.0, combined))
            st.last_signal = combined
            return combined
        except Exception:
            return st.last_signal
