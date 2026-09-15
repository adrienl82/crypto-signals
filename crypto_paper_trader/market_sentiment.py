"""Indice de sentiment global du marche crypto (Fear & Greed Index).

Source: alternative.me, API publique et gratuite, sans cle. La valeur ne
change qu'environ 1x/jour cote alternative.me -- on la met en cache pour
ne pas la re-interroger a chaque cycle (par defaut toutes les 5 min).

Contrairement au score par indicateur (RSI, MACD, ...) qui est specifique a
un actif, cet indice est global au marche crypto : il sert de contexte
("le marche est en euphorie / en capitulation ?"), pas de signal isole.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

_FNG_URL = "https://api.alternative.me/fng/"
_TTL_SECONDS = 3 * 3600  # la donnee ne bouge qu'1x/jour, 3h de cache est large

_cache: dict = {"data": None, "fetched_at": 0.0}


def fetch_fear_greed(force: bool = False) -> dict | None:
    """Retourne un dict {value, classification, previous_value, trend, timestamp}
    ou None si l'API est indisponible et qu'aucune valeur en cache n'existe.

    value : 0 (peur extreme) .. 100 (avidite extreme).
    trend : delta par rapport a la veille (positif = plus d'avidite).
    """
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["fetched_at"] < _TTL_SECONDS:
        return _cache["data"]

    try:
        resp = requests.get(_FNG_URL, params={"limit": 2}, timeout=10)
        resp.raise_for_status()
        payload = resp.json()["data"]
        current = payload[0]
        previous = payload[1] if len(payload) > 1 else None

        value = float(current["value"])
        previous_value = float(previous["value"]) if previous else None
        data = {
            "value": value,
            "classification": current["value_classification"],
            "timestamp": int(current["timestamp"]),
            "previous_value": previous_value,
            "trend": round(value - previous_value, 2) if previous_value is not None else None,
        }
        _cache["data"] = data
        _cache["fetched_at"] = now
        return data
    except Exception:
        log.exception("Fear & Greed Index indisponible")
        return _cache["data"]  # derniere valeur connue si on en a une, sinon None
