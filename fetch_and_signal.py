#!/usr/bin/env python3
"""Récupère les cours BTC/ETH, calcule RSI+SMA, pousse un signal vers Home Assistant.

Conçu pour tourner en cron/systemd sur un Raspberry Pi (accès réseau normal,
pas de restriction particulière côté exchange).

Dépendances: pip install -r requirements.txt
Config: copier config.example.yaml -> config.yaml et remplir tes valeurs.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import ccxt
import pandas as pd
import yaml

from ha_client import HomeAssistantClient
from indicators import classify_signal, compute_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("crypto-signals")

HISTORY_DIR = Path(__file__).parent / "history"


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_ohlcv(exchange_id: str, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls({"enableRateLimit": True})
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df


def append_history(symbol_key: str, row: pd.Series) -> None:
    HISTORY_DIR.mkdir(exist_ok=True)
    fpath = HISTORY_DIR / f"{symbol_key}.csv"
    header = not fpath.exists()
    row.to_frame().T.to_csv(fpath, mode="a", header=header, index=False)


def process_symbol(cfg: dict, symbol_cfg: dict, ha: HomeAssistantClient) -> None:
    symbol = symbol_cfg["symbol"]          # ex: "BTC/USDT"
    entity_key = symbol_cfg["entity_key"]  # ex: "btc"
    exchange_id = cfg.get("exchange", "binance")
    timeframe = cfg.get("timeframe", "1d")
    limit = cfg.get("limit", 60)

    df = fetch_ohlcv(exchange_id, symbol, timeframe, limit)
    df = compute_all(df, fast=cfg["sma_fast"], slow=cfg["sma_slow"], rsi_period=cfg["rsi_period"])
    signal = classify_signal(df, rsi_buy=cfg["rsi_buy"], rsi_sell=cfg["rsi_sell"])

    last = df.iloc[-1]
    log.info(
        "%s close=%.2f rsi=%.1f sma_fast=%.2f sma_slow=%.2f -> %s",
        symbol, last["close"], last["rsi"], last["sma_fast"], last["sma_slow"], signal,
    )

    append_history(entity_key, last)

    entity_id = f"sensor.{entity_key}_signal"
    ha.set_state(
        entity_id,
        state=signal,
        attributes={
            "friendly_name": f"{symbol} signal",
            "close": round(float(last["close"]), 2),
            "rsi": round(float(last["rsi"]), 1),
            "sma_fast": round(float(last["sma_fast"]), 2),
            "sma_slow": round(float(last["sma_slow"]), 2),
            "unit_of_measurement": "signal",
        },
    )


def main() -> int:
    cfg = load_config()
    ha = HomeAssistantClient(cfg["ha_base_url"], cfg["ha_token"])

    for symbol_cfg in cfg["symbols"]:
        try:
            process_symbol(cfg, symbol_cfg, ha)
        except Exception:
            log.exception("Échec traitement %s", symbol_cfg.get("symbol"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
