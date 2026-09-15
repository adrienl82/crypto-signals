"""Client pour l'API HA, via le proxy Supervisor quand on tourne en add-on.

Dans un add-on avec `homeassistant_api: true` et `hassio_api: true`, le
Supervisor injecte automatiquement la variable d'environnement
SUPERVISOR_TOKEN, et l'API HA est joignable sur http://supervisor/core/api.
Doc: https://developers.home-assistant.io/docs/add-ons/communication/
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


class HomeAssistantClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 5.0):
        self.base_url = (base_url or "http://supervisor/core/api").rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        if not self.token:
            log.warning("Aucun token disponible (ni SUPERVISOR_TOKEN, ni ha_token en option) — les appels échoueront en 401.")
        else:
            log.info("Token pret (longueur=%d, source=%s)", len(self.token), "option ha_token" if token else "SUPERVISOR_TOKEN")

    def set_state(self, entity_id: str, state: str, attributes: dict | None = None) -> bool:
        url = f"{self.base_url}/states/{entity_id}"
        payload = {"state": state, "attributes": attributes or {}}
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("HA set_state failed for %s: %s", entity_id, exc)
            return False

    def get_state(self, entity_id: str) -> str | None:
        """Retourne l'etat brut (string) de l'entite, ou None si absente/erreur
        (ex: helper pas encore cree cote HA -> on ne bloque pas le cycle pour ca)."""
        url = f"{self.base_url}/states/{entity_id}"
        try:
            resp = requests.get(url, headers=self.headers, timeout=self.timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("state")
        except requests.RequestException as exc:
            log.error("HA get_state failed for %s: %s", entity_id, exc)
            return None

    def call_service(self, domain: str, service: str, data: dict | None = None) -> bool:
        url = f"{self.base_url}/services/{domain}/{service}"
        try:
            resp = requests.post(url, json=data or {}, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("HA call_service failed for %s.%s: %s", domain, service, exc)
            return False
