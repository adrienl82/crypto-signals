"""Client minimal pour l'API REST de Home Assistant.

Doc HA: https://developers.home-assistant.io/docs/api/rest/
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def set_state(self, entity_id: str, state: str, attributes: dict | None = None) -> bool:
        """Crée/,met à jour une entité (ex: sensor.btc_signal)."""
        url = f"{self.base_url}/api/states/{entity_id}"
        payload = {"state": state, "attributes": attributes or {}}
        try:
            resp = requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            resp.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.error("HA set_state failed for %s: %s", entity_id, exc)
            return False
