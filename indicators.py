"""Indicateurs techniques - calculs purs, sans dépendance réseau."""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def classify_signal(
    df: pd.DataFrame,
    rsi_buy: float = 35.0,
    rsi_sell: float = 65.0,
) -> str:
    """Retourne 'buy', 'sell' ou 'hold' à partir de la dernière ligne du df.

    df doit contenir les colonnes: close, sma_fast, sma_slow, rsi
    Règle simple, volontairement lisible : RSI en zone extrême
    + confirmation de tendance par le croisement des moyennes mobiles.
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    bullish_cross = prev["sma_fast"] <= prev["sma_slow"] and last["sma_fast"] > last["sma_slow"]
    bearish_cross = prev["sma_fast"] >= prev["sma_slow"] and last["sma_fast"] < last["sma_slow"]

    if last["rsi"] < rsi_buy or bullish_cross:
        return "buy"
    if last["rsi"] > rsi_sell or bearish_cross:
        return "sell"
    return "hold"


def compute_all(df: pd.DataFrame, fast: int = 5, slow: int = 10, rsi_period: int = 14) -> pd.DataFrame:
    out = df.copy()
    out["sma_fast"] = sma(out["close"], fast)
    out["sma_slow"] = sma(out["close"], slow)
    out["rsi"] = rsi(out["close"], rsi_period)
    return out
