# Changelog

Toutes les évolutions notables de l'add-on **Crypto Paper Trader** sont
listées ici. Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/).

## 1.5.1 - 2026-09-16

### Corrigé
- Build Docker : ajout de `linux-headers` (fournit `linux/limits.h`, absent
  de la libc musl) requis par la compilation de `llama-cpp-python` depuis
  les sources sur Alpine/aarch64. Sans ce paquet, le build échouait avec
  `fatal error: linux/limits.h: No such file or directory`.

## 1.5.0 - 2026-09-16

### Ajouté
- Signal de sentiment par actif basé sur les news : classification bullish/bearish
  des derniers titres RSS/CryptoPanic via un modèle LLM local fine-tuné
  (Qwen2.5-1.5B, quantifié GGUF), chargé avec `llama-cpp-python`.
- Nouvelle option `w_news_sentiment` (poids du signal news dans le moteur de
  score), indépendante du `w_sentiment` existant (Fear & Greed global).
- Option `sentiment_model_path` pour pointer vers le fichier `.gguf` du modèle.
- Mapping `share:rw` pour charger le modèle depuis `/share/` sans l'embarquer
  dans l'image Docker.

### Modifié
- `Dockerfile` : compilation de `llama-cpp-python` depuis les sources
  (ajout de `cmake`, `g++`, `make`), aucune roue precompilee ne ciblant
  musl/aarch64.

## 1.4.1 - 2026-09-15

### Corrigé
- Endpoint CryptoPanic `v1` → `v2` (l'ancien renvoyait une erreur).

## 1.4.0 - 2026-09-15

### Ajouté
- Suivi des news crypto par actif suivi (flux RSS toujours actifs,
  CryptoPanic optionnel via token) : sensors HA `sensor.crypto_news_<actif>`.

## 1.3.0 - 2026-09-15

### Modifié
- Reset du portefeuille rendu quasi temps réel, découplé du rythme du cycle
  de trading (déclenché via `input_boolean.reset_paper_portfolio` sans
  attendre le prochain cycle).

## 1.2.0 - 2026-09-15

### Ajouté
- Reset du portefeuille pilotable depuis Home Assistant via un
  `input_boolean` dédié.
- Sentiment global du marché (indice Fear & Greed, alternative.me) comme
  6ᵉ composante du moteur de score, en contrarien.

### Corrigé
- Le cycle ne plante plus si `timeframe` est vide ou invalide dans la config
  (fix défensif, log d'avertissement au lieu d'un crash).
- Exclusion de la bougie en cours de formation du calcul des signaux (RSI /
  croisement SMA), qui provoquait du sur-trading.

## 1.1.0 - 2026-09-15

### Ajouté
- Moteur de score multi-indicateurs optionnel (`use_score_engine`) combinant
  RSI, MACD, Bollinger, ADX (force de tendance) et volume en un score
  pondéré -1..+1, avec stop/target suggérés à partir de l'ATR.

## 1.0.0 - 2026-09-13

### Ajouté
- Première version de l'add-on : simulation d'achat/vente BTC/ETH en continu
  sur capital fictif, signaux RSI + croisement SMA, frais type Kraken,
  exposition maximale par actif, stop-loss/take-profit, publication de
  l'état du portefeuille dans Home Assistant.
