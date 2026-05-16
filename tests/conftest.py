"""Shared fixtures: in-memory DB, ephemeral kill switch, config factory."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from kalshi_agent.config import (
    ApiConfig,
    Config,
    DiscordConfig,
    FeesConfig,
    JournalConfig,
    KalshiConfig,
    KillSwitchConfig,
    MarketsConfig,
    PlaceholderStrategyConfig,
    RiskConfig,
    ScheduleConfig,
    Secrets,
    StorageConfig,
    StrategyConfig,
)
from kalshi_agent.safety.kill_switch import KillSwitch
from kalshi_agent.storage.db import _enable_sqlite_pragmas
from kalshi_agent.storage.models import Base


@pytest.fixture
def rsa_private_key_path(tmp_path: Path) -> Path:
    """Generate a fresh RSA key for signing tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "key.pem"
    p.write_bytes(pem)
    return p


@pytest.fixture
def secrets(rsa_private_key_path: Path, monkeypatch) -> Secrets:
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(rsa_private_key_path))
    monkeypatch.setenv("CONTROL_BEARER_TOKEN", "test-bearer-token-aaaaaaaaaaaaaaaaaaaa")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    return Secrets()


@pytest.fixture
def config(tmp_path: Path, secrets: Secrets) -> Config:
    return Config(
        mode="paper",
        kalshi=KalshiConfig(
            base_url_paper="https://demo-api.kalshi.co/trade-api/v2",
            base_url_live="https://api.elections.kalshi.com/trade-api/v2",
            ws_url_paper="wss://demo-api.kalshi.co/trade-api/ws/v2",
            ws_url_live="wss://api.elections.kalshi.com/trade-api/ws/v2",
        ),
        risk=RiskConfig(
            max_position_per_market_usd=25.0,
            max_total_exposure_usd=200.0,
            max_daily_loss_usd=50.0,
            per_order_max_contracts=50,
            max_orders_per_minute=10,
            min_edge_after_fees_bps=200,
            kelly_fraction=0.25,
            max_kelly_size_pct_bankroll=0.05,
        ),
        fees=FeesConfig(),
        markets=MarketsConfig(whitelist_categories=["economics", "kpi"]),
        discord=DiscordConfig(),
        api=ApiConfig(),
        kill_switch=KillSwitchConfig(halt_file_path=tmp_path / "HALT"),
        storage=StorageConfig(db_path=tmp_path / "test.db"),
        journal=JournalConfig(log_dir=tmp_path / "logs"),
        schedule=ScheduleConfig(
            eod_summary_cron="59 23 * * *",
            weekly_summary_cron="59 23 * * 0",
        ),
        strategy=StrategyConfig(
            placeholder=PlaceholderStrategyConfig(test_ticker="TEST-MARKET")
        ),
        secrets=secrets,
    )


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    event.listen(engine, "connect", _enable_sqlite_pragmas)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session_maker(db_engine):
    return sessionmaker(bind=db_engine, expire_on_commit=False, future=True)


@pytest.fixture
def kill_switch(config: Config) -> KillSwitch:
    return KillSwitch(config.kill_switch.halt_file_path)


@pytest.fixture
def bankroll() -> Decimal:
    return Decimal("1000")
