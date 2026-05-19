"""Configuration: YAML for non-secret values, env vars for secrets."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class KalshiConfig(BaseModel):
    base_url_paper: str
    base_url_live: str
    ws_url_paper: str
    ws_url_live: str
    rate_limit_writes_per_second: float = 8.0
    rate_limit_reads_per_second: float = 18.0


class RiskConfig(BaseModel):
    max_position_per_market_usd: float
    max_total_exposure_usd: float
    max_daily_loss_usd: float
    per_order_max_contracts: int
    max_orders_per_minute: int
    min_edge_after_fees_bps: int
    kelly_fraction: float
    max_kelly_size_pct_bankroll: float
    error_spike_threshold: int = 5
    error_spike_window_seconds: int = 60

    @field_validator("kelly_fraction", "max_kelly_size_pct_bankroll")
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("must be in (0, 1]")
        return v


class FeesConfig(BaseModel):
    taker_rate: float = 0.07
    maker_taker_ratio: float = 0.25
    assume_taker: bool = True


class MarketsConfig(BaseModel):
    whitelist_categories: list[str] = Field(default_factory=list)
    whitelist_tickers: list[str] = Field(default_factory=list)


class DiscordConfig(BaseModel):
    notify_on: list[str] = Field(default_factory=list)


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class KillSwitchConfig(BaseModel):
    halt_file_path: Path


class StorageConfig(BaseModel):
    db_path: Path


class JournalConfig(BaseModel):
    log_dir: Path
    retention_days: int = 90


class PlaceholderStrategyConfig(BaseModel):
    test_ticker: str
    emit_interval_seconds: int = 3600


class LLMAssessorStrategyConfig(BaseModel):
    # If non-empty, manual override: only these tickers are assessed.
    # If empty, the strategy auto-discovers from open markets.
    tickers: list[str] = Field(default_factory=list)
    # Auto-discovery filters (used when `tickers` is empty)
    categories: list[str] = Field(default_factory=list)
    max_markets_per_tick: int = 20
    min_volume_contracts: int = 100
    min_hours_to_close: float = 2.0
    discovery_max_pages: int = 5  # pages of 200 to scan per discovery call
    # Common filters
    min_edge: float = 0.04
    min_confidence: float = 0.6
    signal_ttl_minutes: int = 10
    min_seconds_between_signals_per_ticker: int = 1800


class StrategyConfig(BaseModel):
    active: Literal["placeholder", "llm_assessor"] = "placeholder"
    placeholder: PlaceholderStrategyConfig
    llm_assessor: LLMAssessorStrategyConfig | None = None


class LLMConfig(BaseModel):
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "anthropic/claude-sonnet-4.6"
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout_seconds: float = 60.0


class ScheduleConfig(BaseModel):
    eod_summary_cron: str
    weekly_summary_cron: str
    reconciliation_interval_seconds: int = 3600
    strategy_tick_seconds: int = 60
    risk_monitor_tick_seconds: int = 1
    fill_poll_interval_seconds: int = 5
    bankroll_ttl_seconds: float = 10.0
    ws_watchdog_timeout_seconds: float = 60.0
    display_timezone: str = "America/New_York"


class Secrets(BaseSettings):
    """Secrets loaded exclusively from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    kalshi_api_key_id: SecretStr
    kalshi_private_key_path: Path
    discord_webhook_url: SecretStr | None = None
    control_bearer_token: SecretStr
    openrouter_api_key: SecretStr | None = None
    liveness_heartbeat_url: SecretStr | None = None


class Config(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    kalshi: KalshiConfig
    risk: RiskConfig
    fees: FeesConfig
    markets: MarketsConfig
    discord: DiscordConfig
    api: ApiConfig
    kill_switch: KillSwitchConfig
    storage: StorageConfig
    journal: JournalConfig
    schedule: ScheduleConfig
    strategy: StrategyConfig
    llm: LLMConfig = Field(default_factory=LLMConfig)
    secrets: Secrets

    @property
    def kalshi_base_url(self) -> str:
        return self.kalshi.base_url_live if self.mode == "live" else self.kalshi.base_url_paper

    @property
    def kalshi_ws_url(self) -> str:
        return self.kalshi.ws_url_live if self.mode == "live" else self.kalshi.ws_url_paper


def load_config(yaml_path: Path, mode_override: str | None = None) -> Config:
    raw = yaml.safe_load(yaml_path.read_text())
    if mode_override:
        raw["mode"] = mode_override
    raw["secrets"] = Secrets()
    return Config.model_validate(raw)
