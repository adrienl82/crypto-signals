# crypto-signals

Petit service qui tourne sur un Raspberry Pi (à côté de Home Assistant), récupère
les cours BTC/ETH, calcule RSI + moyennes mobiles, et pousse un signal
`buy` / `sell` / `hold` dans Home Assistant (`sensor.btc_signal`, `sensor.eth_signal`).

Le calcul est 100% local, sans dépendance à une IA — fiable, gratuit, pas de latence.

## Installation sur le Raspberry Pi

```bash
git clone <ton-repo> crypto-signals
cd crypto-signals
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
nano config.yaml   # remplir ha_base_url + ha_token
```

Test manuel :

```bash
python3 fetch_and_signal.py
```

Tu dois voir dans les logs un état calculé pour BTC et ETH, et les entités
`sensor.btc_signal` / `sensor.eth_signal` doivent apparaître dans Home Assistant
(Outils de développement -> États).

## Exécution automatique (systemd timer)

```bash
sudo cp deploy/crypto-signals.service /etc/systemd/system/
sudo cp deploy/crypto-signals.timer /etc/systemd/system/
# adapter les chemins User=/WorkingDirectory dans le .service à ton install
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-signals.timer
systemctl list-timers | grep crypto
```

Le service tourne alors tout seul, toutes les heures (configurable dans le `.timer`),
indépendamment de toute session Claude.

## Notifications côté Home Assistant

Voir `deploy/ha_automation_example.yaml` : une automatisation HA classique qui
envoie une notif push quand le signal change vers `buy` ou `sell`.

## Historique

Chaque exécution ajoute une ligne dans `history/btc.csv` et `history/eth.csv`
(close, rsi, sma...) — utile pour backtester la stratégie plus tard ou tracer
un graphique dans Grafana/HA.

## Limites à connaître

- RSI/SMA sur données quotidiennes = signaux peu fréquents (quelques fois par mois).
  Pour plus de réactivité, passe `timeframe` à `"4h"` ou `"1h"` dans `config.yaml`.
- La stratégie (`indicators.py: classify_signal`) est volontairement simple.
  Aucune garantie de performance — à considérer comme un outil d'aide à la
  décision, pas un système de trading automatique (aucun ordre n'est passé ici).
- Si tu veux un jour ajouter un résumé narratif quotidien via l'API Claude,
  ça se branche facilement en fin de `process_symbol()` — mais ce n'est pas
  nécessaire au fonctionnement du service.
