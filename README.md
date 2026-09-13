# crypto-signals — Home Assistant Add-on

Add-on HA (installable depuis l'appli, sans terminal) qui calcule RSI + moyennes
mobiles sur BTC/ETH et pousse un état `buy` / `sell` / `hold` dans
`sensor.btc_signal` / `sensor.eth_signal`. Calcul 100% local, aucune IA requise
pour le fonctionnement.

## Installation (depuis ton téléphone, appli Home Assistant)

1. **Paramètres → Modules complémentaires → Boutique des add-ons**
2. Menu ⋮ (en haut à droite) → **Dépôts (Repositories)**
3. Colle : `https://github.com/adrienl82/crypto-signals` → Ajouter
4. Ferme, retourne dans la boutique (parfois il faut rafraîchir / attendre 30s),
   tu dois voir apparaître **"Crypto Signals"** tout en bas
5. Installe-le (le Pi build l'image, ça prend quelques minutes la 1ère fois)
6. Onglet **Configuration** de l'add-on : ajuste `symbols`, `interval_minutes`,
   `rsi_buy`/`rsi_sell` si tu veux — les valeurs par défaut fonctionnent telles
   quelles (BTC+ETH, toutes les heures)
7. **Démarrer** l'add-on, puis coche "Démarrage automatique" et "Watchdog"
8. Onglet **Log** : tu dois voir les lignes `close=... rsi=... -> hold/buy/sell`

Les entités `sensor.btc_signal` et `sensor.eth_signal` apparaissent alors dans
**Outils de développement → États**.

## Notifications & Dashboard

Trois capteurs par actif : `sensor.<x>_signal` (buy/sell/hold), `sensor.<x>_price`
et `sensor.<x>_rsi` (numériques — nécessaires pour tracer une tendance dans une
carte Lovelace, l'état d'un capteur texte n'étant pas graphable).

1. Colle `deploy/ha_automation_example.yaml` dans une automatisation
   (Paramètres → Automatisations → "+" → "..." → Modifier en YAML)
2. Colle `deploy/lovelace_dashboard_example.yaml` dans un dashboard → tu
   obtiens les signaux courants, les prix, un graphique d'historique
   prix+RSI par actif, et un interrupteur pour couper les notifs (c'est
   l'interrupteur natif marche/arrêt de l'automatisation elle-même — pas
   besoin de créer un helper séparé)

Note : l'`entity_id` exact d'une automatisation dépend de comment HA slugifie
son `alias` à la création. Si `automation.crypto_alerte_signal_btc` n'existe
pas tel quel dans ton dashboard, va voir Outils de développement → États,
cherche "crypto" pour trouver le vrai nom, et corrige la carte en conséquence.

## Pourquoi un add-on et pas systemd/cron ?

Tu es en **HA OS**, un système fermé : pas d'accès `systemctl` normal sur l'hôte.
Le mécanisme supporté pour du code perso qui tourne en continu, c'est l'add-on
(conteneur géré par le Supervisor, démarrage auto, logs intégrés, etc.).
Bonus : `homeassistant_api: true` donne l'accès à l'API HA sans créer de token
manuellement (le Supervisor l'injecte automatiquement).

## Limites

- Stratégie volontairement simple (RSI + croisement SMA) — outil d'aide à la
  décision, pas un bot de trading (aucun ordre n'est jamais passé).
- Historique brut dans `/data/history/*.csv` (persistant tant que l'add-on
  n'est pas désinstallé).

## Add-on 2 : Crypto Paper Trader (simulation live)

Deuxième add-on dans ce même dépôt (`crypto_paper_trader/`) : simule des
achats/ventes en continu avec un capital fictif (1000 EUR par défaut), en
utilisant des prix live (via ccxt) et la même stratégie que le backtest
(`backtest.py` à la racine). Aucun ordre réel n'est jamais passé.

### Installation
Même dépôt, donc même procédure : une fois `adrienl82/crypto-signals` ajouté
comme dépôt d'add-ons, tu verras aussi **"Crypto Paper Trader"** dans la
boutique, en plus de "Crypto Signals".

### Capteurs créés
- `sensor.paper_portfolio_value` — valeur totale (cash + positions), en EUR
- `sensor.paper_portfolio_cash`
- `sensor.paper_portfolio_return` — performance en %
- `sensor.paper_position_btc` / `sensor.paper_position_eth` — valeur de
  chaque position ouverte
- `sensor.paper_portfolio_last_trade` — dernier trade, avec les 5 derniers
  dans l'attribut `history`

### Configuration
Capital initial, frais (%), exposition max par actif, seuils RSI,
stop-loss/take-profit : tout est réglable dans l'onglet Configuration de
l'add-on. **Le capital initial n'est utilisé qu'à la toute première
exécution** (le portefeuille est ensuite persistant dans `/data/portfolio.json`).
Pour repartir de zéro : désinstalle/réinstalle l'add-on, ou supprime ce
fichier via l'add-on "File editor"/"Terminal & SSH".

### Dashboard (carte à ajouter)
```yaml
type: entities
title: Simulation trading (1000 EUR fictifs)
entities:
  - entity: sensor.paper_portfolio_value
    name: Valeur totale
  - entity: sensor.paper_portfolio_return
    name: Performance
  - entity: sensor.paper_portfolio_cash
    name: Cash disponible
  - entity: sensor.paper_position_btc
    name: Position BTC
  - entity: sensor.paper_position_eth
    name: Position ETH
  - entity: sensor.paper_portfolio_last_trade
    name: Dernier trade
```

### Limites
- Simulation pure, pas de garantie de performance, stratégie simple non
  optimisée — outil pédagogique/comparatif, pas un conseil financier
- Les prix récupérés dépendent de la disponibilité de l'exchange choisi
  (Kraken par défaut) pour la paire configurée
