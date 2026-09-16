#!/usr/bin/env python3
"""Simulation d'achat/vente en continu, capital fictif, prix live.

Meme logique que le backtest (voir backtest.py dans le repo) : RSI + croisement
SMA pour entrer/sortir, exposition max par actif, stop-loss/take-profit, frais
type Kraken. Aucun ordre reel n'est jamais passe -- portefeuille virtuel.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import ccxt
import pandas as pd

from ha_client import HomeAssistantClient
from indicators import DEFAULT_WEIGHTS, compute_all, score_market
from market_news import ASSET_KEYWORDS, fetch_cryptopanic_news, fetch_rss_news
from market_sentiment import fetch_fear_greed
from news_sentiment_model import get_news_sentiment_score
from portfolio import load_state, portfolio_value, record_trade, reset_state, save_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("crypto-paper-trader")

OPTIONS_PATH = Path("/data/options.json")
STATE_PATH = Path("/data/portfolio.json")


def load_options() -> dict:
    with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_ohlcv(exchange_id: str, symbol: str, timeframe: str, limit: int = 60) -> pd.DataFrame:
    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    # +1 pour compenser la bougie en cours qu'on va retirer juste apres,
    # sans perdre de profondeur d'historique pour les indicateurs.
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 1)
    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")

    # La derniere bougie retournee par l'exchange est generalement celle en
    # cours de formation (pas encore cloturee). La garder fait "flip-flopper"
    # le RSI/croisement SMA a chaque cycle tant que le prix bouge dans la
    # bougie courante, meme sans nouvelle bougie -> sur-trading + frais qui
    # mangent tout gain. On ne garde que les bougies deja cloturees.
    if len(df):
        try:
            tf_ms = exchange.parse_timeframe(timeframe) * 1000
            now_ms = exchange.milliseconds()
            if df.iloc[-1]["ts"] + tf_ms > now_ms:
                df = df.iloc[:-1].reset_index(drop=True)
        except Exception:
            # timeframe invalide/vide cote config : on garde toutes les
            # bougies plutot que de faire planter le cycle. A corriger dans
            # la config de l'add-on (champ "timeframe", ex: "1h").
            log.warning(
                "Impossible d'exclure la bougie en cours (timeframe=%r invalide) - "
                "verifie le champ 'timeframe' dans la config de l'add-on",
                timeframe,
            )
    return df


def run_cycle(opts: dict, state: dict, ha: HomeAssistantClient) -> dict:
    fee = opts["fee_pct"] / 100
    max_exposure_pct = opts["max_exposure_per_asset_pct"] / 100
    rsi_buy, rsi_sell = opts["rsi_buy"], opts["rsi_sell"]
    stop_loss = opts["stop_loss_pct"] / 100
    take_profit = opts["take_profit_pct"] / 100
    use_score_engine = opts.get("use_score_engine", False)
    score_weights = {
        "rsi": opts.get("w_rsi", DEFAULT_WEIGHTS["rsi"]),
        "macd": opts.get("w_macd", DEFAULT_WEIGHTS["macd"]),
        "bollinger": opts.get("w_bollinger", DEFAULT_WEIGHTS["bollinger"]),
        "trend": opts.get("w_trend", DEFAULT_WEIGHTS["trend"]),
        "volume": opts.get("w_volume", DEFAULT_WEIGHTS["volume"]),
        "sentiment": opts.get("w_sentiment", DEFAULT_WEIGHTS["sentiment"]),
        "news_sentiment": opts.get("w_news_sentiment", DEFAULT_WEIGHTS["news_sentiment"]),
    }
    sentiment_model_path = opts.get("sentiment_model_path") or None

    # Sentiment global du marche (Fear & Greed) : une seule requete par cycle,
    # partagee entre tous les actifs -- ce n'est pas un signal par actif.
    sentiment = fetch_fear_greed()
    fear_greed_value = sentiment["value"] if sentiment else None

    prices: dict[str, float] = {}
    frames: dict[str, pd.DataFrame] = {}
    scores: dict[str, dict] = {}

    for symbol_cfg in opts["symbols"]:
        symbol, key = symbol_cfg["symbol"], symbol_cfg["entity_key"]
        try:
            df = fetch_ohlcv(opts["exchange"], symbol, opts["timeframe"])
            df = compute_all(df, fast=opts["sma_fast"], slow=opts["sma_slow"], rsi_period=opts["rsi_period"])
        except Exception:
            log.exception("Fetch impossible pour %s, actif ignore ce cycle", symbol)
            continue
        frames[key] = df
        prices[key] = float(df.iloc[-1]["close"])
        # Titres recents pour cet actif (rafraichis par refresh_news(), qui
        # tourne a son propre rythme) : None si le modele est indisponible
        # ou qu'aucun titre n'a pu etre classifie, plutot qu'un score neutre.
        news_sentiment_score = get_news_sentiment_score(
            _latest_titles_by_asset.get(key, []), sentiment_model_path
        )
        # calcule toujours le score (visibilite/monitoring dans HA), meme si
        # use_score_engine=false : ca permet de comparer avant de basculer.
        try:
            scores[key] = score_market(
                df, rsi_buy=rsi_buy, rsi_sell=rsi_sell, weights=score_weights,
                buy_threshold=opts.get("score_buy_threshold", 0.35),
                sell_threshold=opts.get("score_sell_threshold", -0.35),
                atr_stop_mult=opts.get("atr_stop_mult", 1.5),
                atr_target_mult=opts.get("atr_target_mult", 3.0),
                fear_greed_value=fear_greed_value,
                news_sentiment_score=news_sentiment_score,
            )
        except Exception:
            log.exception("Calcul du score impossible pour %s", symbol)

    total_before = portfolio_value(state, prices)

    for symbol_cfg in opts["symbols"]:
        key = symbol_cfg["entity_key"]
        symbol = symbol_cfg["symbol"]
        if key not in frames or len(frames[key]) < 2:
            continue
        df = frames[key]
        last, prev = df.iloc[-1], df.iloc[-2]
        price = prices[key]
        pos = state["positions"][key]

        bullish_cross = prev["sma_fast"] <= prev["sma_slow"] and last["sma_fast"] > last["sma_slow"]
        bearish_cross = prev["sma_fast"] >= prev["sma_slow"] and last["sma_fast"] < last["sma_slow"]
        score_result = scores.get(key)

        if use_score_engine and score_result:
            buy_signal = score_result["action"] == "buy"
            sell_signal_core = score_result["action"] == "sell"
            buy_reason = f"score {score_result['score']:+.2f}"
            sell_reason = f"score {score_result['score']:+.2f}"
        else:
            buy_signal = last["rsi"] < rsi_buy or bullish_cross
            sell_signal_core = last["rsi"] > rsi_sell or bearish_cross
            buy_reason = "RSI survente" if last["rsi"] < rsi_buy else "croisement haussier"
            sell_reason = "RSI surachat" if last["rsi"] > rsi_sell else "croisement baissier"

        # --- Sortie ---
        # Le stop-loss / take-profit en % reste actif dans tous les cas : c'est le
        # filet de securite, meme quand le score engine pilote l'entree/sortie normale.
        if pos["qty"] > 0:
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
            stop_hit = pnl_pct <= stop_loss
            tp_hit = pnl_pct >= take_profit

            if sell_signal_core or stop_hit or tp_hit:
                proceeds = pos["qty"] * price
                trade_fee = proceeds * fee
                state["cash"] += proceeds - trade_fee
                reason = "stop-loss" if stop_hit else "take-profit" if tp_hit else sell_reason
                record_trade(state, asset=key, symbol=symbol, action="SELL",
                              price=round(price, 2), qty=round(pos["qty"], 8),
                              fee=round(trade_fee, 2), pnl_pct=round(pnl_pct * 100, 2), reason=reason)
                state["positions"][key] = {"qty": 0.0, "entry_price": None, "invested": 0.0}
                continue

        # --- Entree ---
        total_now = portfolio_value(state, prices)
        current_exposure = pos["qty"] * price
        max_allowed = total_now * max_exposure_pct
        room = max_allowed - current_exposure

        if buy_signal and room > 10 and state["cash"] > 10:
            invest_amount = min(room, state["cash"] * 0.5)
            if invest_amount > 10:
                trade_fee = invest_amount * fee
                qty = (invest_amount - trade_fee) / price
                state["cash"] -= invest_amount
                pos["qty"] += qty
                pos["entry_price"] = price
                pos["invested"] += invest_amount
                record_trade(state, asset=key, symbol=symbol, action="BUY",
                              price=round(price, 2), qty=round(qty, 8),
                              fee=round(trade_fee, 2), pnl_pct=None, reason=buy_reason)

    total_after = portfolio_value(state, prices)
    push_to_ha(ha, opts, state, prices, total_after, scores, sentiment)
    log.info("Valeur portefeuille: %.2f EUR (depart %.2f, avant ce cycle %.2f)",
              total_after, state["initial_capital"], total_before)
    return state


def push_to_ha(ha: HomeAssistantClient, opts: dict, state: dict, prices: dict, total: float,
               scores: dict | None = None, sentiment: dict | None = None) -> None:
    initial = state["initial_capital"]
    return_pct = (total / initial - 1) * 100 if initial else 0.0

    ha.set_state("sensor.paper_portfolio_value", state=round(total, 2), attributes={
        "friendly_name": "Portefeuille simulation - valeur totale",
        "unit_of_measurement": "EUR",
        "device_class": "monetary",
        "state_class": "measurement",
    })
    ha.set_state("sensor.paper_portfolio_cash", state=round(state["cash"], 2), attributes={
        "friendly_name": "Portefeuille simulation - cash disponible",
        "unit_of_measurement": "EUR",
        "device_class": "monetary",
        "state_class": "measurement",
    })
    ha.set_state("sensor.paper_portfolio_return", state=round(return_pct, 2), attributes={
        "friendly_name": "Portefeuille simulation - performance",
        "unit_of_measurement": "%",
        "state_class": "measurement",
    })
    last_trades = state["trades"][-5:]
    ha.set_state("sensor.paper_portfolio_last_trade", state=(last_trades[-1]["action"] if last_trades else "aucun"),
                 attributes={"friendly_name": "Dernier trade (simulation)", "history": last_trades})

    if sentiment:
        ha.set_state("sensor.crypto_fear_greed_index", state=sentiment["value"], attributes={
            "friendly_name": "Indice Fear & Greed (marche crypto)",
            "state_class": "measurement",
            "classification": sentiment["classification"],
            "previous_value": sentiment["previous_value"],
            "trend": sentiment["trend"],
            "source": "alternative.me",
        })

    for symbol_cfg in opts["symbols"]:
        key = symbol_cfg["entity_key"]
        pos = state["positions"][key]
        value = pos["qty"] * prices.get(key, 0.0)
        ha.set_state(f"sensor.paper_position_{key}", state=round(value, 2), attributes={
            "friendly_name": f"Position simulee {key.upper()}",
            "unit_of_measurement": "EUR",
            "device_class": "monetary",
            "state_class": "measurement",
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
        })

        score_result = (scores or {}).get(key)
        if score_result:
            ha.set_state(f"sensor.paper_market_score_{key}", state=score_result["score"], attributes={
                "friendly_name": f"Score marche {key.upper()}",
                "state_class": "measurement",
                "action": score_result["action"],
                "confidence": score_result["confidence"],
                "breakdown": score_result["breakdown"],
                "atr": score_result["atr"],
                "suggested_stop": score_result["stop"],
                "suggested_target": score_result["target"],
            })


RESET_HELPER = "input_boolean.reset_paper_portfolio"

# Derniers titres par actif, rafraichis par refresh_news() et consommes par
# run_cycle() pour le score de sentiment news -- les deux tournent a des
# rythmes independants (NEWS_POLL_SECONDS vs interval_minutes), d'ou ce
# cache plutot qu'un appel direct aux flux a chaque cycle de trading.
_latest_titles_by_asset: dict[str, list[str]] = {}


def refresh_news(opts: dict, ha: HomeAssistantClient) -> None:
    """Interroge RSS (toujours) + CryptoPanic (si token configure) et pousse
    un sensor HA par actif suivi. Independant du cycle de trading -- la news
    a son propre rythme de rafraichissement (cf. NEWS_POLL_SECONDS)."""
    symbol_keys = [s["entity_key"] for s in opts["symbols"]]
    asset_keywords = {k: ASSET_KEYWORDS[k] for k in symbol_keys if k in ASSET_KEYWORDS}

    rss_by_asset = fetch_rss_news(asset_keywords, max_items_per_asset=5)

    cp_token = opts.get("cryptopanic_token") or None
    cp_currencies = [k.upper() for k in symbol_keys if k in ASSET_KEYWORDS]
    cp_items = fetch_cryptopanic_news(cp_token, cp_currencies, max_items=15) or []
    cp_by_asset: dict[str, list[dict]] = {key: [] for key in asset_keywords}
    for item in cp_items:
        for code in item.get("currencies") or []:
            key = code.lower()
            if key in cp_by_asset:
                cp_by_asset[key].append(item)

    for key in asset_keywords:
        rss_items = rss_by_asset.get(key, [])
        cp_items_for_asset = cp_by_asset.get(key, [])
        latest = (cp_items_for_asset[:1] or rss_items[:1] or [{}])[0]
        ha.set_state(
            f"sensor.crypto_news_{key}",
            state=latest.get("title", "aucune news recente")[:250],
            attributes={
                "friendly_name": f"News {key.upper()}",
                "rss": rss_items,
                "cryptopanic": cp_items_for_asset,
                "cryptopanic_enabled": cp_token is not None,
            },
        )
        # CryptoPanic en priorite (deja filtre par devise, plus fiable) complete
        # par RSS si besoin, dans l'ordre le plus recent d'abord.
        titles = [i["title"] for i in cp_items_for_asset if i.get("title")]
        titles += [i["title"] for i in rss_items if i.get("title") and i["title"] not in titles]
        _latest_titles_by_asset[key] = titles


def maybe_reset(opts: dict, state: dict, ha: HomeAssistantClient) -> dict:
    """Si le helper HA input_boolean.reset_paper_portfolio est active, on repart
    a zero (cash=initial_capital, aucune position, historique vide) et on
    desactive le helper pour ne pas reset en boucle. Absence du helper (pas
    encore cree cote HA) = get_state renvoie None, ce qui ne declenche rien."""
    if ha.get_state(RESET_HELPER) != "on":
        return state
    symbol_keys = [s["entity_key"] for s in opts["symbols"]]
    state = reset_state(opts["initial_capital"], symbol_keys)
    ha.call_service("input_boolean", "turn_off", {"entity_id": RESET_HELPER})
    return state


CONTROL_POLL_SECONDS = 10  # frequence de verification du helper de reset, independante du cycle de trading
NEWS_POLL_SECONDS = 20 * 60  # rythme du rafraichissement des news, independant du cycle de trading


def main() -> None:
    opts = load_options()
    ha = HomeAssistantClient(
        base_url=opts.get("ha_base_url") or None,
        token=opts.get("ha_token") or None,
    )
    symbol_keys = [s["entity_key"] for s in opts["symbols"]]
    state = load_state(STATE_PATH, opts["initial_capital"], symbol_keys)

    next_cycle_at = 0.0  # force un premier cycle de trading immediat au demarrage
    next_news_at = 0.0   # idem pour les news

    while True:
        opts = load_options()

        # Boucle de controle rapide (reset, futurs toggles) : quasi temps reel,
        # decouplee du rythme du cycle de trading qui reste lent expres
        # (evite le sur-trading, cf. fix bougie en cours de formation).
        state_before = state
        state = maybe_reset(opts, state, ha)
        if state is not state_before:
            save_state(STATE_PATH, state)
            log.info("Portefeuille reinitialise via input_boolean, sans attendre le prochain cycle")

        now = time.time()

        if now >= next_news_at:
            try:
                refresh_news(opts, ha)
            except Exception:
                log.exception("Echec du rafraichissement des news")
            next_news_at = now + NEWS_POLL_SECONDS

        if now >= next_cycle_at:
            try:
                state = run_cycle(opts, state, ha)
            except Exception:
                log.exception("Echec du cycle de simulation")
            save_state(STATE_PATH, state)
            next_cycle_at = now + max(60, int(opts.get("interval_minutes", 60)) * 60)

        time.sleep(CONTROL_POLL_SECONDS)


if __name__ == "__main__":
    main()
