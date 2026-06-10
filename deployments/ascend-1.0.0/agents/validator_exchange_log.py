# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""JSONL logger for validator request / agent response exchange."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from taos.im.protocol.models import LazyAccounts, LazyBooks


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _serialize_books(books: Any) -> dict[str, Any] | None:
    if books is None:
        return None
    if isinstance(books, LazyBooks):
        return {
            str(book_id): book.parse().model_dump(mode="json")
            for book_id, book in books.items()
        }
    return {
        str(book_id): (
            book.model_dump(mode="json")
            if hasattr(book, "model_dump")
            else _json_safe(book)
        )
        for book_id, book in books.items()
    }


def _serialize_agent_accounts(accounts: Any, agent_id: int) -> dict[str, Any] | None:
    if accounts is None:
        return None
    uid_accounts = accounts.get(agent_id)
    if uid_accounts is None:
        uid_accounts = accounts.get(str(agent_id))
    if uid_accounts is None:
        return None
    if isinstance(accounts, LazyAccounts):
        return {
            str(book_id): account.parse().model_dump(mode="json")
            for book_id, account in uid_accounts.items()
        }
    return {
        str(book_id): (
            account.model_dump(mode="json")
            if hasattr(account, "model_dump")
            else _json_safe(account)
        )
        for book_id, account in uid_accounts.items()
    }


def _serialize_agent_notices(notices: Any, agent_id: int) -> list[Any] | None:
    if notices is None:
        return None
    agent_notices = notices.get(agent_id)
    if agent_notices is None:
        agent_notices = notices.get(str(agent_id))
    if agent_notices is None:
        return []
    return [_json_safe(notice) for notice in agent_notices]


def serialize_validator_request(
    state: MarketSimulationStateUpdate,
    agent_id: int,
) -> dict[str, Any]:
    """Build a JSON-serializable snapshot of the validator state update."""
    dendrite = getattr(state, "dendrite", None)
    validator_hotkey = getattr(dendrite, "hotkey", None) if dendrite else None
    config = state.config
    if config is not None and hasattr(config, "model_dump"):
        config_payload = config.model_dump(mode="json")
    else:
        config_payload = _json_safe(config)

    return {
        "timestamp": state.timestamp,
        "version": getattr(state, "version", None),
        "model": getattr(state, "model", None),
        "validator_hotkey": validator_hotkey,
        "config": config_payload,
        "books": _serialize_books(state.books),
        "accounts": _serialize_agent_accounts(state.accounts, agent_id),
        "notices": _serialize_agent_notices(state.notices, agent_id),
    }


def serialize_agent_response(response: FinanceAgentResponse) -> dict[str, Any]:
    return response.model_dump(mode="json")


class ValidatorExchangeLogger:
    """Append-only JSONL logger for validator request / agent response pairs."""

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    def log_exchange(
        self,
        *,
        agent_id: int,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
    ) -> None:
        record = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "logged_at_unix": time.time(),
            "agent_id": agent_id,
            "simulation_timestamp": state.timestamp,
            "validator_hotkey": getattr(
                getattr(state, "dendrite", None), "hotkey", None
            ),
            "request": serialize_validator_request(state, agent_id),
            "response": serialize_agent_response(response),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
