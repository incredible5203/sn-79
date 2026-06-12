"""
CPU-light prediction overlay for AscendPredictAgent.

Uses PassiveAggressiveRegressor (online) per book with microstructure features.
Combines prediction direction with inventory skew to size new quotes only
(completions stay at base quantity inside ascend_score_tick).
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

import numpy as np
from sklearn.linear_model import PassiveAggressiveRegressor

from competitive_utils import (
    inventory_skew,
    microprice,
    sim_order_qty,
    tape_imbalance_ratio,
    tick_size,
)

if TYPE_CHECKING:
    from taos.im.protocol.models import Book


class PredictOverlay:
    """Per-book online regressor + skew-aware quantity policy."""

    def __init__(
        self,
        *,
        base_quantity: float,
        max_quantity: float,
        volume_decimals: int,
        inventory_skew_soft: float,
        predict_threshold: float = 0.002,
        agree_size_k: float = 0.5,
        predict_max_books: int = 13,
        time_budget_ms: float = 400.0,
        min_train_samples: int = 8,
    ) -> None:
        self.base_quantity = base_quantity
        self.max_quantity = max_quantity
        self.volume_decimals = volume_decimals
        self.inventory_skew_soft = inventory_skew_soft
        self.predict_threshold = predict_threshold
        self.agree_size_k = agree_size_k
        self.predict_max_books = predict_max_books
        self.time_budget_ms = time_budget_ms
        self.min_train_samples = min_train_samples

        self._models: dict[int, PassiveAggressiveRegressor] = {}
        self._train_count: dict[int, int] = {}
        self._prev: dict[int, tuple[np.ndarray, float]] = {}
        self._last_ms: float = 0.0
        self._skipped: bool = False

    def _model(self, book_id: int) -> PassiveAggressiveRegressor:
        if book_id not in self._models:
            self._models[book_id] = PassiveAggressiveRegressor(
                C=1.0, epsilon=0.002, random_state=42 + book_id
            )
            self._train_count[book_id] = 0
        return self._models[book_id]

    @staticmethod
    def _top_imbalance(book: Book) -> float:
        if not book.bids or not book.asks:
            return 0.0
        bq = float(book.bids[0].quantity)
        aq = float(book.asks[0].quantity)
        denom = bq + aq
        if denom <= 0:
            return 0.0
        return (bq - aq) / denom

    def _features(
        self, book: Book, mid: float, tick: float, last_mid: float | None
    ) -> np.ndarray:
        bid = book.bids[0].price
        ask = book.asks[0].price
        spread = ask - bid
        spread_ratio = spread / mid if mid > 0 else 0.0
        spread_ticks = spread / tick if tick > 0 else 0.0
        mp = microprice(book)
        mp_edge = ((mp - mid) / tick) if (mp is not None and tick > 0) else 0.0
        if last_mid and last_mid > 0:
            log_ret = math.log(mid / last_mid)
        else:
            log_ret = 0.0
        tape = tape_imbalance_ratio(book)
        top_imb = self._top_imbalance(book)
        return np.array(
            [
                spread_ratio,
                mp_edge,
                tape,
                log_ret,
                top_imb,
                spread_ticks * 0.01,
            ],
            dtype=np.float64,
        )

    @staticmethod
    def _direction_sign(value: float, threshold: float) -> int:
        if value > threshold:
            return 1
        if value < -threshold:
            return -1
        return 0

    def _skew_sign(self, skew: float) -> int:
        soft = self.inventory_skew_soft
        if skew > soft * 0.5:
            return -1
        if skew < -soft * 0.5:
            return 1
        return 0

    def _round_qty(self, qty: float) -> float:
        return sim_order_qty(
            self.base_quantity,
            min(qty, self.max_quantity),
            1.0,
            self.volume_decimals,
        )

    def _quantity_policy(
        self, pred_sign: int, skew_sign: int, pred_value: float
    ) -> float:
        base = self.base_quantity
        if pred_sign == 0:
            return self._round_qty(base)
        if skew_sign != 0 and pred_sign != skew_sign:
            return self._round_qty(base)
        if pred_sign == skew_sign and skew_sign != 0:
            conf = min(abs(pred_value) / max(self.predict_threshold, 1e-9), 2.0)
            boosted = base * (1.0 + self.agree_size_k * conf)
            return self._round_qty(boosted)
        return self._round_qty(base)

    def _update_model(self, book_id: int, features: np.ndarray, mid: float) -> None:
        prev = self._prev.get(book_id)
        if prev is not None:
            prev_x, prev_mid = prev
            if prev_mid > 0 and mid > 0:
                y = math.log(mid / prev_mid)
                model = self._model(book_id)
                n = self._train_count[book_id]
                if n < self.min_train_samples:
                    if n == 0:
                        model.partial_fit(prev_x.reshape(1, -1), np.array([y]))
                    else:
                        model.partial_fit(prev_x.reshape(1, -1), np.array([y]))
                    self._train_count[book_id] = n + 1
                else:
                    model.partial_fit(prev_x.reshape(1, -1), np.array([y]))
        self._prev[book_id] = (features, mid)

    def book_quote_qty(
        self,
        state,
        accounts,
        simulation_config,
        last_mid: dict[int, float],
    ) -> dict[int, float]:
        t0 = time.perf_counter()
        pdec = simulation_config.priceDecimals
        tick = tick_size(pdec)
        candidates: list[tuple[float, int, float, int, float]] = []

        for book_id, book in state.books.items():
            if book_id not in accounts:
                continue
            if not book.bids or not book.asks:
                continue
            bid, ask = book.bids[0].price, book.asks[0].price
            if ask <= bid:
                continue
            mid = (bid + ask) * 0.5
            prev_mid = last_mid.get(book_id)
            features = self._features(book, mid, tick, prev_mid)
            self._update_model(book_id, features, mid)

            skew = inventory_skew(accounts, book_id, mid)
            skew_s = self._skew_sign(skew)
            pred_val = 0.0
            pred_s = 0
            n = self._train_count.get(book_id, 0)
            if n >= self.min_train_samples:
                try:
                    pred_val = float(self._models[book_id].predict(features.reshape(1, -1))[0])
                    pred_s = self._direction_sign(pred_val, self.predict_threshold)
                except Exception:
                    pred_s = 0

            spread_ticks = (ask - bid) / tick if tick > 0 else 0.0
            rank = spread_ticks + abs(pred_val) * 100.0
            candidates.append((rank, book_id, pred_val, pred_s, skew_s))

            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if elapsed_ms > self.time_budget_ms:
                self._skipped = True
                break

        candidates.sort(key=lambda x: -x[0])
        selected = candidates[: max(1, self.predict_max_books)]

        out: dict[int, float] = {}
        for _rank, book_id, pred_val, pred_s, skew_s in selected:
            out[book_id] = self._quantity_policy(pred_s, skew_s, pred_val)

        self._last_ms = (time.perf_counter() - t0) * 1000.0
        return out
