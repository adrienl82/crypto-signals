"""Indicateurs techniques - calculs purs, sans dépendance réseau."""
from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(period).std()
    return mid + num_std * std, mid, mid - num_std * std


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low = df["high"], df["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)
    tr = atr(df, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(alpha=1 / period).mean()


def volume_spike(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Ratio volume courant / moyenne mobile du volume. >1.2 = pic de volume notable."""
    avg_vol = sma(df["volume"], period)
    return df["volume"] / avg_vol.replace(0, float("nan"))


def classify_signal(df: pd.DataFrame, rsi_buy: float = 35.0, rsi_sell: float = 65.0) -> str:
    """Retourne 'buy', 'sell' ou 'hold' à partir de la dernière ligne du df (logique historique,
    conservée pour compatibilité quand use_score_engine=false).

    df doit contenir les colonnes: close, sma_fast, sma_slow, rsi
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

    # indicateurs additionnels pour le moteur de score (score_market ci-dessous)
    _, _, macd_hist = macd(out["close"])
    out["macd_hist"] = macd_hist
    bb_upper, bb_mid, bb_lower = bollinger_bands(out["close"])
    out["bb_upper"], out["bb_mid"], out["bb_lower"] = bb_upper, bb_mid, bb_lower
    out["atr"] = atr(out)
    out["adx"] = adx(out)
    out["vol_ratio"] = volume_spike(out)
    return out


# ---------------------------------------------------------------------------
# Moteur de score multi-indicateurs (activé par use_score_engine dans config.yaml)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "rsi": 1.0, "macd": 1.0, "bollinger": 0.7, "trend": 0.8, "volume": 0.5, "sentiment": 0.4,
    "news_sentiment": 0.4,
}


def _score_rsi(value: float, rsi_buy: float, rsi_sell: float) -> float:
    if pd.isna(value):
        return 0.0
    if value <= rsi_buy:
        return 1.0
    if value >= rsi_sell:
        return -1.0
    mid = (rsi_buy + rsi_sell) / 2
    span = (rsi_sell - rsi_buy) / 2
    return (mid - value) / span if span else 0.0


def _score_macd(hist_last: float, hist_prev: float) -> float:
    if pd.isna(hist_last) or pd.isna(hist_prev):
        return 0.0
    if hist_prev <= 0 and hist_last > 0:
        return 1.0
    if hist_prev >= 0 and hist_last < 0:
        return -1.0
    return 0.3 if hist_last > 0 else -0.3


def _score_bollinger(close: float, upper: float, lower: float, mid: float) -> float:
    if pd.isna(upper) or pd.isna(lower):
        return 0.0
    if close <= lower:
        return 1.0
    if close >= upper:
        return -1.0
    span = upper - lower
    return (mid - close) / (span / 2) if span else 0.0


def _score_trend(adx_value: float, price_above_slow_sma: bool) -> float:
    if pd.isna(adx_value):
        return 0.0
    strength = min(adx_value / 40.0, 1.0)
    return strength if price_above_slow_sma else -strength


def _score_volume(vol_ratio: float, price_up: bool) -> float:
    if pd.isna(vol_ratio) or vol_ratio < 1.2:
        return 0.0
    boost = min(vol_ratio - 1.0, 1.0)
    return boost if price_up else -boost


def _score_sentiment(fng_value: float | None) -> float | None:
    """Fear & Greed Index, utilise en contrarien : peur extreme => plutot
    favorable a l'achat (score positif), avidite extreme => plutot prudent
    (score negatif). Retourne None si la donnee n'est pas disponible, pour
    que score_market() puisse l'exclure proprement du calcul (plutot que de
    diluer le score avec un 0 qui n'a pas de sens ici)."""
    if fng_value is None:
        return None
    if fng_value <= 25:
        return 1.0
    if fng_value >= 75:
        return -1.0
    mid, span = 50.0, 25.0
    return (mid - fng_value) / span


def score_market(
    df: pd.DataFrame,
    rsi_buy: float = 35.0,
    rsi_sell: float = 65.0,
    weights: dict | None = None,
    buy_threshold: float = 0.35,
    sell_threshold: float = -0.35,
    atr_stop_mult: float = 1.5,
    atr_target_mult: float = 3.0,
    fear_greed_value: float | None = None,
    news_sentiment_score: float | None = None,
) -> dict:
    """Combine RSI, MACD, Bollinger, ADX (force de tendance), volume et,
    optionnellement, l'indice Fear & Greed (sentiment global du marche, en
    contrarien) en un score pondere -1..+1, au lieu d'un seul seuil RSI /
    croisement SMA.

    df doit être le résultat de compute_all() (colonnes rsi, macd_hist, bb_*, adx, vol_ratio, atr).
    fear_greed_value : valeur 0..100 de l'indice Fear & Greed du jour (meme
    valeur pour tous les actifs, c'est un contexte marche global) ou None
    pour l'exclure du calcul (ex: API indisponible).
    news_sentiment_score : moyenne -1..+1 du sentiment des dernieres news
    propres a cet actif (cf. news_sentiment_model.py), ou None si le modele
    est indisponible ou qu'aucune news recente n'a ete classifiee -- distinct
    du Fear & Greed qui est un contexte marche global, pas par actif.
    Retourne : action, score, confidence, breakdown par indicateur, et stop/target
    suggérés à partir de l'ATR courant (utile en info même si non utilisé pour trader).
    """
    weights = weights or DEFAULT_WEIGHTS
    last, prev = df.iloc[-1], df.iloc[-2]
    price_up = last["close"] > prev["close"]
    trend_up = last["close"] > last["sma_slow"]

    parts = {
        "rsi": (_score_rsi(last["rsi"], rsi_buy, rsi_sell), weights.get("rsi", 1.0)),
        "macd": (_score_macd(last["macd_hist"], prev["macd_hist"]), weights.get("macd", 1.0)),
        "bollinger": (
            _score_bollinger(last["close"], last["bb_upper"], last["bb_lower"], last["bb_mid"]),
            weights.get("bollinger", 0.7),
        ),
        "trend": (_score_trend(last["adx"], trend_up), weights.get("trend", 0.8)),
        "volume": (_score_volume(last["vol_ratio"], price_up), weights.get("volume", 0.5)),
    }

    sentiment_score = _score_sentiment(fear_greed_value)
    if sentiment_score is not None:
        parts["sentiment"] = (sentiment_score, weights.get("sentiment", DEFAULT_WEIGHTS["sentiment"]))

    if news_sentiment_score is not None:
        parts["news_sentiment"] = (
            news_sentiment_score,
            weights.get("news_sentiment", DEFAULT_WEIGHTS["news_sentiment"]),
        )

    total_w = sum(w for _, w in parts.values())
    score = sum(s * w for s, w in parts.values()) / total_w if total_w else 0.0

    if score >= buy_threshold:
        action = "buy"
    elif score <= sell_threshold:
        action = "sell"
    else:
        action = "hold"

    atr_val = last["atr"]
    stop = target = None
    if not pd.isna(atr_val):
        if action == "buy":
            stop = last["close"] - atr_stop_mult * atr_val
            target = last["close"] + atr_target_mult * atr_val
        elif action == "sell":
            stop = last["close"] + atr_stop_mult * atr_val
            target = last["close"] - atr_target_mult * atr_val

    return {
        "action": action,
        "score": round(float(score), 4),
        "confidence": round(abs(float(score)), 4),
        "breakdown": {name: round(float(s), 3) for name, (s, _) in parts.items()},
        "atr": round(float(atr_val), 6) if not pd.isna(atr_val) else None,
        "stop": round(float(stop), 6) if stop is not None else None,
        "target": round(float(target), 6) if target is not None else None,
    }
