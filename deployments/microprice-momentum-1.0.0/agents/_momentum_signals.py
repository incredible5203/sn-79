"""Momentum + microprice signal engine for MicropriceMomentumMaker (Option C)."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class MomentumState:
    mid_history: deque = field(default_factory=lambda: deque(maxlen=6))
    last_D_raw: float = 0.0
    last_quoted_tick: int = -1


class MomentumSignalEngine:
    def __init__(
        self,
        momentum_weight: float = 0.6,
        microprice_weight: float = 0.4,
        mid_history_len: int = 6,
        momentum_norm_scale: float = 0.002,
    ):
        self.momentum_weight = momentum_weight
        self.microprice_weight = microprice_weight
        self.mid_history_len = mid_history_len
        self.momentum_norm_scale = momentum_norm_scale
        self.states: dict[int, MomentumState] = defaultdict(MomentumState)

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

            st.mid_history.append(mid)
            mids = list(st.mid_history)
            if len(mids) >= 4:
                returns = [
                    (mids[-1 - i] - mids[-2 - i]) / max(abs(mids[-2 - i]), 1e-9)
                    for i in range(min(3, len(mids) - 1))
                ]
                weights = [0.7**i for i in range(len(returns))]
                ewma_return = sum(w * r for w, r in zip(weights, returns)) / sum(weights)
                ewma_return_norm = max(
                    -1.0, min(1.0, ewma_return / self.momentum_norm_scale)
                )
            else:
                ewma_return_norm = 0.0

            d_raw = (
                self.momentum_weight * ewma_return_norm
                + self.microprice_weight * micro_dev
            )
            st.last_D_raw = d_raw
            return d_raw
        except Exception:
            return st.last_D_raw
