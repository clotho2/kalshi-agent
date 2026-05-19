"""Entry point: parse args, wire components, run forever."""

from __future__ import annotations

import argparse
import asyncio
import signal
from decimal import Decimal
from pathlib import Path

import uvicorn

from kalshi_agent.bankroll import Bankroll
from kalshi_agent.config import load_config
from kalshi_agent.execution import Executor
from kalshi_agent.fills import FillIngestor
from kalshi_agent.journal.discord import DiscordNotifier
from kalshi_agent.journal.logger import configure_logging, get_logger
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.liveness import LivenessHeartbeat
from kalshi_agent.llm.openrouter import OpenRouterClient
from kalshi_agent.recovery import recover_in_flight_orders
from kalshi_agent.safety.halt_actions import HaltMonitor
from kalshi_agent.safety.kill_switch import KillSwitch
from kalshi_agent.safety.reconciliation import Reconciler
from kalshi_agent.safety.risk_monitor import RiskMonitor
from kalshi_agent.scheduler import build_scheduler
from kalshi_agent.storage.db import healthcheck, init_db, make_engine, session_factory
from kalshi_agent.strategies.llm_market_assessor import LLMMarketAssessor
from kalshi_agent.strategies.placeholder import PlaceholderStrategy


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="kalshi-agent")
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--mode", choices=["paper", "live"], default=None,
                   help="override config mode")
    return p.parse_args()


async def amain(args: argparse.Namespace) -> int:
    config = load_config(args.config, mode_override=args.mode)
    configure_logging(config.journal.log_dir, config.journal.retention_days)
    log = get_logger("kalshi_agent.main")

    log.info("startup", mode=config.mode, base_url=config.kalshi_base_url,
             strategy=config.strategy.active)

    engine = make_engine(config.storage.db_path)
    init_db(engine)
    sm = session_factory(engine)

    kill = KillSwitch(config.kill_switch.halt_file_path)
    discord = DiscordNotifier(
        config.secrets.discord_webhook_url.get_secret_value()
        if config.secrets.discord_webhook_url else None
    )

    client_cm = KalshiClient(
        base_url=config.kalshi_base_url,
        api_key_id=config.secrets.kalshi_api_key_id.get_secret_value(),
        private_key_path=config.secrets.kalshi_private_key_path,
        rate_limit_reads_per_second=config.kalshi.rate_limit_reads_per_second,
        rate_limit_writes_per_second=config.kalshi.rate_limit_writes_per_second,
    )
    client = await client_cm.__aenter__()

    reconciler = Reconciler(client, sm, discord)
    risk = RiskMonitor(config, kill, sm)
    bankroll = Bankroll(client, ttl_seconds=config.schedule.bankroll_ttl_seconds,
                        fallback_dollars=Decimal("0"))
    halt_monitor = HaltMonitor(kill, client, sm, discord)
    fill_ingestor = FillIngestor(config, client, sm, discord)

    # Strategy selection
    llm_client_cm: OpenRouterClient | None = None
    llm_client: OpenRouterClient | None = None
    if config.strategy.active == "llm_assessor":
        if not config.secrets.openrouter_api_key:
            log.error("openrouter_api_key_missing")
            await client_cm.__aexit__(None, None, None)
            return 2
        if not config.strategy.llm_assessor:
            log.error("llm_assessor_config_missing")
            await client_cm.__aexit__(None, None, None)
            return 2
        la = config.strategy.llm_assessor
        # Either manual tickers OR discovery categories must be provided
        if not la.tickers and not la.categories:
            log.error("llm_assessor_needs_tickers_or_categories")
            await client_cm.__aexit__(None, None, None)
            return 2
        llm_client_cm = OpenRouterClient(
            api_key=config.secrets.openrouter_api_key.get_secret_value(),
            model=config.llm.model,
            base_url=config.llm.base_url,
            timeout=config.llm.timeout_seconds,
        )
        llm_client = await llm_client_cm.__aenter__()
        strategy = LLMMarketAssessor(
            kalshi_client=client,
            llm_client=llm_client,
            session_maker=sm,
            tickers=la.tickers,
            categories=la.categories or config.markets.whitelist_categories,
            max_markets_per_tick=la.max_markets_per_tick,
            min_volume_contracts=la.min_volume_contracts,
            min_hours_to_close=la.min_hours_to_close,
            discovery_max_pages=la.discovery_max_pages,
            min_edge=Decimal(str(la.min_edge)),
            min_confidence=Decimal(str(la.min_confidence)),
            signal_ttl_minutes=la.signal_ttl_minutes,
            min_seconds_between_signals_per_ticker=la.min_seconds_between_signals_per_ticker,
        )
    else:
        strategy = PlaceholderStrategy(
            config.strategy.placeholder.test_ticker,
            config.strategy.placeholder.emit_interval_seconds,
        )

    executor = Executor(config, client, risk, reconciler, sm, discord, bankroll)

    sched = build_scheduler(config, strategy, executor, reconciler, fill_ingestor, sm, discord)
    sched.start()

    stop_event = asyncio.Event()
    risk_task = asyncio.create_task(risk.run_background(stop_event))
    halt_task = asyncio.create_task(halt_monitor.run(stop_event))
    fill_task = asyncio.create_task(
        fill_ingestor.poll_loop(stop_event, config.schedule.fill_poll_interval_seconds)
    )

    liveness = LivenessHeartbeat(
        config.secrets.liveness_heartbeat_url.get_secret_value()
        if config.secrets.liveness_heartbeat_url else None
    )
    liveness_task = asyncio.create_task(liveness.run(stop_event))

    # Startup: recover in-flight orders, then refresh bankroll, then reconcile, then catch up fills
    async def _startup_sequence() -> None:
        try:
            await recover_in_flight_orders(client, sm, discord)
        except Exception as e:
            log.error("startup_recovery_failed", error=str(e))
        try:
            await bankroll.refresh()
        except Exception as e:
            log.error("startup_bankroll_failed", error=str(e))
        try:
            await reconciler.reconcile(source="startup")
        except Exception as e:
            log.error("startup_reconcile_failed", error=str(e))
        try:
            await fill_ingestor.catch_up()
        except Exception as e:
            log.error("startup_fill_catchup_failed", error=str(e))

    startup_task = asyncio.create_task(_startup_sequence())

    from kalshi_agent.api.server import create_app

    async def trigger_reconcile() -> dict:
        result = await reconciler.reconcile(source="manual_http")
        await fill_ingestor.catch_up()
        return result

    app = create_app(config, sm, kill, trigger_reconcile, lambda: healthcheck(engine))
    uconfig = uvicorn.Config(app, host=config.api.host, port=config.api.port,
                             log_level="info", lifespan="off")
    server = uvicorn.Server(uconfig)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: stop_event.set())

    server_task = asyncio.create_task(server.serve())
    log.info("server_started", host=config.api.host, port=config.api.port)
    if discord.enabled:
        await discord.post(
            f":rocket: kalshi-agent started in **{config.mode}** mode "
            f"(strategy: {config.strategy.active})"
        )

    await stop_event.wait()
    log.info("shutdown_signal_received")
    server.should_exit = True
    sched.shutdown(wait=False)
    for t in (risk_task, halt_task, fill_task, liveness_task, startup_task):
        t.cancel()
    await asyncio.gather(server_task, return_exceptions=True)
    if llm_client_cm:
        await llm_client_cm.__aexit__(None, None, None)
    await client_cm.__aexit__(None, None, None)
    log.info("shutdown_complete")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
