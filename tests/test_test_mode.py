"""apply_test_mode: zeroes thresholds on demo, refuses live."""

from __future__ import annotations

import pytest

from kalshi_agent.config import (
    Config,
    LLMAssessorStrategyConfig,
    apply_test_mode,
)


def _with_assessor(config: Config) -> Config:
    config.strategy.active = "llm_assessor"
    config.strategy.llm_assessor = LLMAssessorStrategyConfig(
        categories=["economics"],
        min_confidence=0.6,
        min_edge=0.04,
        min_seconds_between_signals_per_ticker=1800,
    )
    return config


def test_zeroes_all_gates(config: Config) -> None:
    _with_assessor(config)
    config.risk.min_edge_after_fees_bps = 200

    apply_test_mode(config)

    assert config.risk.min_edge_after_fees_bps == 0
    assert config.strategy.llm_assessor.min_confidence == 0.0
    assert config.strategy.llm_assessor.min_edge == 0.0
    assert config.strategy.llm_assessor.min_seconds_between_signals_per_ticker == 0


def test_refuses_live_mode(config: Config) -> None:
    _with_assessor(config)
    config.mode = "live"

    with pytest.raises(ValueError, match="live mode"):
        apply_test_mode(config)


def test_handles_missing_assessor_block(config: Config) -> None:
    # Placeholder strategy: no llm_assessor block, but risk gate still zeroed.
    config.risk.min_edge_after_fees_bps = 200
    apply_test_mode(config)
    assert config.risk.min_edge_after_fees_bps == 0
