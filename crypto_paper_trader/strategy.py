"""Regles de trading partagees entre le live (run_loop.py) et le backtest
(backtest_core.py), pour qu'un resultat de backtest decrive exactement ce
que l'add-on fera en direct.

Garde-fous contre le sur-trading (cause principale des pertes observees
d'octobre a novembre 2026 : ~60 allers-retours par actif en 36 jours, chacun
coutant 2 x 0,26 % de frais, pour un gain moyen avant frais de ~0,03 %) :
  - une seule decision par bougie cloturee (gere par l'appelant via
    candle_ts : le live saute les cycles qui retombent sur la meme bougie) ;
  - une seule entree par position (pas de rachat a chaque cycle tant que le
    signal reste "buy"), sauf si allow_pyramiding=true ;
  - duree minimale de detention avant une sortie sur signal (le stop-loss et
    le take-profit restent toujours actifs) ;
  - delai minimal apres une vente avant de racheter le meme actif ;
  - exposition totale plafonnee (en plus du plafond par actif).
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from indicators import score_market

# Valeurs par defaut des nouvelles options (aussi utilisees si l'option est
# absente de /data/options.json, ex : config pas encore re-sauvegardee).
DEFAULTS = {
    "min_hold_bars": 4,
    "cooldown_bars": 2,
    "allow_pyramiding": False,
    "max_total_exposure_pct": 100.0,
    "trend_filter_days": 100,
}

MIN_ORDER_EUR = 10.0


def trend_bars(cfg: dict, tf_ms: int) -> int:
    """Nombre de bougies couvertes par le filtre de tendance (0 = desactive)."""
    days = cfg.get("trend_filter_days", DEFAULTS["trend_filter_days"]) or 0
    return int(days * 86_400_000 // tf_ms) if days > 0 else 0


def add_trend_column(df: pd.DataFrame, cfg: dict, tf_ms: int) -> pd.DataFrame:
    """Ajoute sma_trend : moyenne mobile du prix sur trend_filter_days jours.

    Backtest sur 2 ans (BTC/ETH/XRP, bougies 4h) : le score seul n'a aucun
    avantage, meme sans frais (~-5 %), mais n'acheter que quand le prix est
    au-dessus de sa moyenne 100 jours (et sortir quand il passe dessous)
    ramene la perte de -27 % a -8 % et la pire baisse de -47 % a -17 %.
    Meme effet avec 200 jours : ce n'est pas un reglage cale sur le passe."""
    n = trend_bars(cfg, tf_ms)
    df["sma_trend"] = df["close"].rolling(n).mean() if n else float("nan")
    return df


def empty_position() -> dict:
    return {"qty": 0.0, "entry_price": None, "invested": 0.0,
            "entry_ts": None, "exit_ts": None, "last_candle_ts": None}


def weights_from(cfg: dict, include_news: bool = True) -> dict:
    w = {
        "rsi": cfg["w_rsi"], "macd": cfg["w_macd"], "bollinger": cfg["w_bollinger"],
        "trend": cfg["w_trend"], "volume": cfg["w_volume"], "sentiment": cfg["w_sentiment"],
    }
    if include_news:
        w["news_sentiment"] = cfg["w_news_sentiment"]
    return w


def compute_signals(use_score_engine: bool, df: pd.DataFrame, cfg: dict,
                    fear_greed_value: float | None = None,
                    news_sentiment_score: float | None = None,
                    score_result: dict | None = None) -> dict:
    """Signaux d'entree/sortie sur la derniere bougie de df (resultat de
    compute_all, + sma_trend si le filtre de tendance est actif).
    score_result peut etre fourni s'il a deja ete calcule."""
    signals = _raw_signals(use_score_engine, df, cfg, fear_greed_value, news_sentiment_score, score_result)
    if (cfg.get("trend_filter_days", DEFAULTS["trend_filter_days"]) or 0) > 0:
        sma_trend = df["sma_trend"].iat[-1] if "sma_trend" in df else float("nan")
        if pd.isna(sma_trend):
            # historique trop court pour calculer la moyenne : on n'achete pas
            if signals["buy"]:
                signals["buy_block"] = "achat bloque : historique trop court pour le filtre de tendance"
            signals["buy"] = False
        elif df["close"].iat[-1] <= sma_trend:
            if signals["buy"]:
                signals["buy_block"] = "achat bloque : sous la tendance de fond"
            signals["buy"] = False
            signals["sell"] = True
            signals["sell_reason"] = "sous la tendance de fond"
    return signals


def _raw_signals(use_score_engine, df, cfg, fear_greed_value, news_sentiment_score, score_result):
    if use_score_engine:
        if score_result is None:
            score_result = score_market(
                df, rsi_buy=cfg["rsi_buy"], rsi_sell=cfg["rsi_sell"], weights=weights_from(cfg),
                buy_threshold=cfg["score_buy_threshold"], sell_threshold=cfg["score_sell_threshold"],
                atr_stop_mult=cfg["atr_stop_mult"], atr_target_mult=cfg["atr_target_mult"],
                fear_greed_value=fear_greed_value, news_sentiment_score=news_sentiment_score,
            )
        label = f"score {score_result['score']:+.2f}"
        return {"buy": score_result["action"] == "buy", "sell": score_result["action"] == "sell",
                "buy_reason": label, "sell_reason": label, "score_result": score_result}

    last, prev = df.iloc[-1], df.iloc[-2]
    bullish_cross = prev["sma_fast"] <= prev["sma_slow"] and last["sma_fast"] > last["sma_slow"]
    bearish_cross = prev["sma_fast"] >= prev["sma_slow"] and last["sma_fast"] < last["sma_slow"]
    return {
        "buy": bool(last["rsi"] < cfg["rsi_buy"] or bullish_cross),
        "sell": bool(last["rsi"] > cfg["rsi_sell"] or bearish_cross),
        "buy_reason": "RSI survente" if last["rsi"] < cfg["rsi_buy"] else "croisement haussier",
        "sell_reason": "RSI surachat" if last["rsi"] > cfg["rsi_sell"] else "croisement baissier",
        "score_result": score_result,
    }


def _bars_between(ts_from: int | None, ts_to: int, tf_ms: int) -> float:
    if ts_from is None:
        return float("inf")
    return (ts_to - ts_from) / tf_ms


def apply_step(book: dict, key: str, price: float, candle_ts: int, tf_ms: int,
               signals: dict, cfg: dict, prices: dict[str, float],
               record: Callable[..., None]) -> tuple[str, str | None]:
    """Applique les regles a un actif pour une bougie cloturee.

    book : {"cash": float, "positions": {key: position}} (modifie en place).
    prices : dernier prix connu de chaque actif (pour la valeur totale).
    record(action, price, qty, fee, pnl_pct, reason) : journalise un trade.
    Retourne (action effective "BUY"/"SELL"/"HOLD", raison).
    """
    fee = cfg["fee_pct"] / 100
    min_hold = cfg.get("min_hold_bars", DEFAULTS["min_hold_bars"])
    cooldown = cfg.get("cooldown_bars", DEFAULTS["cooldown_bars"])
    pyramiding = cfg.get("allow_pyramiding", DEFAULTS["allow_pyramiding"])
    max_total = cfg.get("max_total_exposure_pct", DEFAULTS["max_total_exposure_pct"]) / 100
    pos = book["positions"][key]

    # --- Sortie ---
    if pos["qty"] > 0:
        pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
        stop_hit = pnl_pct <= cfg["stop_loss_pct"] / 100
        tp_hit = pnl_pct >= cfg["take_profit_pct"] / 100
        held = _bars_between(pos.get("entry_ts"), candle_ts, tf_ms)
        signal_exit = signals["sell"] and held >= min_hold
        if stop_hit or tp_hit or signal_exit:
            proceeds = pos["qty"] * price
            trade_fee = proceeds * fee
            book["cash"] += proceeds - trade_fee
            reason = "stop-loss" if stop_hit else "take-profit" if tp_hit else signals["sell_reason"]
            record("SELL", price, pos["qty"], trade_fee, pnl_pct * 100, reason)
            fresh = empty_position()
            fresh["exit_ts"] = candle_ts
            fresh["last_candle_ts"] = pos.get("last_candle_ts")
            book["positions"][key] = fresh
            return "SELL", reason
        if signals["sell"]:
            return "HOLD", f"signal sell ignore (detention {held:.0f}/{min_hold} bougies)"

    # --- Entree ---
    if not signals["buy"]:
        return "HOLD", signals.get("buy_block")
    if pos["qty"] > 0 and not pyramiding:
        return "HOLD", "deja en position"
    since_exit = _bars_between(pos.get("exit_ts"), candle_ts, tf_ms)
    if since_exit < cooldown:
        return "HOLD", f"cooldown apres vente ({since_exit:.0f}/{cooldown} bougies)"

    total = book["cash"] + sum(p["qty"] * prices.get(k, 0.0) for k, p in book["positions"].items())
    invested = total - book["cash"]
    room_asset = total * cfg["max_exposure_per_asset_pct"] / 100 - pos["qty"] * price
    room_total = total * max_total - invested
    invest_amount = min(room_asset, room_total, book["cash"])
    if invest_amount < MIN_ORDER_EUR:
        return "HOLD", "signal buy mais montant < 10"

    trade_fee = invest_amount * fee
    qty = (invest_amount - trade_fee) / price
    book["cash"] -= invest_amount
    if pos["qty"] > 0 and pos["entry_price"] is not None:
        pos["entry_price"] = (pos["qty"] * pos["entry_price"] + qty * price) / (pos["qty"] + qty)
    else:
        pos["entry_price"] = price
        pos["entry_ts"] = candle_ts
    pos["qty"] += qty
    pos["invested"] = pos.get("invested", 0.0) + invest_amount
    record("BUY", price, qty, trade_fee, None, signals["buy_reason"])
    return "BUY", signals["buy_reason"]
