# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
Vault scoring engine — PnL-preserving maker for fast κ₃, Penalty=0.

The prior vault design (2-tick spreads, two-sided on narrow books, 6% skew
tolerance) caused realized PnL to peak then bleed (-142k on UID 209). This
module delegates to ascend_score_tick with vault-tuned defaults that match
top-miner patterns (UID 202/26/251): wide-spread edge, lean inventory,
completion-first round trips, microprice-gated quotes.
"""

from __future__ import annotations

from competitive_utils import ascend_score_tick


def vault_score_tick(
    response,
    state,
    accounts,
    simulation_config,
    direction,
    *,
    last_mid,
    mids_scratch,
    requote_hints=None,
    min_quantity: float,
    max_quantity: float,
    max_fee_rate: float,
    quantity_scale: float,
    expiry_period: int,
    max_books_per_tick: int = 5,
    max_total_instructions: int = 14,
    max_instructions_per_book: int = 3,
    max_requote_per_tick: int = 5,
    book_rotation_groups: int = 24,
    cadence_interval_ns: int = 24_000_000_000,
    rotation_windows: int = 4,
    min_spread_ticks: float = 7.0,
    min_quote_spread_ticks: float | None = 8.5,
    min_rt_edge_ticks: float = 6.5,
    min_quote_rt_edge_ticks: float | None = 7.0,
    min_completion_edge_ticks: float | None = 8.0,
    min_microprice_edge_ticks: float = 2.0,
    max_spread_ratio: float = 0.0009,
    inventory_skew_soft: float = 0.003,
    inventory_skew_hard: float = 0.007,
    inside_depth_ticks: int = 1,
    deep_spread_ticks: float = 11.0,
    inactive_book_frac: float = 0.15,
    max_tape_imbalance: float = 0.20,
    cold_book_volume_threshold: float = 500.0,
    risk_off_skewed_books: int = 2,
    two_sided_wide_ticks: float = 12.0,
    max_flatten_per_tick: int = 8,
) -> None:
    ascend_score_tick(
        response,
        state,
        accounts,
        simulation_config,
        direction,
        last_mid=last_mid,
        mids_scratch=mids_scratch,
        requote_hints=requote_hints,
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        max_fee_rate=max_fee_rate,
        quantity_scale=quantity_scale,
        expiry_period=expiry_period,
        inventory_skew_soft=inventory_skew_soft,
        inventory_skew_hard=inventory_skew_hard,
        max_books_per_tick=max_books_per_tick,
        max_instructions_per_book=max_instructions_per_book,
        max_total_instructions=max_total_instructions,
        max_requote_per_tick=max_requote_per_tick,
        book_rotation_groups=book_rotation_groups,
        cadence_interval_ns=cadence_interval_ns,
        rotation_windows=rotation_windows,
        max_spread_ratio=max_spread_ratio,
        min_spread_ticks=min_spread_ticks,
        min_rt_edge_ticks=min_rt_edge_ticks,
        min_completion_rt_edge_ticks=min_completion_edge_ticks,
        min_microprice_edge_ticks=min_microprice_edge_ticks,
        min_quote_spread_ticks=min_quote_spread_ticks,
        min_quote_rt_edge_ticks=min_quote_rt_edge_ticks,
        max_tape_imbalance=max_tape_imbalance,
        cold_book_volume_threshold=cold_book_volume_threshold,
        inside_depth_ticks=inside_depth_ticks,
        deep_spread_ticks=deep_spread_ticks,
        inactive_book_frac=inactive_book_frac,
        risk_off_skewed_books=risk_off_skewed_books,
        two_sided_wide_ticks=two_sided_wide_ticks,
        max_flatten_per_tick=max_flatten_per_tick,
    )
