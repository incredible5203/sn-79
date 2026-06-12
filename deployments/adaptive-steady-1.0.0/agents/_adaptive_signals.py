"""Microprice-only signal engine for AdaptiveSteadyMaker (Option B)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class MicroState:
    micro_ema: float = 0.0
    last_signal: float = 0.0


class MicropriceSignalEngine:
    def __init__(self, alpha: float = 0.30):
        self.alpha = alpha
        self.states: dict[int, MicroState] = defaultdict(MicroState)

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
            st.last_signal = st.micro_ema
            return st.micro_ema
        except Exception:
            return st.last_signal
