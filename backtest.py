"""Simulation d'achat/vente moderee-risque sur BTC/ETH, frais type Kraken.

Regles (risque modere, pas de levier, pas de vente a decouvert) :
- Capital initial : 1000 EUR, reparti dynamiquement entre BTC, ETH et cash.
- Exposition max par actif : 40% du capital total (diversification, pas de
  mise "all-in" sur une seule crypto).
- Achat quand : RSI < 35 (survente) OU croisement haussier SMA5/SMA10,
  ET qu'on a moins de 40% deja investi sur cet actif.
- Vente (sortie complete de la position) quand : RSI > 65 (surachat) OU
  croisement baissier SMA5/SMA10 OU stop-loss -7% OU take-profit +12%.
- Frais : 0.26% par ordre (taker Kraken), appliques a l'achat ET a la vente.
"""
from __future__ import annotations
from io import StringIO
import pandas as pd
import numpy as np

FEE = 0.0026  # 0.26% par ordre, taker Kraken
MAX_EXPOSURE_PER_ASSET = 0.40
RSI_BUY, RSI_SELL = 35, 65
STOP_LOSS, TAKE_PROFIT = -0.07, 0.12
SMA_FAST, SMA_SLOW, RSI_PERIOD = 5, 10, 14

btc_csv = """2026-08-13,63505.0
2026-08-14,63062.0
2026-08-15,63114.0
2026-08-16,62921.0
2026-08-17,64567.0
2026-08-18,64770.0
2026-08-19,69374.0
2026-08-20,73063.0
2026-08-21,78369.0
2026-08-22,77147.0
2026-08-23,77729.0
2026-08-24,79012.0
2026-08-25,78523.0
2026-08-26,79028.0
2026-08-27,80256.0
2026-08-28,77837.0
2026-08-29,78237.0
2026-08-30,77713.0
2026-08-31,78629.0
2026-09-01,77476.0
2026-09-02,77421.0
2026-09-03,81267.0
2026-09-04,79747.0
2026-09-05,79898.0
2026-09-06,80450.0
2026-09-07,79178.0
2026-09-08,78505.0
2026-09-09,78392.0
2026-09-10,76627.0
2026-09-11,77175.0
2026-09-12,77229.0
2026-09-13,77247.0"""

eth_csv = """2026-08-12,1879.80
2026-08-13,1886.15
2026-08-14,1882.21
2026-08-15,1882.69
2026-08-16,1876.01
2026-08-17,1913.59
2026-08-18,1917.49
2026-08-19,2253.37
2026-08-20,2327.74
2026-08-21,2515.45
2026-08-22,2423.61
2026-08-23,2462.67
2026-08-24,2482.86
2026-08-25,2442.63
2026-08-26,2506.89
2026-08-27,2510.91
2026-08-28,2442.70
2026-08-29,2457.19
2026-08-30,2418.59
2026-08-31,2467.65
2026-09-01,2418.58
2026-09-02,2393.39
2026-09-03,2507.79
2026-09-04,2455.73
2026-09-05,2480.33
2026-09-06,2515.16
2026-09-07,2490.01
2026-09-08,2485.06
2026-09-09,2468.45
2026-09-10,2437.70
2026-09-11,2516.37
2026-09-12,2513.58"""


def load(csv_text):
    df = pd.read_csv(StringIO(csv_text), header=None, names=["date", "close"])
    df["date"] = pd.to_datetime(df["date"])
    return df


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def prep(df):
    df = df.copy()
    df["sma_fast"] = df["close"].rolling(SMA_FAST).mean()
    df["sma_slow"] = df["close"].rolling(SMA_SLOW).mean()
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    return df


btc = prep(load(btc_csv))
eth = prep(load(eth_csv))

# Aligner sur les dates communes aux deux actifs
dates = sorted(set(btc["date"]) & set(eth["date"]))
btc = btc.set_index("date").loc[dates]
eth = eth.set_index("date").loc[dates]

assets = {"BTC": btc, "ETH": eth}

cash = 1000.0
positions = {k: {"qty": 0.0, "entry_price": None, "invested": 0.0} for k in assets}
initial_capital = cash
log = []
equity_curve = []

for i, date in enumerate(dates):
    if i < max(SMA_SLOW, RSI_PERIOD):
        # pas assez d'historique pour des indicateurs fiables
        total = cash + sum(positions[a]["qty"] * assets[a].loc[date, "close"] for a in assets)
        equity_curve.append((date, total))
        continue

    for name, df in assets.items():
        row = df.loc[date]
        prev = df.iloc[df.index.get_loc(date) - 1]
        price = row["close"]
        pos = positions[name]

        bullish_cross = prev["sma_fast"] <= prev["sma_slow"] and row["sma_fast"] > row["sma_slow"]
        bearish_cross = prev["sma_fast"] >= prev["sma_slow"] and row["sma_fast"] < row["sma_slow"]

        # --- Sortie de position ---
        if pos["qty"] > 0:
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
            sell_signal = row["rsi"] > RSI_SELL or bearish_cross
            stop_hit = pnl_pct <= STOP_LOSS
            tp_hit = pnl_pct >= TAKE_PROFIT

            if sell_signal or stop_hit or tp_hit:
                proceeds = pos["qty"] * price
                fee = proceeds * FEE
                cash += proceeds - fee
                reason = "stop-loss" if stop_hit else ("take-profit" if tp_hit else ("RSI surachat" if row["rsi"] > RSI_SELL else "croisement baissier"))
                log.append(dict(date=date, asset=name, action="SELL", price=round(price, 2),
                                 qty=round(pos["qty"], 6), fee=round(fee, 2),
                                 pnl_pct=round(pnl_pct * 100, 2), reason=reason))
                positions[name] = {"qty": 0.0, "entry_price": None, "invested": 0.0}
                continue  # une seule action par actif/jour

        # --- Entree en position ---
        total_portfolio = cash + sum(positions[a]["qty"] * assets[a].loc[date, "close"] for a in assets)
        current_exposure = pos["qty"] * price
        max_allowed = total_portfolio * MAX_EXPOSURE_PER_ASSET
        room = max_allowed - current_exposure

        buy_signal = row["rsi"] < RSI_BUY or bullish_cross
        if buy_signal and room > 10 and cash > 10:
            invest_amount = min(room, cash * 0.5)  # jamais tout le cash d'un coup
            if invest_amount > 10:
                fee = invest_amount * FEE
                qty = (invest_amount - fee) / price
                cash -= invest_amount
                positions[name]["qty"] += qty
                positions[name]["entry_price"] = price
                positions[name]["invested"] += invest_amount
                reason = "RSI survente" if row["rsi"] < RSI_BUY else "croisement haussier"
                log.append(dict(date=date, asset=name, action="BUY", price=round(price, 2),
                                 qty=round(qty, 6), fee=round(fee, 2), pnl_pct=None, reason=reason))

    total = cash + sum(positions[a]["qty"] * assets[a].loc[date, "close"] for a in assets)
    equity_curve.append((date, total))

final_value = equity_curve[-1][1]
total_return = (final_value / initial_capital - 1) * 100

eq_df = pd.DataFrame(equity_curve, columns=["date", "value"])
eq_df["peak"] = eq_df["value"].cummax()
eq_df["drawdown_pct"] = (eq_df["value"] / eq_df["peak"] - 1) * 100
max_dd = eq_df["drawdown_pct"].min()

print("=== Journal des transactions ===")
for l in log:
    print(l)

print()
print("=== Resultat ===")
print(f"Capital initial      : {initial_capital:.2f} EUR")
print(f"Valeur finale         : {final_value:.2f} EUR")
print(f"Performance           : {total_return:+.2f} %")
print(f"Max drawdown          : {max_dd:.2f} %")
print(f"Nb trades             : {len(log)} ({sum(1 for l in log if l['action']=='BUY')} achats / {sum(1 for l in log if l['action']=='SELL')} ventes)")
print(f"Frais totaux payes    : {sum(l['fee'] for l in log):.2f} EUR")

# comparaison "buy and hold" 50/50 pour contexte
half = initial_capital / 2
btc_qty0 = (half * (1 - FEE)) / btc.iloc[max(SMA_SLOW, RSI_PERIOD)]["close"]
eth_qty0 = (half * (1 - FEE)) / eth.iloc[max(SMA_SLOW, RSI_PERIOD)]["close"]
bh_final = btc_qty0 * btc.iloc[-1]["close"] + eth_qty0 * eth.iloc[-1]["close"]
print(f"\nComparatif buy&hold 50/50 (achat unique J0, sans rebalancement): {bh_final:.2f} EUR ({(bh_final/initial_capital-1)*100:+.2f} %)")
