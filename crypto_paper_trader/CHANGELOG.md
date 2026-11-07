# Changelog

Toutes les évolutions notables de l'add-on **Crypto Paper Trader** sont
listées ici. Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.0.0/).

## 1.8.1 - 2026-11-08

### Corrigé
- Une position sur un actif retiré de `symbols` n'était plus jamais vendue
  et comptait pour 0 dans la valeur du portefeuille (observé en retirant
  MOVR : performance affichée à -50 % au lieu de -30 %). Elle est
  maintenant vendue au cycle suivant, raison « actif retire de la config ».

## 1.8.0 - 2026-11-07

### Contexte
Analyse du journal des décisions du 2 octobre au 7 novembre 2026 (score
engine, seuils ±0,15, cycle d'une minute) : -30 % sur le portefeuille
simulé, 820 trades en 36 jours. Avant frais, la stratégie était à peu près
à l'équilibre sur BTC/ETH/XRP ; ce sont les frais (environ 60 allers-retours
par actif à 0,52 % chacun) et la baisse du marché qui expliquent la perte.
Le backtest sur 2 ans montre aussi que le score seul n'a pas d'avantage,
même sans frais.

### Ajouté
- `strategy.py` : règles de trading partagées par le live et le backtest
  (un résultat de backtest décrit désormais exactement ce que fait l'add-on).
- Une seule décision par bougie clôturée : les cycles suivants sur la même
  bougie ne font que rafraîchir les capteurs (avant, le même signal
  déclenchait un achat à chaque minute).
- Une seule entrée par position (`allow_pyramiding: false`).
- `min_hold_bars` (défaut 4) : durée minimale de détention avant une sortie
  sur signal ; le stop-loss et le take-profit restent toujours actifs.
- `cooldown_bars` (défaut 2) : délai après une vente avant de racheter.
- `max_total_exposure_pct` (défaut 100) : plafond d'exposition totale (avec
  4 actifs à 40 % chacun, l'exposition pouvait monter à 160 %).
- `trend_filter_days` (défaut 100, 0 = désactivé) : n'achète que si le prix
  est au-dessus de sa moyenne sur N jours, et sort quand il passe dessous.
  Backtest BTC/ETH/XRP en 4h, février 2025 à novembre 2026 : -8,5 % (pire
  baisse -17 %) contre -27,5 % sans filtre et -36,5 % en buy & hold (pire
  baisse -62 %).
- `history_exchange` (défaut `binance`) : source de l'historique du backtest.

### Corrigé
- Le backtest embarqué ne couvrait que 30 jours en 1h malgré
  `backtest_days: 365` : l'API OHLC de Kraken ne renvoie que les 720
  dernières bougies. L'historique vient maintenant de Binance (paire EUR, ou
  USDT si elle n'existe pas), avec Kraken en secours et un avertissement si
  la période obtenue est trop courte.

### Modifié
- Valeurs par défaut : `timeframe: 4h`, `interval_minutes: 15`,
  `use_score_engine: true`, `backtest_days: 730`. Le filtre de 100 jours
  demande 600 bougies : impossible en 1h avec Kraken (720 bougies max), d'où
  le passage en 4h. Ces valeurs ne s'appliquent pas aux options déjà
  enregistrées dans Home Assistant : il faut les modifier dans l'onglet
  Configuration de l'add-on.

## 1.7.0 - 2026-10-02

### Ajouté
- Journal complet des décisions : chaque cycle écrit désormais une ligne par
  actif dans `/share/crypto-paper-trader/decisions.csv` -- y compris les
  "hold", contrairement à l'historique des trades qui ne garde que les
  BUY/SELL exécutés. Colonnes : timestamp, actif, prix, mode (legacy/score),
  signaux bruts, score, confiance, breakdown par indicateur (JSON), action
  effective et raison. Objectif : après plusieurs jours d'observation,
  pouvoir confronter ce que le moteur a vu à chaque instant (pas seulement
  les trades passés) à l'évolution réelle du prix ensuite, pour affiner les
  poids/seuils sur des données plutôt qu'à l'intuition.

## 1.6.2 - 2026-10-02

### Corrigé
- Prix de revient (`entry_price`) écrasé au lieu d'être moyenné lors d'un
  rachat sur une position déjà ouverte (le score engine peut racheter un
  actif plusieurs cycles de suite tant que le score reste au-dessus du
  seuil, observé en conditions réelles : 4 achats ETH en 4 minutes). Avant
  le fix, `entry_price` ne reflétait que le dernier achat partiel, ce qui
  faussait le déclenchement du stop-loss/take-profit et le `pnl_pct`
  affiché à la revente. Corrigé dans `run_loop.py`, `backtest_core.py` et
  `backtest.py` (moyenne pondérée qté × prix à chaque achat supplémentaire).

## 1.6.1 - 2026-10-02

### Modifié
- `interval_minutes` accepte désormais des valeurs de 1 à 1440 (au lieu de
  5 à 1440), pour permettre un cycle de trading jusqu'à chaque minute --
  utile pour observer le comportement du score engine sur un timeframe très
  court (`timeframe: "1m"`) en mode test/observation. Le plancher reel de
  60s dans run_loop.py (`max(60, interval_minutes*60)`) n'a pas changé.

## 1.6.0 - 2026-09-30

### Ajouté
- Backtest embarqué sur données Kraken réelles, déclenchable depuis Home
  Assistant via le helper `input_boolean.run_crypto_backtest` (même principe
  que le reset de portefeuille) : compare la stratégie actuellement déployée
  (RSI + croisement SMA), le moteur de score multi-indicateurs
  (`use_score_engine=true`) et un buy & hold de référence, sur les actifs
  suivis (`symbols`) et la config live (poids/seuils/frais). Résumé poussé
  dans `sensor.paper_backtest_summary` / `sensor.paper_backtest_status`,
  courbes d'équity et journaux de trades complets écrits dans
  `/share/crypto-backtest/`. Nouvelle option `backtest_days` (défaut 365).
  Complète `backtest.py` (racine du repo), pensé pour un lancement manuel
  sur PC avec export graphique.

### Notes
- L'indicateur `news_sentiment` (LLM local sur les titres RSS/CryptoPanic)
  n'est pas backtestable : aucune archive historique des titres passés
  n'existe. Le backtest utilise donc RSI/MACD/Bollinger/tendance/volume et,
  si disponible, l'historique Fear & Greed -- pas le 7e indicateur.

## 1.5.2 - 2026-09-16

### Modifié
- Flux RSS élargis : remplacement de `CryptoSlate` (bloqué par un challenge
  Cloudflare, HTTP 403 constant) par Bitcoinist, et ajout de 8 nouvelles
  sources (Bitcoin.com, AMBCrypto, CryptoNews, BeInCrypto, The Defiant,
  Protos, Watcher.Guru, The Daily Hodl) pour une meilleure couverture des
  news par actif.
- URL U.Today corrigée (`/rss` → `/rss.php`, l'ancienne faisait une
  redirection 301).

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
