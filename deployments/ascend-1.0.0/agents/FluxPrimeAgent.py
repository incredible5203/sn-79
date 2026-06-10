# FluxPrimeAgent — UID 209 order-flow maker (ascend engine, flux profile)
from __future__ import annotations

import os
import sys

_agent_dir = os.path.dirname(os.path.abspath(__file__))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from _ascend_agent_base import AscendAgent
from competitive_utils import param_bool
from taos.common.agents import launch
from taos.im.protocol import FinanceAgentResponse, MarketSimulationStateUpdate
from validator_exchange_log import ValidatorExchangeLogger


class FluxPrimeAgent(AscendAgent):
    """UID 209 — immunity test: kappa blitz (completion + cold-book sweep)."""

    agent_label = "FluxPrimeAgent"
    default_ascend_profile = "blitz"

    def initialize(self) -> None:
        super().initialize()
        self.exchange_log_enabled = param_bool(
            getattr(self.config, "exchange_log", True),
            True,
        )
        log_path = getattr(self.config, "exchange_log_path", None)
        if not log_path:
            log_path = os.path.join(self.output_dir, "validator_exchange.jsonl")
        self._exchange_logger = (
            ValidatorExchangeLogger(str(log_path))
            if self.exchange_log_enabled
            else None
        )

    def report(
        self,
        state: MarketSimulationStateUpdate,
        response: FinanceAgentResponse,
    ) -> None:
        if self._exchange_logger is not None:
            self._exchange_logger.log_exchange(
                agent_id=self.uid,
                state=state,
                response=response,
            )
        super().report(state, response)


if __name__ == "__main__":
    launch(FluxPrimeAgent)
