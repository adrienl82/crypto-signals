"""
backtest_core.py - Backtest embarque, declenchable depuis Home Assistant.

Contrairement a backtest.py (a la racine du repo, pense pour etre lance a la
main sur un PC), ce module tourne DANS le conteneur de l'add-on -- il
reutilise donc directement les options live (config.yaml / opts), y compris
les poids/seuils actuellement configures, et beneficie de l'acces reseau du
Pi (qui fonctionne deja, contrairement a un sandbox dont le reseau sortant
serait restreint). Declenche via le helper HA input_boolean.run_crypto_backtest
(voir maybe_run_backtest() dans run_loop.py), a la maniere du reset de
portefeuille.

Duplique une partie de la logique de backtest.py (fetch_history, Sim,
run_strategy, metrics) plutot que de l'importer : l'image Docker ne contient
que crypto_paper_trader/ (cf. Dockerfile, COPY . . avec ce dossier comme
contexte de build), pas la racine du repo ou vit backtest.py.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd
import requests

from indicators import compute_all, score_market

log = logging.getLogger("crypto-paper-trader")

OUTDIR = Path("/share/crypto-backtest")


def fetch_history(exchange, symbol: str, timeframe: str, since_ms: int, limit: int = 720) -> pd.DataFrame:
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    now_ms = exchange.milliseconds()
    since = since_ms
    rows: list = []
    while since < now_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= since:
            break
        since = last_ts + tf_ms
        time.sleep(exchange.rateLimit / 1000)
        if len(batch) < limit:
            break
    if not rows:
        raise RuntimeError(f"Aucune donnee recuperee pour {symbol}")
    df = (
        pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        .drop_duplicates("ts")
        .sort_values("ts")
        .reset_index(drop=True)
    )
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    if len(df) and df.iloc[-1]["ts"] + tf_ms > exchange.milliseconds():
        df = df.iloc[:-1].reset_index(drop=True)
    return df


def fetch_fng_history() -> pd.Series | None:
    try:
        r = requests.get(
            "https://api.alternative.me/fng/", params={"limit": 0, "format": "json"}, timeout=20
        )
        r.raise_for_status()
        data = r.json()["data"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Historique Fear & Greed indisponible (%s) -- indicateur 'sentiment' exclu du backtest", exc)
        return None
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True)
    df["value"] = df["value"].astype(float)
    return df.set_index("date")["value"].sort_index()


def fng_at(fng_series: pd.Series | None, ts: pd.Timestamp) -> float | None:
    if fng_series is None:
        return None
    idx = fng_series.index.asof(ts)
    if pd.isna(idx):
        return None
    return float(fng_series.loc[idx])


class Sim:
    def __init__(self, initial_capital: float, keys: list[str]):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {k: {"qty": 0.0, "entry_price": None} for k in keys}
        self.trades: list[dict] = []
        self.equity: list[tuple] = []

    def value(self, prices: dict[str, float]) -> float:
        v = self.cash
        for k, pos in self.positions.items():
            v += pos["qty"] * prices.get(k, 0.0)
        return v

    def record(self, date, asset, action, price, qty, fee, pnl_pct, reason):
        self.trades.append(dict(date=date, asset=asset, action=action, price=price,
                                 qty=qty, fee=fee, pnl_pct=pnl_pct, reason=reason))


def run_strategy(mode: str, cfg: dict, assets: dict, calendar, initial_capital: float,
                  fng_series: pd.Series | None) -> Sim:
    keys = list(assets.keys())
    sim = Sim(initial_capital, keys)
    fee = cfg["fee_pct"] / 100
    max_exposure_pct = cfg["max_exposure_per_asset_pct"] / 100
    stop_loss = cfg["stop_loss_pct"] / 100
    take_profit = cfg["take_profit_pct"] / 100
    weights = {
        "rsi": cfg["w_rsi"], "macd": cfg["w_macd"], "bollinger": cfg["w_bollinger"],
        "trend": cfg["w_trend"], "volume": cfg["w_volume"], "sentiment": cfg["w_sentiment"],
    }
    last_price: dict[str, float] = {}

    for t in calendar:
        prices = dict(last_price)
        for key in keys:
            info = assets[key]
            i = info["idx_by_date"].get(t)
            if i is not None:
                prices[key] = float(info["df"].iloc[i]["close"])
        last_price.update(prices)

        for key in keys:
            info = assets[key]
            i = info["idx_by_date"].get(t)
            if i is None or i < 1:
                continue
            df = info["df"]
            last, prev = df.iloc[i], df.iloc[i - 1]
            if pd.isna(last.get("rsi")) or pd.isna(prev.get("rsi")):
                continue
            price = float(last["close"])
            pos = sim.positions[key]

            if mode == "score":
                fgv = fng_at(fng_series, t)
                score_result = score_market(
                    df.iloc[: i + 1], rsi_buy=cfg["rsi_buy"], rsi_sell=cfg["rsi_sell"],
                    weights=weights, buy_threshold=cfg["score_buy_threshold"],
                    sell_threshold=cfg["score_sell_threshold"],
                    atr_stop_mult=cfg["atr_stop_mult"], atr_target_mult=cfg["atr_target_mult"],
                    fear_greed_value=fgv, news_sentiment_score=None,
                )
                buy_signal = score_result["action"] == "buy"
                sell_signal_core = score_result["action"] == "sell"
                buy_reason = f"score {score_result['score']:+.2f}"
                sell_reason = f"score {score_result['score']:+.2f}"
            else:
                bullish_cross = prev["sma_fast"] <= prev["sma_slow"] and last["sma_fast"] > last["sma_slow"]
                bearish_cross = prev["sma_fast"] >= prev["sma_slow"] and last["sma_fast"] < last["sma_slow"]
                buy_signal = last["rsi"] < cfg["rsi_buy"] or bullish_cross
                sell_signal_core = last["rsi"] > cfg["rsi_sell"] or bearish_cross
                buy_reason = "RSI survente" if last["rsi"] < cfg["rsi_buy"] else "croisement haussier"
                sell_reason = "RSI surachat" if last["rsi"] > cfg["rsi_sell"] else "croisement baissier"

            if pos["qty"] > 0:
                pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
                stop_hit = pnl_pct <= stop_loss
                tp_hit = pnl_pct >= take_profit
                if sell_signal_core or stop_hit or tp_hit:
                    proceeds = pos["qty"] * price
                    trade_fee = proceeds * fee
                    sim.cash += proceeds - trade_fee
                    reason = "stop-loss" if stop_hit else "take-profit" if tp_hit else sell_reason
                    sim.record(t, key, "SELL", price, pos["qty"], trade_fee, pnl_pct * 100, reason)
                    sim.positions[key] = {"qty": 0.0, "entry_price": None}
                    continue

            total_now = sim.value(prices)
            current_exposure = pos["qty"] * price
            max_allowed = total_now * max_exposure_pct
            room = max_allowed - current_exposure
            if buy_signal and room > 10 and sim.cash > 10:
                invest_amount = min(room, sim.cash * 0.5)
                if invest_amount > 10:
                    trade_fee = invest_amount * fee
                    qty = (invest_amount - trade_fee) / price
                    sim.cash -= invest_amount
                    # Moyenne ponderee du prix de revient si on ajoute a une
                    # position existante (meme fix que run_cycle() dans run_loop.py).
                    if pos["qty"] > 0 and pos["entry_price"] is not None:
                        pos["entry_price"] = (
                            pos["qty"] * pos["entry_price"] + qty * price
                        ) / (pos["qty"] + qty)
                    else:
                        pos["entry_price"] = price
                    pos["qty"] += qty
                    sim.record(t, key, "BUY", price, qty, trade_fee, None, buy_reason)

        sim.equity.append((t, sim.value(prices)))

    return sim


def run_buy_and_hold(cfg: dict, assets: dict, calendar, initial_capital: float) -> Sim:
    keys = list(assets.keys())
    sim = Sim(initial_capital, keys)
    fee = cfg["fee_pct"] / 100
    bought = False
    last_price: dict[str, float] = {}
    for t in calendar:
        for key in keys:
            info = assets[key]
            i = info["idx_by_date"].get(t)
            if i is not None:
                last_price[key] = float(info["df"].iloc[i]["close"])
        if not bought and all(k in last_price for k in keys):
            share = initial_capital / len(keys)
            for key in keys:
                price = last_price[key]
                trade_fee = share * fee
                qty = (share - trade_fee) / price
                sim.cash -= share
                sim.positions[key]["qty"] = qty
                sim.positions[key]["entry_price"] = price
                sim.record(t, key, "BUY", price, qty, trade_fee, None, "buy&hold initial")
            bought = True
        sim.equity.append((t, sim.value(last_price)))
    return sim


def metrics(sim: Sim) -> dict:
    eq = pd.Series({d: v for d, v in sim.equity}).sort_index()
    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    running_max = eq.cummax()
    drawdown = eq / running_max - 1
    max_dd = drawdown.min()
    hourly_returns = eq.pct_change().dropna()
    std = hourly_returns.std()
    sharpe = (hourly_returns.mean() / std * np.sqrt(24 * 365)) if std and not pd.isna(std) else float("nan")
    sells = [tr for tr in sim.trades if tr["action"] == "SELL"]
    wins = [tr for tr in sells if tr["pnl_pct"] and tr["pnl_pct"] > 0]
    win_rate = len(wins) / len(sells) * 100 if sells else float("nan")
    total_fees = sum(tr["fee"] for tr in sim.trades)
    mid = len(eq) // 2
    r1 = (eq.iloc[mid] / eq.iloc[0] - 1) * 100 if mid > 0 else float("nan")
    r2 = (eq.iloc[-1] / eq.iloc[mid] - 1) * 100 if mid > 0 else float("nan")
    return dict(
        final_value=round(float(eq.iloc[-1]), 2),
        total_return_pct=round(float(total_return * 100), 2),
        max_drawdown_pct=round(float(max_dd * 100), 2),
        sharpe_approx=round(float(sharpe), 2) if not pd.isna(sharpe) else None,
        num_trades=len(sim.trades),
        num_round_trips=len(sells),
        win_rate_pct=round(float(win_rate), 1) if not pd.isna(win_rate) else None,
        total_fees=round(float(total_fees), 2),
        first_half_return_pct=round(float(r1), 2) if not pd.isna(r1) else None,
        second_half_return_pct=round(float(r2), 2) if not pd.isna(r2) else None,
    )


def build_cfg(opts: dict) -> dict:
    from indicators import DEFAULT_WEIGHTS
    return dict(
        fee_pct=opts["fee_pct"],
        max_exposure_per_asset_pct=opts["max_exposure_per_asset_pct"],
        rsi_buy=opts["rsi_buy"], rsi_sell=opts["rsi_sell"],
        stop_loss_pct=opts["stop_loss_pct"], take_profit_pct=opts["take_profit_pct"],
        score_buy_threshold=opts.get("score_buy_threshold", 0.35),
        score_sell_threshold=opts.get("score_sell_threshold", -0.35),
        atr_stop_mult=opts.get("atr_stop_mult", 1.5), atr_target_mult=opts.get("atr_target_mult", 3.0),
        w_rsi=opts.get("w_rsi", DEFAULT_WEIGHTS["rsi"]), w_macd=opts.get("w_macd", DEFAULT_WEIGHTS["macd"]),
        w_bollinger=opts.get("w_bollinger", DEFAULT_WEIGHTS["bollinger"]),
        w_trend=opts.get("w_trend", DEFAULT_WEIGHTS["trend"]),
        w_volume=opts.get("w_volume", DEFAULT_WEIGHTS["volume"]),
        w_sentiment=opts.get("w_sentiment", DEFAULT_WEIGHTS["sentiment"]),
        sma_fast=opts["sma_fast"], sma_slow=opts["sma_slow"], rsi_period=opts["rsi_period"],
    )


def run_backtest(opts: dict, days: int = 365, use_fng: bool = True) -> dict:
    """Backteste la config live (opts) sur des donnees Kraken reelles, pour
    les actifs actuellement suivis (opts['symbols']). Bloquant (peut prendre
    1-3 minutes selon le nombre d'actifs/jours) -- a lancer via le helper HA
    dedie, pas a chaque cycle. Retourne un resume JSON-serialisable (pas les
    trades complets, trop volumineux pour un attribut HA) et ecrit les
    courbes d'equity + journaux de trades complets dans /share/crypto-backtest/."""
    cfg = build_cfg(opts)
    keys = [s["entity_key"] for s in opts["symbols"]]
    exchange = getattr(ccxt, opts["exchange"])({"enableRateLimit": True})
    since_ms = exchange.milliseconds() - days * 24 * 3600 * 1000

    assets = {}
    for symbol_cfg in opts["symbols"]:
        key, symbol = symbol_cfg["entity_key"], symbol_cfg["symbol"]
        raw = fetch_history(exchange, symbol, opts["timeframe"], since_ms)
        df = compute_all(raw, fast=cfg["sma_fast"], slow=cfg["sma_slow"], rsi_period=cfg["rsi_period"])
        assets[key] = {"df": df, "idx_by_date": {d: i for i, d in enumerate(df["date"])}}

    fng_series = fetch_fng_history() if use_fng else None

    from functools import reduce
    calendar = reduce(
        lambda a, b: a.union(b), (pd.DatetimeIndex(a["df"]["date"]) for a in assets.values())
    ).sort_values()

    initial_capital = opts["initial_capital"]
    sim_legacy = run_strategy("legacy", cfg, assets, calendar, initial_capital, fng_series)
    sim_score = run_strategy("score", cfg, assets, calendar, initial_capital, fng_series)
    sim_bh = run_buy_and_hold(cfg, assets, calendar, initial_capital)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    for name, sim in (("legacy", sim_legacy), ("score", sim_score), ("buy_hold", sim_bh)):
        pd.DataFrame(sim.equity, columns=["date", "value"]).to_csv(OUTDIR / f"equity_{name}.csv", index=False)
        pd.DataFrame(sim.trades).to_csv(OUTDIR / f"trades_{name}.csv", index=False)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "timeframe": opts["timeframe"],
        "symbols": keys,
        "fear_greed_included": fng_series is not None,
        "news_sentiment_included": False,  # jamais backtestable, cf. docstring module
        "strategies": {
            "legacy": metrics(sim_legacy),
            "score": metrics(sim_score),
            "buy_hold": metrics(sim_bh),
        },
    }
