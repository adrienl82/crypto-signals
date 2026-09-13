#!/usr/bin/env python3
"""Point d'entrée de l'add-on. Tourne en boucle infinie (pas de cron/systemd
nécessaire : c'est le conteneur de l'add-on lui-même qui reste "up")."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from ha_client import HomeAssistantClient
from indicators import classify_signal, compute_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crypto-signals")

OPTIONS_PATH = Path("/data/options.json")
HISTORY_DIR = Path("/data/history")


def load_options() -> dict:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_ohlcv(exchange_id: str, symbol: str, timeframe: str, limit: int = 60) -> pd.DataFrame:
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df


def append_history(entity_key: str, row: pd.Series) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    fpath = HISTORY_DIR / f"{entity_key}.csv"
    header = not fpath.exists()
    row.to_frame().T.to_csv(fpath, mode="a", header=header, index=False)


def process_symbol(opts: dict, symbol_cfg: dict, ha: HomeAssistantClient) -> None:
    symbol = symbol_cfg["symbol"]
    entity_key = symbol_cfg["entity_key"]

    df = fetch_ohlcv(opts["exchange"], symbol, opts["timeframe"])
    df = compute_all(df, fast=opts["sma_fast"], slow=opts["sma_slow"], rsi_period=opts["rsi_period"])
    signal = classify_signal(df, rsi_buy=opts["rsi_buy"], rsi_sell=opts["rsi_sell"])

    last = df.iloc[-1]
    log.info(
        "%s close=%.2f rsi=%.1f sma_fast=%.2f sma_slow=%.2f -> %s",
        symbol, last["close"], last["rsi"], last["sma_fast"], last["sma_slow"], signal,
    )
    append_history(entity_key, last)

    ha.set_state(
        f"sensor.{entity_key}_signal",
        state=signal,
        attributes={
            "friendly_name": f"{symbol} signal",
            "close": round(float(last["close"]), 2),
            "rsi": round(float(last["rsi"]), 1),
            "sma_fast": round(float(last["sma_fast"]), 2),
            "sma_slow": round(float(last["sma_slow"]), 2),
        },
    )

    # Capteurs numériques séparés : nécessaires pour que les cartes Lovelace
    # (history-graph, sensor card...) puissent tracer une courbe de tendance.
    # L'état d'un capteur texte ("hold"/"buy"/"sell") n'est pas graphable,
    # mais son historique reste dispo dans l'onglet "Historique" de l'entité.
    ha.set_state(
        f"sensor.{entity_key}_price",
        state=round(float(last["close"]), 2),
        attributes={
            "friendly_name": f"{symbol} prix",
            "unit_of_measurement": "USD",
            "device_class": "monetary",
            "state_class": "measurement",
        },
    )
    ha.set_state(
        f"sensor.{entity_key}_rsi",
        state=round(float(last["rsi"]), 1),
        attributes={
            "friendly_name": f"{symbol} RSI",
            "state_class": "measurement",
        },
    )


def main() -> None:
    opts = load_options()
    ha = HomeAssistantClient(
        base_url=opts.get("ha_base_url") or None,
        token=opts.get("ha_token") or None,
    )
    while True:
        opts = load_options()  # relu à chaque cycle: la config est modifiable à chaud depuis l'UI
        for symbol_cfg in opts["symbols"]:
            try:
                process_symbol(opts, symbol_cfg, ha)
            except Exception:
                log.exception("Échec traitement %s", symbol_cfg.get("symbol"))
        time.sleep(max(60, int(opts.get("interval_minutes", 60)) * 60))


if __name__ == "__main__":
    main()
