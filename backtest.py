#!/usr/bin/env python3
"""
backtest.py - Backtest sur donnees historiques REELLES (Kraken, via ccxt),
en reutilisant le code exact du moteur de decision de crypto_paper_trader
(indicators.py: compute_all/score_market, et la logique de run_cycle() dans
run_loop.py, reimplementee ici a l'identique : meme frais, meme exposition
max, meme stop-loss/take-profit, meme ordre de traitement des actifs).

A LANCER SUR UNE MACHINE AVEC ACCES INTERNET COMPLET (ton PC, par exemple --
pas depuis un environnement sandboxe dont le reseau sortant est restreint
aux registres de paquets, comme un Claude Code cloud sandbox).

Installation :
    pip install ccxt pandas requests matplotlib

Usage basique (comparaison legacy vs score engine vs buy&hold, 18 mois,
BTC/ETH/XRP, capital/fees/seuils = ceux actuellement dans config.yaml) :

    python backtest.py

Options utiles :
    --days 730                  Profondeur d'historique (defaut 540 ~ 18 mois)
    --timeframe 1h               Meme valeur que le "timeframe" de l'add-on
    --symbols btc,eth,xrp         Sous-ensemble d'actifs a tester
    --initial-capital 1000
    --no-fng                     Exclut l'indice Fear & Greed du score engine
    --no-plot                    Ne genere pas le PNG (utile sans matplotlib)
    --outdir backtest_results

Ce que ca teste et ce que ca ne teste PAS :
  - RSI, MACD, Bollinger, ADX/tendance, volume : testes fidelement, avec le
    VRAI code de indicators.py (import direct, pas de reimplementation) sur
    de vraies bougies Kraken -> aucun risque de divergence avec la prod.
  - Fear & Greed (indice "sentiment") : inclus si --no-fng n'est pas passe,
    via l'historique complet de alternative.me (gratuit, sans cle).
  - news_sentiment (LLM local sur les titres RSS/CryptoPanic) : NON
    testable ici. Il n'existe pas d'archive historique de "quel titre RSS
    est apparu a quelle heure passee" -- ce signal ne peut etre evalue
    qu'en conditions reelles (live), pas en backtest. Le score engine
    backteste ici utilise donc 5 ou 6 indicateurs sur les 7 deployes ; la
    performance reelle peut differer dans la mesure ou les news comptent.

Pas d'optimisation automatique des poids/seuils ici (volontairement, pour
eviter l'overfitting) : ce script COMPARE la config actuellement deployee
(legacy, puisque use_score_engine=false) au score engine tel que configure
dans config.yaml, sur des donnees reelles. Si les resultats donnent envie
d'ajuster des poids/seuils, refaire tourner ce meme script ensuite sur une
periode differente (ou sur la portion "out-of-sample" affichee) avant de
faire confiance au reglage -- ne jamais juger un reglage sur les memes
donnees qui ont servi a le choisir.
"""
from __future__ import annotations

import argparse
import sys
import time
from functools import reduce
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent / "crypto_paper_trader"))
from indicators import DEFAULT_WEIGHTS, compute_all, score_market  # noqa: E402

# ---------------------------------------------------------------------------
# Valeurs par defaut = copie de crypto_paper_trader/config.yaml (options),
# pour que le backtest teste exactement ce qui est deploye.
# ---------------------------------------------------------------------------
CFG = dict(
    exchange="kraken",
    fee_pct=0.26,
    max_exposure_per_asset_pct=40,
    rsi_period=14,
    sma_fast=5,
    sma_slow=10,
    rsi_buy=35,
    rsi_sell=65,
    stop_loss_pct=-7,
    take_profit_pct=12,
    score_buy_threshold=0.35,
    score_sell_threshold=-0.35,
    atr_stop_mult=1.5,
    atr_target_mult=3.0,
    w_rsi=1.0, w_macd=1.0, w_bollinger=0.7, w_trend=0.8, w_volume=0.5, w_sentiment=0.4,
)

ALL_SYMBOLS = {
    "btc": "BTC/EUR",
    "eth": "ETH/EUR",
    "xrp": "XRP/EUR",
}


# ---------------------------------------------------------------------------
# Donnees
# ---------------------------------------------------------------------------

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
    # meme logique que fetch_ohlcv() dans run_loop.py : on exclut la bougie
    # en cours de formation.
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
        print(f"[!] Historique Fear & Greed indisponible ({exc}) -- indicateur 'sentiment' exclu.", file=sys.stderr)
        return None
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["timestamp"].astype(int), unit="s", utc=True)
    df["value"] = df["value"].astype(float)
    series = df.set_index("date")["value"].sort_index()
    return series


def fng_at(fng_series: pd.Series | None, ts: pd.Timestamp) -> float | None:
    if fng_series is None:
        return None
    idx = fng_series.index.asof(ts)
    if pd.isna(idx):
        return None
    return float(fng_series.loc[idx])


# ---------------------------------------------------------------------------
# Simulation (reimplementation fidele de run_cycle() dans run_loop.py, sans
# la partie Home Assistant / news, sur donnees historiques)
# ---------------------------------------------------------------------------

class Sim:
    def __init__(self, initial_capital: float, keys: list[str]):
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions = {k: {"qty": 0.0, "entry_price": None} for k in keys}
        self.trades: list[dict] = []
        self.equity: list[tuple] = []  # (date, value)

    def value(self, prices: dict[str, float]) -> float:
        v = self.cash
        for k, pos in self.positions.items():
            v += pos["qty"] * prices.get(k, 0.0)
        return v

    def record(self, date, asset, action, price, qty, fee, pnl_pct, reason):
        self.trades.append(
            dict(date=date, asset=asset, action=action, price=price, qty=qty,
                 fee=fee, pnl_pct=pnl_pct, reason=reason)
        )


def run_strategy(mode: str, assets: dict[str, dict], calendar: pd.DatetimeIndex,
                  initial_capital: float, fng_series: pd.Series | None) -> Sim:
    """mode: 'legacy' ou 'score'. assets[key] = {"df": df_complet, "idx_by_date": dict}"""
    keys = list(assets.keys())
    sim = Sim(initial_capital, keys)
    fee = CFG["fee_pct"] / 100
    max_exposure_pct = CFG["max_exposure_per_asset_pct"] / 100
    stop_loss = CFG["stop_loss_pct"] / 100
    take_profit = CFG["take_profit_pct"] / 100
    weights = {
        "rsi": CFG["w_rsi"], "macd": CFG["w_macd"], "bollinger": CFG["w_bollinger"],
        "trend": CFG["w_trend"], "volume": CFG["w_volume"], "sentiment": CFG["w_sentiment"],
    }

    last_price: dict[str, float] = {}

    for t in calendar:
        prices = dict(last_price)
        # met a jour les prix des actifs qui ont une bougie a cet instant
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
                continue  # pas de bougie cloturee a cet instant pour cet actif
            df = info["df"]
            last, prev = df.iloc[i], df.iloc[i - 1]
            if pd.isna(last.get("rsi")) or pd.isna(prev.get("rsi")):
                continue  # periode de warmup des indicateurs
            price = float(last["close"])
            pos = sim.positions[key]

            if mode == "score":
                fgv = fng_at(fng_series, t)
                score_result = score_market(
                    df.iloc[: i + 1], rsi_buy=CFG["rsi_buy"], rsi_sell=CFG["rsi_sell"],
                    weights=weights, buy_threshold=CFG["score_buy_threshold"],
                    sell_threshold=CFG["score_sell_threshold"],
                    atr_stop_mult=CFG["atr_stop_mult"], atr_target_mult=CFG["atr_target_mult"],
                    fear_greed_value=fgv, news_sentiment_score=None,
                )
                buy_signal = score_result["action"] == "buy"
                sell_signal_core = score_result["action"] == "sell"
                buy_reason = f"score {score_result['score']:+.2f}"
                sell_reason = f"score {score_result['score']:+.2f}"
            else:
                bullish_cross = prev["sma_fast"] <= prev["sma_slow"] and last["sma_fast"] > last["sma_slow"]
                bearish_cross = prev["sma_fast"] >= prev["sma_slow"] and last["sma_fast"] < last["sma_slow"]
                buy_signal = last["rsi"] < CFG["rsi_buy"] or bullish_cross
                sell_signal_core = last["rsi"] > CFG["rsi_sell"] or bearish_cross
                buy_reason = "RSI survente" if last["rsi"] < CFG["rsi_buy"] else "croisement haussier"
                sell_reason = "RSI surachat" if last["rsi"] > CFG["rsi_sell"] else "croisement baissier"

            # --- Sortie ---
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

            # --- Entree ---
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
                    pos["qty"] += qty
                    pos["entry_price"] = price
                    sim.record(t, key, "BUY", price, qty, trade_fee, None, buy_reason)

        sim.equity.append((t, sim.value(prices)))

    return sim


def run_buy_and_hold(assets: dict[str, dict], calendar: pd.DatetimeIndex, initial_capital: float) -> Sim:
    keys = list(assets.keys())
    sim = Sim(initial_capital, keys)
    fee = CFG["fee_pct"] / 100
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


# ---------------------------------------------------------------------------
# Metriques
# ---------------------------------------------------------------------------

def metrics(sim: Sim) -> dict:
    eq = pd.Series({d: v for d, v in sim.equity}).sort_index()
    total_return = eq.iloc[-1] / eq.iloc[0] - 1
    running_max = eq.cummax()
    drawdown = eq / running_max - 1
    max_dd = drawdown.min()
    hourly_returns = eq.pct_change().dropna()
    sharpe = (
        hourly_returns.mean() / hourly_returns.std() * np.sqrt(24 * 365)
        if hourly_returns.std() not in (0, None) and not pd.isna(hourly_returns.std())
        else float("nan")
    )
    sells = [tr for tr in sim.trades if tr["action"] == "SELL"]
    wins = [tr for tr in sells if tr["pnl_pct"] and tr["pnl_pct"] > 0]
    win_rate = len(wins) / len(sells) * 100 if sells else float("nan")
    total_fees = sum(tr["fee"] for tr in sim.trades)
    return dict(
        final_value=eq.iloc[-1],
        total_return_pct=total_return * 100,
        max_drawdown_pct=max_dd * 100,
        sharpe_approx=sharpe,
        num_trades=len(sim.trades),
        num_round_trips=len(sells),
        win_rate_pct=win_rate,
        total_fees=total_fees,
    )


def print_report(name: str, m: dict, initial_capital: float) -> None:
    print(f"\n--- {name} ---")
    print(f"  Valeur finale        : {m['final_value']:.2f} EUR (depart {initial_capital:.2f})")
    print(f"  Rendement total      : {m['total_return_pct']:+.2f} %")
    print(f"  Drawdown max         : {m['max_drawdown_pct']:.2f} %")
    print(f"  Sharpe (approx.)     : {m['sharpe_approx']:.2f}")
    print(f"  Trades               : {m['num_trades']} (dont {m['num_round_trips']} ventes)")
    print(f"  Win rate             : {m['win_rate_pct']:.1f} %")
    print(f"  Frais payes cumules  : {m['total_fees']:.2f} EUR")


def print_period_split(sim: Sim, label: str) -> None:
    """Rendement 1ere moitie / 2eme moitie de la periode testee, pour
    verifier que la performance n'est pas due a un seul gros mouvement."""
    eq = pd.Series({d: v for d, v in sim.equity}).sort_index()
    mid = len(eq) // 2
    first_half = eq.iloc[: mid + 1]
    second_half = eq.iloc[mid:]
    r1 = (first_half.iloc[-1] / first_half.iloc[0] - 1) * 100
    r2 = (second_half.iloc[-1] / second_half.iloc[0] - 1) * 100
    print(f"  [{label}] 1ere moitie periode : {r1:+.2f} %  |  2eme moitie (plus recente / out-of-sample) : {r2:+.2f} %")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=540, help="Profondeur d'historique en jours (defaut 540)")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--symbols", default="btc,eth,xrp", help="Sous-ensemble parmi btc,eth,xrp")
    parser.add_argument("--initial-capital", type=float, default=1000.0)
    parser.add_argument("--no-fng", action="store_true", help="Exclut Fear & Greed du score engine")
    parser.add_argument("--no-plot", action="store_true")
    parser.add_argument("--outdir", default="backtest_results")
    args = parser.parse_args()

    keys = [k.strip().lower() for k in args.symbols.split(",") if k.strip()]
    for k in keys:
        if k not in ALL_SYMBOLS:
            parser.error(f"Actif inconnu: {k} (choix possibles: {', '.join(ALL_SYMBOLS)})")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    exchange = ccxt.kraken({"enableRateLimit": True})
    since_ms = exchange.milliseconds() - args.days * 24 * 3600 * 1000

    print(f"Telechargement de {args.days} jours de bougies {args.timeframe} depuis Kraken pour {', '.join(keys)}...")
    assets: dict[str, dict] = {}
    for key in keys:
        symbol = ALL_SYMBOLS[key]
        print(f"  - {symbol}...", end=" ", flush=True)
        raw = fetch_history(exchange, symbol, args.timeframe, since_ms)
        df = compute_all(raw, fast=CFG["sma_fast"], slow=CFG["sma_slow"], rsi_period=CFG["rsi_period"])
        idx_by_date = {d: i for i, d in enumerate(df["date"])}
        assets[key] = {"df": df, "idx_by_date": idx_by_date}
        print(f"{len(df)} bougies ({df['date'].iloc[0]} -> {df['date'].iloc[-1]})")

    fng_series = None if args.no_fng else fetch_fng_history()

    # calendrier commun = union triee des dates de toutes les bougies
    calendar = reduce(
        lambda a, b: a.union(b), (pd.DatetimeIndex(a["df"]["date"]) for a in assets.values())
    ).sort_values()

    print("\nSimulation en cours (legacy, score engine, buy&hold)...")
    sim_legacy = run_strategy("legacy", assets, calendar, args.initial_capital, fng_series)
    sim_score = run_strategy("score", assets, calendar, args.initial_capital, fng_series)
    sim_bh = run_buy_and_hold(assets, calendar, args.initial_capital)

    m_legacy = metrics(sim_legacy)
    m_score = metrics(sim_score)
    m_bh = metrics(sim_bh)

    print("\n" + "=" * 60)
    print(f"RESULTATS -- {args.days} jours, {args.timeframe}, actifs: {', '.join(keys)}")
    print("=" * 60)
    print_report("Legacy (RSI + croisement SMA) -- mode ACTUELLEMENT DEPLOYE", m_legacy, args.initial_capital)
    print_report("Score engine multi-indicateurs (use_score_engine=true)", m_score, args.initial_capital)
    print_report("Buy & hold (reference)", m_bh, args.initial_capital)

    print("\nRobustesse (1ere moitie vs 2eme moitie de la periode testee) :")
    print_period_split(sim_legacy, "legacy")
    print_period_split(sim_score, "score ")
    print_period_split(sim_bh, "b&h   ")

    # export CSV des courbes et des trades
    for name, sim in (("legacy", sim_legacy), ("score", sim_score), ("buy_hold", sim_bh)):
        pd.DataFrame(sim.equity, columns=["date", "value"]).to_csv(outdir / f"equity_{name}.csv", index=False)
        pd.DataFrame(sim.trades).to_csv(outdir / f"trades_{name}.csv", index=False)
    print(f"\nCourbes et journaux de trades exportes dans {outdir}/")

    if not args.no_plot:
        try:
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(11, 6))
            for name, sim in (("Legacy (deploye)", sim_legacy), ("Score engine", sim_score), ("Buy & hold", sim_bh)):
                eq = pd.Series({d: v for d, v in sim.equity}).sort_index()
                ax.plot(eq.index, eq.values, label=name)
            ax.axhline(args.initial_capital, color="grey", linestyle="--", linewidth=0.8, label="Capital initial")
            ax.set_title(f"Backtest {args.days}j ({args.timeframe}) -- {', '.join(keys)}")
            ax.set_ylabel("Valeur portefeuille (EUR)")
            ax.legend()
            fig.tight_layout()
            out_png = outdir / "equity_curve.png"
            fig.savefig(out_png, dpi=130)
            print(f"Graphique sauvegarde dans {out_png}")
        except ImportError:
            print("[!] matplotlib non installe -- graphique non genere (pip install matplotlib, ou --no-plot)")


if __name__ == "__main__":
    main()
