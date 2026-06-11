# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Protocol compatibility helpers for tensor agent package (SN-79 miner)."""

from __future__ import annotations

import bittensor as bt

from taos.im.protocol.instructions import OrderDirection, TimeInForce
from taos.im.protocol.response import FinanceAgentResponse


def parse_direction(direction) -> OrderDirection:
    if isinstance(direction, OrderDirection):
        return direction
    d = str(direction).upper()
    if d in ("BUY", "0"):
        return OrderDirection.BUY
    return OrderDirection.SELL


def parse_time_in_force(tif) -> TimeInForce:
    if isinstance(tif, TimeInForce):
        return tif
    mapping = {
        "GTC": TimeInForce.GTC,
        "GTT": TimeInForce.GTT,
        "IOC": TimeInForce.IOC,
        "FOK": TimeInForce.FOK,
    }
    return mapping.get(str(tif).upper(), TimeInForce.GTC)


def is_trade_notice(notice) -> bool:
    t = getattr(notice, "type", None)
    if t in ("ET", "EVENT_TRADE"):
        return True
    return type(notice).__name__ == "TradeEvent"


class CompatFinanceAgentResponse:
    """Snake_case / string-friendly wrapper around FinanceAgentResponse."""

    def __init__(self, agent_id: int, accounts: dict | None = None):
        self._inner = FinanceAgentResponse(agent_id=agent_id)
        self._accounts = accounts or {}

    @property
    def agent_id(self) -> int:
        return self._inner.agent_id

    def set_accounts(self, accounts: dict) -> None:
        self._accounts = accounts or {}

    def limit_order(
        self,
        book_id,
        direction,
        quantity,
        price,
        *,
        post_only=False,
        postOnly=None,
        time_in_force="GTC",
        timeInForce=None,
        delay=0,
        leverage=0.0,
        **kwargs,
    ) -> None:
        po = postOnly if postOnly is not None else post_only
        tif = timeInForce if timeInForce is not None else parse_time_in_force(time_in_force)
        expiry = kwargs.pop("expiryPeriod", kwargs.pop("expiry_period", None))
        extra = {}
        if expiry is not None:
            extra["expiryPeriod"] = expiry
            if tif == TimeInForce.GTC:
                tif = TimeInForce.GTT
        self._inner.limit_order(
            book_id,
            parse_direction(direction),
            quantity,
            price,
            delay=delay,
            postOnly=bool(po),
            timeInForce=tif,
            leverage=leverage,
            **extra,
        )

    def market_order(self, book_id, direction, quantity, delay=0, **kwargs) -> None:
        self._inner.market_order(
            book_id,
            parse_direction(direction),
            quantity,
            delay=delay,
            **{k: v for k, v in kwargs.items() if k in ("clientOrderId", "leverage", "settlement_option")},
        )

    def cancel_orders(self, book_id, order_ids, delay=0, **kwargs) -> None:
        self._inner.cancel_orders(book_id, list(order_ids), delay=delay)

    def close_positions(
        self,
        book_id,
        settlement_option="FIFO",
        order_ids=None,
        delay=0,
        **kwargs,
    ) -> None:
        if order_ids:
            self._inner.close_positions(book_id, list(order_ids), delay=delay)
            return
        account = self._accounts.get(book_id)
        if not account:
            return
        loans = getattr(account, "loans", None)
        if not loans:
            return
        oid = min(loans.keys())
        self._inner.close_position(book_id, oid, delay=delay)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def unwrap_response(response):
    """Return the protocol FinanceAgentResponse expected by the validator synapse."""
    inner = getattr(response, "_inner", None)
    return inner if inner is not None else response


def log_agent_tick(uid: int, events, response) -> None:
    """Emit bt.logging output like FinanceSimulationAgent report/update."""
    if bt.logging.current_state_value != "Info":
        return

    trade_lines: list[str] = []
    for notice in events or []:
        if not is_trade_notice(notice):
            continue
        maker_id = getattr(notice, "makerAgentId", None)
        taker_id = getattr(notice, "takerAgentId", None)
        if uid not in (maker_id, taker_id):
            continue
        role = "PASSIVE" if uid == maker_id else "AGGRESSIVE"
        side = "BUY" if getattr(notice, "side", 1) == 0 else "SELL"
        book_id = getattr(notice, "bookId", "?")
        price = getattr(notice, "price", 0.0)
        qty = getattr(notice, "quantity", 0.0)
        trade_lines.append(
            f"BOOK {book_id} : {side} TRADE : YOUR {role} FILL {qty}@{price}"
        )

    instructions = getattr(response, "instructions", None) or []
    if not trade_lines and not instructions:
        return

    text = "-" * 50 + "\n"
    if trade_lines:
        text += "EVENTS\n" + "-" * 50 + "\n"
        text += "\n".join(trade_lines) + "\n"
    text += "INSTRUCTIONS\n" + "-" * 50 + "\n"
    if instructions:
        text += "\n".join(str(i) for i in instructions) + "\n"
    else:
        text += "NO INSTRUCTIONS TO SUBMIT\n"
    text += "-" * 50
    bt.logging.info(".\n" + text)
