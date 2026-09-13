"""État du portefeuille de simulation, persisté dans /data/portfolio.json.

Ce fichier survit aux redémarrages de l'add-on (le dossier /data est
persistant). Pour repartir de zéro : supprime portfolio.json (via l'add-on
File editor/Terminal & SSH) ou désinstalle/réinstalle l'add-on.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("crypto-paper-trader")

MAX_TRADE_LOG = 200  # on ne garde que les N derniers trades dans l'état


def load_state(path: Path, initial_capital: float, symbol_keys: list[str]) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        # ajoute les actifs manquants si la config a change depuis (nouveau symbole)
        for key in symbol_keys:
            state["positions"].setdefault(key, {"qty": 0.0, "entry_price": None, "invested": 0.0})
        return state

    log.info("Aucun portfolio.json existant -> initialisation avec %.2f de capital", initial_capital)
    return {
        "cash": initial_capital,
        "initial_capital": initial_capital,
        "positions": {key: {"qty": 0.0, "entry_price": None, "invested": 0.0} for key in symbol_keys},
        "trades": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["trades"] = state["trades"][-MAX_TRADE_LOG:]
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    tmp.replace(path)  # ecriture atomique, evite un fichier corrompu en cas de coupure


def portfolio_value(state: dict, prices: dict[str, float]) -> float:
    total = state["cash"]
    for key, pos in state["positions"].items():
        total += pos["qty"] * prices.get(key, 0.0)
    return total


def record_trade(state: dict, **kwargs) -> None:
    entry = {"date": datetime.now(timezone.utc).isoformat(), **kwargs}
    state["trades"].append(entry)
    log.info("TRADE %s", entry)
