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

from indicators import DEFAULT_WEIGHTS, compute_all
from strategy import DEFAULTS, add_trend_column, apply_step, compute_signals, empty_position

log = logging.getLogger("crypto-paper-trader")

OUTDIR = Path("/share/crypto-backtest")


def fetch_history(exchange, symbol: str, timeframe: str, since_ms: int, limit: int = 1000) -> pd.DataFrame:
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


def load_history(opts: dict, symbol: str, timeframe: str, since_ms: int) -> tuple[pd.DataFrame, str]:
    """Historique de bougies pour le backtest.

    L'API OHLC de Kraken ne renvoie que les 720 dernieres bougies quel que
    soit `since` (30 jours en 1h, 120 jours en 4h) : un backtest "365 jours"
    sur Kraken en 1h ne couvrait donc en realite qu'un mois. On prend
    l'historique sur `history_exchange` (Binance par defaut, historique
    complet pagine) ; si la paire EUR n'y existe pas, on essaie la paire USDT
    du meme actif (rendements quasi identiques, a la variation EUR/USD pres),
    sinon on retombe sur l'exchange de trading, avec un avertissement si la
    periode obtenue est plus courte que demandee."""
    base, quote = symbol.split("/")
    hist_id = opts.get("history_exchange") or "binance"
    candidates = [(hist_id, symbol)]
    if quote != "USDT":
        candidates.append((hist_id, f"{base}/USDT"))
    candidates.append((opts["exchange"], symbol))

    last_exc: Exception | None = None
    for ex_id, sym in candidates:
        try:
            exchange = getattr(ccxt, ex_id)({"enableRateLimit": True})
            if sym not in exchange.load_markets():
                continue
            df = fetch_history(exchange, sym, timeframe, since_ms)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Historique %s indisponible sur %s (%s)", sym, ex_id, exc)
            continue
        source = f"{ex_id}:{sym}"
        covered_days = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 86_400_000
        wanted_days = (exchange.milliseconds() - since_ms) / 86_400_000
        if covered_days < wanted_days * 0.9:
            log.warning("Backtest %s : seulement %.0f jours d'historique sur %.0f demandes (source %s)",
                        symbol, covered_days, wanted_days, source)
        return df, source
    raise RuntimeError(f"Aucun historique trouve pour {symbol}") from last_exc


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
        self.positions = {k: empty_position() for k in keys}
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
                  fng_series: pd.Series | None, tf_ms: int) -> Sim:
    """Rejoue la strategie bougie par bougie avec les memes regles que le live
    (strategy.apply_step). mode = "score" (score engine) ou "legacy"."""
    keys = list(assets.keys())
    sim = Sim(initial_capital, keys)
    book = {"cash": sim.cash, "positions": sim.positions}
    last_price: dict[str, float] = {}

    for t in calendar:
        for key in keys:
            i = assets[key]["idx_by_date"].get(t)
            if i is not None:
                last_price[key] = float(assets[key]["df"].iloc[i]["close"])

        for key in keys:
            info = assets[key]
            i = info["idx_by_date"].get(t)
            if i is None or i < 1:
                continue
            df = info["df"]
            if pd.isna(df.iloc[i].get("rsi")) or pd.isna(df.iloc[i - 1].get("rsi")):
                continue
            signals = compute_signals(mode == "score", df.iloc[i - 1: i + 1], cfg,
                                      fear_greed_value=fng_at(fng_series, t))
            ts = int(df.iloc[i]["ts"])
            apply_step(book, key, last_price[key], ts, tf_ms, signals, cfg, last_price,
                       lambda action, price, qty, fee, pnl, reason, key=key:
                       sim.record(t, key, action, price, qty, fee, pnl, reason))

        sim.cash = book["cash"]
        sim.equity.append((t, sim.value(last_price)))

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


def metrics(sim: Sim, bars_per_year: float) -> dict:
    eq = pd.Series({d: v for d, v in sim.equity}).sort_index()
    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    running_max = eq.cummax()
    drawdown = eq / running_max - 1
    max_dd = drawdown.min()
    bar_returns = eq.pct_change().dropna()
    std = bar_returns.std()
    sharpe = (bar_returns.mean() / std * np.sqrt(bars_per_year)) if std and not pd.isna(std) else float("nan")
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
    return dict(
        fee_pct=opts["fee_pct"],
        max_exposure_per_asset_pct=opts["max_exposure_per_asset_pct"],
        max_total_exposure_pct=opts.get("max_total_exposure_pct", DEFAULTS["max_total_exposure_pct"]),
        min_hold_bars=opts.get("min_hold_bars", DEFAULTS["min_hold_bars"]),
        cooldown_bars=opts.get("cooldown_bars", DEFAULTS["cooldown_bars"]),
        allow_pyramiding=opts.get("allow_pyramiding", DEFAULTS["allow_pyramiding"]),
        trend_filter_days=opts.get("trend_filter_days", DEFAULTS["trend_filter_days"]),
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
        w_news_sentiment=opts.get("w_news_sentiment", DEFAULT_WEIGHTS["news_sentiment"]),
        sma_fast=opts["sma_fast"], sma_slow=opts["sma_slow"], rsi_period=opts["rsi_period"],
    )


def load_assets(opts: dict, days: int) -> tuple[dict, dict[str, str], int]:
    """Telecharge l'historique de chaque actif suivi et calcule les indicateurs.
    Retourne (assets, source par actif, duree d'une bougie en ms)."""
    cfg = build_cfg(opts)
    timeframe = opts["timeframe"]
    tf_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
    since_ms = int(time.time() * 1000) - days * 24 * 3600 * 1000
    assets, sources = {}, {}
    for symbol_cfg in opts["symbols"]:
        key, symbol = symbol_cfg["entity_key"], symbol_cfg["symbol"]
        raw, sources[key] = load_history(opts, symbol, timeframe, since_ms)
        df = compute_all(raw, fast=cfg["sma_fast"], slow=cfg["sma_slow"], rsi_period=cfg["rsi_period"])
        df = add_trend_column(df, cfg, tf_ms)
        assets[key] ={"df": df, "idx_by_date": {d: i for i, d in enumerate(df["date"])}}
    return assets, sources, tf_ms


def make_calendar(assets: dict) -> pd.DatetimeIndex:
    from functools import reduce
    return reduce(
        lambda a, b: a.union(b), (pd.DatetimeIndex(a["df"]["date"]) for a in assets.values())
    ).sort_values()


def run_backtest(opts: dict, days: int = 365, use_fng: bool = True) -> dict:
    """Backteste la config live (opts) sur des donnees reelles, pour les
    actifs actuellement suivis (opts['symbols']), avec les memes regles que
    le live (strategy.py). Bloquant (quelques minutes selon le nombre
    d'actifs/jours) -- a lancer via le helper HA dedie, pas a chaque cycle.
    Retourne un resume JSON-serialisable et ecrit les courbes d'equity +
    journaux de trades complets dans /share/crypto-backtest/."""
    cfg = build_cfg(opts)
    keys = [s["entity_key"] for s in opts["symbols"]]
    assets, sources, tf_ms = load_assets(opts, days)
    fng_series = fetch_fng_history() if use_fng else None
    calendar = make_calendar(assets)
    bars_per_year = 365 * 86_400_000 / tf_ms

    initial_capital = opts["initial_capital"]
    sim_legacy = run_strategy("legacy", cfg, assets, calendar, initial_capital, fng_series, tf_ms)
    sim_score = run_strategy("score", cfg, assets, calendar, initial_capital, fng_series, tf_ms)
    sim_bh = run_buy_and_hold(cfg, assets, calendar, initial_capital)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    for name, sim in (("legacy", sim_legacy), ("score", sim_score), ("buy_hold", sim_bh)):
        pd.DataFrame(sim.equity, columns=["date", "value"]).to_csv(OUTDIR / f"equity_{name}.csv", index=False)
        pd.DataFrame(sim.trades).to_csv(OUTDIR / f"trades_{name}.csv", index=False)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "covered_from": calendar[0].isoformat() if len(calendar) else None,
        "covered_to": calendar[-1].isoformat() if len(calendar) else None,
        "timeframe": opts["timeframe"],
        "symbols": keys,
        "data_sources": sources,
        "fear_greed_included": fng_series is not None,
        "news_sentiment_included": False,  # jamais backtestable, cf. docstring module
        "strategies": {
            "legacy": metrics(sim_legacy, bars_per_year),
            "score": metrics(sim_score, bars_per_year),
            "buy_hold": metrics(sim_bh, bars_per_year),
        },
    }
